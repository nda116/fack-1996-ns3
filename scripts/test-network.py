#!/usr/bin/env python3

import argparse
import csv
import shutil
import sys
import tempfile
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

from validate import TIMER_FIELDS, rows, validate, validate_wire, validate_ns1_timer
from ns3_paths import find_binary
from ns3_paths import profile_options, read_settings

TEST_SCHEMA = 2
ALGORITHMS = ("reno", "reno-sack", "fack")

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def network_cases(profile="standard"):
    scenarios = (
        ("no-loss", [], 50, {"retransmissions": 0, "timeouts": 0, "duplicate_segments": 0}, []),
        ("one-interior-loss", ["--extraDrops=10"], 50,
         {"injected_drops": 1, "retransmissions": 1, "timeouts": 0, "duplicate_segments": 0}, ["fast-recovery"]),
        ("tail-loss-rto", ["--extraDrops=29"], 30,
         {"injected_drops": 1, "retransmissions": 1, "rto_expiries": 1, "forced_timeouts": 0, "duplicate_segments": 0}, ["rto"]),
        ("multi-interior-loss", ["--extraDrops=10,12,14"], 50,
         {"injected_drops": 3}, ["multi-loss"]),
        ("initial-ack-loss", ["--extraAckDrops=1"], 50,
         {"injected_ack_drops": 1, "retransmissions": 1, "rto_expiries": 1, "forced_timeouts": 0, "duplicate_segments": 1}, ["ack-loss", "rto"]),
        ("lost-retransmission", ["--extraDrops=10", "--extraRetransmissionDrops=10"], 300,
         {"injected_drops": 1, "injected_retransmission_drops": 1}, ["lost-retransmission"]),
    )
    cases = []
    for algorithm in ALGORITHMS:
        for name, injected, segments, expected, mechanisms in scenarios:
            cases.append({"case": f"network-tests/{algorithm}/{name}", "kind": "tcp",
                          "options": [f"--algorithm={algorithm}", "--packetWindow=true", "--integerCut=false",
                                      "--queue=100", "--stop=30", f"--totalSegments={segments}", *injected],
                          "expected": {"algorithm": algorithm, "delivered_segments": segments, **expected},
                          "mechanisms": mechanisms})
    for name, options in (("rampdown", ["--rampdown=true"]),
                          ("overdamping", ["--overdamping=true"]),
                          ("both", ["--rampdown=true", "--overdamping=true"])):
        cases.append({"case": f"network-tests/fack/options-{name}", "kind": "tcp",
                      "options": ["--algorithm=fack", "--packetWindow=true", "--integerCut=false",
                                  "--queue=100", "--stop=30", "--totalSegments=100",
                                  "--extraDrops=10,12,14", *options],
                      "expected": {"algorithm": "fack", "delivered_segments": 100, "injected_drops": 3},
                      "mechanisms": [name]})
    for algorithm in ALGORITHMS:
        for detail in (True, False):
            mode = "full" if detail else "light"
            options = [f"--algorithm={algorithm}", "--packetWindow=true", "--integerCut=false",
                       "--queue=20", "--tinyBurstBytes=6000", "--seed=1", "--stop=10", "--warmup=2",
                       "--tinyRate=2", f"--detail={str(detail).lower()}"]
            if algorithm == "fack":
                options += ["--overdamping=true", "--rampdown=true"]
            cases.append({"case": f"network-tests/{algorithm}/logging-{mode}", "kind": "tcp",
                          "options": options, "expected": {"algorithm": algorithm, "detail": detail},
                          "mechanisms": ["logging-equivalence"]})
    for queue in (2, 6, 17, 24):
        cases.append({"case": f"queue-tests/q{queue:02}", "kind": "queue_probe",
                      "options": ["--queueProbe=true", "--queueProbePackets=30", f"--queue={queue}"],
                      "expected": {"mode": "queue_probe", "queue": queue, "queue_probe_packets": 30},
                      "mechanisms": ["queue-rule-equivalence"]})
    for case in cases:
        if case["kind"] == "tcp":
            case["options"] += profile_options(profile)
    return cases

def definition_sha256(profile="standard"):
    return hashlib.sha256(json.dumps(network_cases(profile), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def assert_case(folder, definition):

    folder = Path(folder)
    summary = json.loads((folder / "summary.json").read_text())

    def check(condition, message):
        if not condition:
            raise AssertionError(f"{definition['case']}: {message}")

    for key, value in definition["expected"].items():
        check(summary[key] == value, f"{key}={summary[key]}, expected {value}")
    mechanisms = set(definition["mechanisms"])
    if definition["kind"] == "queue_probe":
        check(summary["drops"] == summary["expected_drops"], "queue source-rule drops differ")
        return {key: summary[key] for key in definition["expected"]} | {"drops": summary["drops"]}
    if "logging-equivalence" in mechanisms:
        return {key: value for key, value in summary.items() if key not in ("detail", "timer_profile_trace")}
    trace = rows(folder / "events.csv")
    names = [r["event"] for r in trace]
    entries = [r for r in trace if r["event"] == "recovery-enter"]
    check(summary["drops"] == summary["injected_drops"] + summary["injected_retransmission_drops"]
          and summary["ack_drops"] == summary["injected_ack_drops"],
          "controlled loss case accidentally experienced DropTail loss")
    if "fast-recovery" in mechanisms:
        check("reno-fast-retransmit" in names or entries, "injected interior loss did not cause fast recovery")
    if "rto" in mechanisms:
        check(summary["rto_expiries"] >= 1, "test did not exercise an ordinary RTO")
        check(any(r["event"] == "timeout" for r in rows(folder / "timers.csv")), "RTO missing in timer trace")
    if "multi-loss" in mechanisms:
        check(summary["retransmissions"] >= 3, "not all injected holes were repaired")
        if summary["algorithm"] != "reno":
            repaired = {int(r["segment"]) for r in trace if r["event"] == "selective-retransmit"}
            check({10, 12, 14}.issubset(repaired), "SACK-based recovery did not selectively repair each injected hole")
    if "multi-loss" in mechanisms and summary.get("sack_option_bytes") and summary["algorithm"] != "reno":
        physical = rows(folder / "link-events.csv")
        sizes = {int(r["wire_bytes"]) for r in physical
                 if int(r["is_ack"]) and not int(r["reverse_flow"])}
        check({40, 48, 56, 64}.issubset(sizes), "wire test missed a 0/1/2/3-block SACK ACK")
    if "ack-loss" in mechanisms:
        ack_drops = [r for r in rows(folder / "link-events.csv") if r["event"] == "injected-ack-drop"]
        check(len(ack_drops) == 1, "ACK fault injection missing from physical forwarding trace")
    if "lost-retransmission" in mechanisms:
        check(summary["retransmissions"] >= 2, "lost repair was not retransmitted again")
        injected = [r for r in rows(folder / "network.csv") if r["event"] == "injected-retransmission-drop"]
        check(len(injected) == 1 and int(injected[0]["segment"]) == 10, "repair-loss injection trace is incorrect")
        if summary["algorithm"] == "fack":
            check(summary["forced_timeouts"] >= 1 and "lost-retransmission" in names,
                  "FACK lost-copy evidence did not force a controller restart")
        else:
            check(summary["rto_expiries"] >= 1, "lost repair did not exercise timer-based recovery")
    if mechanisms & {"rampdown", "both"}:
        check(any(float(r["wintrim"]) > 0 for r in entries), "Rampdown test never had a positive trim")
        check(any(r["event"] == "ack" and float(r["recovery"]) and float(r["wintrim"]) > 0 for r in trace),
              "Rampdown trim was never carried through recovery ACKs")
    if mechanisms & {"overdamping", "both"}:
        check(any(abs(float(r["winmult"]) - .25) < 1e-9 for r in entries), "Overdamping test never used the Slow-start quarter cut")
    observed_keys = set(definition["expected"]) | {
        "retransmissions", "timeouts", "rto_expiries", "forced_timeouts", "duplicate_segments",
        "recovery_count", "injected_drops", "injected_retransmission_drops", "injected_ack_drops"}
    return {key: summary[key] for key in sorted(observed_keys)}

def assert_logging_pairs(result_root):

    paired = []
    for algorithm in ALGORITHMS:
        cases = [Path(result_root) / f"network-tests/{algorithm}/logging-{mode}" for mode in ("full", "light")]
        summaries = [json.loads((folder / "summary.json").read_text()) for folder in cases]
        comparable = [{key: value for key, value in s.items() if key not in ("detail", "timer_profile_trace")} for s in summaries]
        if comparable[0] != comparable[1]:
            differences = [key for key in comparable[0] if comparable[0][key] != comparable[1].get(key)]
            raise AssertionError(f"{algorithm}: full/light summaries differ: {differences}")
        reverse = [json.loads((folder / "reverse/summary.json").read_text()) for folder in cases]
        if [{k: v for k, v in s.items() if k not in ("detail", "timer_profile_trace")} for s in reverse][0] != {
                k: v for k, v in reverse[1].items() if k not in ("detail", "timer_profile_trace")}:
            raise AssertionError(f"{algorithm}: reverse full/light summaries differ")
        paired.append({"algorithm": algorithm, "full_case": str(cases[0].relative_to(result_root)),
                       "light_case": str(cases[1].relative_to(result_root)), "summary_values_identical": True,
                       "reverse_summary_values_identical": True})
    return paired

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ns3", type=Path, required=True)
    parser.add_argument("--study", type=Path, required=True)
    a = parser.parse_args()
    a.ns3, a.study = a.ns3.resolve(), a.study.resolve()
    binary_hash = sha(find_binary(a.ns3))
    sources = {str(f.relative_to(a.study)): sha(f)
               for directory in ("model", "tests", "scripts")
               for f in sorted((a.study / directory).glob("*")) if f.is_file()}
    profile = read_settings(a.study)["profile"]
    common_timer, reports = None, []
    for definition in network_cases(profile):
        folder = a.study / "results" / definition["case"]
        folder.mkdir(parents=True, exist_ok=True)
        command = ["./ns3", "run", "--no-build", "--quiet",
                   shlex.join(["fack-paper", "--out=" + str(folder), *definition["options"]])]
        run = subprocess.run(command, cwd=a.ns3, capture_output=True, text=True)
        (folder / "run.log").write_text(run.stdout + run.stderr)
        provenance = {"utc": datetime.now(timezone.utc).isoformat(), "command": command,
                      "returncode": run.returncode, "binary_sha256": binary_hash,
                      "ns3_version": (a.ns3 / "VERSION").read_text().strip(), "source_sha256": sources}
        (folder / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        run.check_returncode()
        result = validate(folder)
        observed = assert_case(folder, definition)
        summary = json.loads((folder / "summary.json").read_text())
        if definition["kind"] == "tcp":
            timer = {field: summary[field] for field in TIMER_FIELDS}
            common_timer = timer if common_timer is None else common_timer
            if timer != common_timer:
                raise AssertionError(f"{definition['case']}: timer configuration differs between algorithms")
        outputs = {str(f.relative_to(folder)): sha(f) for f in sorted(folder.rglob("*.csv"))}
        outputs["summary.json"] = sha(folder / "summary.json")
        if (folder / "reverse/summary.json").exists():
            outputs["reverse/summary.json"] = sha(folder / "reverse/summary.json")
        provenance.update({"validation": result, "output_sha256": outputs,
                           "csv_sha256": {k: v for k, v in outputs.items() if k.endswith(".csv")}})
        (folder / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        reports.append({**definition, "observed": observed, "validation": result})
        print(f"PASS {definition['case']}: {', '.join(definition['mechanisms']) or 'no injected loss'}", flush=True)
    report = {"status": "PASS", "schema": TEST_SCHEMA, "producer_sha256": sha(Path(__file__)),
              "definition_sha256": definition_sha256(profile), "cases": reports, "shared_timer": common_timer,
              "logging_equivalence_pairs": assert_logging_pairs(a.study / "results"),
              "meaning": "controlled finite recovery, shared timers, FACK options and NetDevice queue-rule equivalence; not paper curve matching or original ns-1 runtime execution"}
    (a.study / "results/network-tests.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS: {len(reports)} controlled network/device cases.")

def timer_main():
    p = argparse.ArgumentParser()
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument("--binary", type=Path)
    selection.add_argument("--ns3", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--legacy-results", type=Path, help="optional frozen figure-12 CSV directory for exact legacy regression")
    a = p.parse_args()
    if a.ns3:
        a.binary = find_binary(a.ns3)
    a.output.mkdir(parents=True, exist_ok=True)
    reports, reverse_pairs, mutation_source = [], {}, None
    for alg in ("reno", "reno-sack", "fack"):
        for case, args in (
            ("no-loss", ["--queue=100", "--totalSegments=50", "--stop=5"]),
            ("tail-loss", ["--queue=100", "--totalSegments=30", "--extraDrops=29", "--stop=10"]),
            ("multi-loss", ["--queue=100", "--totalSegments=50", "--extraDrops=10,12,14", "--stop=30"]),
            ("reverse-full", ["--queue=20", "--tinyBurstBytes=6000", "--seed=1", "--stop=5", "--warmup=1"]),
            ("reverse-light", ["--queue=20", "--tinyBurstBytes=6000", "--seed=1", "--stop=5", "--warmup=1", "--detail=false"]),
        ):
            folder = a.output / alg / case
            folder.mkdir(parents=True, exist_ok=True)
            command = [str(a.binary.resolve()), f"--algorithm={alg}", "--ns1TimestampTimer=true",
                       "--packetWindow=true", "--integerCut=false", "--sackOptionBytes=true", "--out=.", *args]
            if alg == "fack" and case.startswith("reverse"):
                command += ["--overdamping=true", "--rampdown=true"]
            run = subprocess.run(command, cwd=folder, capture_output=True, text=True)
            if run.returncode:
                raise RuntimeError(run.stdout + run.stderr)
            report = validate(folder, write=False)
            summary = json.loads((folder / "summary.json").read_text())
            assert summary["ns1_timestamp_timer"] == 1
            if case != "reverse-light":
                wire = list(csv.DictReader((folder / "link-events.csv").open()))
                for row in wire:
                    assert int(row["wire_bytes"]) == (40 + 8 * int(row["sack_blocks"]) if int(row["is_ack"]) else 1000)
                if case == "multi-loss" and alg != "reno":
                    assert {int(r["sack_blocks"]) for r in wire if int(r["is_ack"])} == {0, 1, 2, 3}
            if case == "no-loss":
                assert summary["delivered_segments"] == 50 and summary["retransmissions"] == 0 and summary["timeouts"] == 0
                assert summary["ns1_timer_samples"] == 50 and summary["ns1_duplicate_ack_samples"] == 0
            if case == "tail-loss":
                assert summary["delivered_segments"] == 30 and summary["rto_expiries"] >= 1
            if case == "multi-loss":
                assert summary["delivered_segments"] == 50 and summary["injected_drops"] == 3
                if alg == "reno-sack":
                    assert summary["ns1_partial_sack_acks"] > 0
                if alg == "fack":
                    assert summary["ns1_duplicate_ack_samples"] > 0
                    mutation_source = folder
            if case.startswith("reverse"):
                reverse = json.loads((folder / "reverse/summary.json").read_text())
                for key in ("ns1_timestamp_timer", "timer_policy", "initial_rto_s", "min_rto_s", "max_rto_s", "timer_tick_s"):
                    assert reverse[key] == summary[key], key
                if case == "reverse-full":
                    validate(folder / "reverse", write=False)
                reverse_pairs.setdefault(alg, []).append((summary, reverse))
            reports.append({"algorithm": alg, "case": case, "checks": report["checks"]})
            print("PASS", alg, case, report["checks"], flush=True)
    for alg, pair in reverse_pairs.items():
        for index in (0, 1):
            comparable = [{k: v for k, v in x[index].items() if k not in ("detail", "timer_profile_trace")} for x in pair]
            assert comparable[0] == comparable[1], (alg, "full/light changed measured summary")

    mutations = 0
    for mutation in ("deadline", "missing-sample", "metadata"):
        with tempfile.TemporaryDirectory(prefix="timer-mutation-", dir=a.output) as temp:
            target = Path(temp)
            for name in ("summary.json", "events.csv", "acks.csv", "timers.csv", "timer-profile.csv"):
                shutil.copyfile(mutation_source / name, target / name)
            summary = json.loads((target / "summary.json").read_text())
            rows = list(csv.DictReader((target / "timer-profile.csv").open()))
            names = list(rows[0])
            if mutation == "deadline":
                row = next(r for r in rows if r["event"] == "arm")
                row["delay_s"] = str(float(row["delay_s"]) + .1)
            elif mutation == "missing-sample":
                row = next(r for r in rows if r["event"] == "sample" and int(r["sequence"]) == int(r["una"]))
                rows.remove(row)
            else:
                summary["ns1_timestamp_timer"] = 0
            with (target / "timer-profile.csv").open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=names); writer.writeheader(); writer.writerows(rows)
            def check(value, message):
                if not value:
                    raise AssertionError(message)
            try:
                validate_ns1_timer(target, summary, check)
            except AssertionError:
                mutations += 1
            else:
                raise AssertionError("replay accepted mutation " + mutation)
    wire_faults = 0
    for mutation in ("missing-journal", "wrong-size"):
        with tempfile.TemporaryDirectory(prefix="wire-mutation-", dir=a.output) as temp:
            target = Path(temp)
            summary = json.loads((mutation_source / "summary.json").read_text())
            if mutation == "wrong-size":
                with (mutation_source / "link-events.csv").open(newline="") as stream:
                    physical = list(csv.DictReader(stream))
                physical[0]["wire_bytes"] = str(int(physical[0]["wire_bytes"]) + 1)
                with (target / "link-events.csv").open("w", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(physical[0]))
                    writer.writeheader(); writer.writerows(physical)
            try:
                validate_wire(target, summary)
            except AssertionError:
                wire_faults += 1
            else:
                raise AssertionError("wire validator accepted mutation " + mutation)
    legacy = []
    if a.legacy_results:
        for alg in ("reno", "fack"):
            folder = a.output / "legacy" / alg
            folder.mkdir(parents=True, exist_ok=True)
            command = [str(a.binary.resolve()), f"--algorithm={alg}", "--ns1TimestampTimer=false",
                       "--packetWindow=true", "--integerCut=false", "--sackOptionBytes=false", "--queue=10", "--tinyBurstBytes=6000", "--seed=1",
                       "--stop=15", "--out=."]
            if alg == "fack": command += ["--overdamping=true", "--rampdown=true"]
            subprocess.run(command, cwd=folder, capture_output=True, text=True, check=True)
            validate(folder, write=False)
            matched = []
            for original in sorted((a.legacy_results / alg).rglob("*.csv")):
                relative = original.relative_to(a.legacy_results / alg)
                assert (folder / relative).read_bytes() == original.read_bytes(), (alg, str(relative))
                matched.append(str(relative))
            assert not (folder / "timer-profile.csv").exists()
            legacy.append({"algorithm": alg, "byte_identical_csv": matched})
            print("PASS legacy", alg, len(matched), "CSV byte-identical", flush=True)
    report = {"status": "PASS", "cases": reports, "logging_pairs": 3, "rejected_mutations": mutations,
              "legacy_cases": legacy, "wire_faults_rejected": wire_faults,
              "binary_sha256": hashlib.sha256(a.binary.read_bytes()).hexdigest(),
              "producer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (a.output / "timer-tests.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS {15+len(legacy)} network runs, 3 full/light pairs, 3 rejected mutations", flush=True)

if __name__ == "__main__":
    if "--timer-tests" in sys.argv:
        sys.argv.remove("--timer-tests")
        timer_main()
    else:
        main()
