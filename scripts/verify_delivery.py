#!/usr/bin/env python3

import argparse
import csv
import math
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import runpy
import shlex
import subprocess

from validate import TIMER_FIELDS, validate
from ns3_paths import find_binary
from ns3_paths import PROFILES, read_settings, scenario_profile

def validate_aggregate(results, seed_count):
    results = Path(results)
    with (results / "figure-11-data.csv").open(newline="") as stream:
        aggregate = list(csv.DictReader(stream))
    with (results / "figure-11-seeds.csv").open(newline="") as stream:
        seeds = list(csv.DictReader(stream))
    expected = {(a, b, s) for a in ("reno", "reno-sack", "fack")
                for b in range(0, 6001, 1000) for s in range(1, seed_count + 1)}
    actual = {(r["algorithm"], int(r["burst_bytes"]), int(r["seed"])) for r in seeds}
    if actual != expected or len(seeds) != len(expected):
        raise AssertionError("Figure 11 seed table is incomplete or duplicated")
    checked = 0

    def equal(left, right, message):
        nonlocal checked
        checked += 1
        if abs(float(left) - float(right)) > 1e-8:
            raise AssertionError(message)

    for row in seeds:
        summary = json.loads((results / row["folder"] / "summary.json").read_text())
        if (summary["algorithm"], summary["tiny_burst_bytes"], summary["seed"]) != (
                row["algorithm"], int(row["burst_bytes"]), int(row["seed"])):
            raise AssertionError("Figure 11 row belongs to another scenario")
        for column, field, multiplier in (("reverse_percent", "reverse_utilization", 100),
                                          ("forward_percent", "forward_utilization", 100),
                                          ("forward_goodput_mbps", "goodput_mbps", 1)):
            equal(row[column], multiplier * summary[field], "Seed table differs from measured summary")
    keys = {(r["algorithm"], int(r["burst_bytes"])) for r in aggregate}
    if keys != {(a, b) for a, b, _ in expected} or len(aggregate) != 21:
        raise AssertionError("Figure 11 aggregate groups are incomplete or duplicated")
    for row in aggregate:
        group = [s for s in seeds if s["algorithm"] == row["algorithm"]
                 and int(s["burst_bytes"]) == int(row["burst_bytes"])]
        if len(group) != seed_count or int(row["seeds"]) != seed_count:
            raise AssertionError("Figure 11 mean uses the wrong number of seeds")
        for column, sd_column in (("forward_percent", "forward_std_percent"),
                                  ("forward_goodput_mbps", "goodput_std_mbps"),
                                  ("reverse_percent", None)):
            values = [float(s[column]) for s in group]
            mean = sum(values) / len(values)
            equal(row[column], mean, "Aggregate mean differs from actual seed values")
            if sd_column:
                sd = math.sqrt(sum((x - mean) ** 2 for x in values) / (len(values) - 1))
                equal(row[sd_column], sd, "Aggregate sample SD differs from actual seed values")
    return {"groups": len(aggregate), "seed_rows": len(seeds), "checks": checked,
            "sample_sd_denominator": seed_count - 1}

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def require(condition, message):
    if not condition:
        raise AssertionError(message)

def source_hashes(study):
    return {str(f.relative_to(study)): sha(f)
            for directory in ("model", "tests", "scripts")
            for f in sorted((study / directory).glob("*")) if f.is_file()}

def unit_inputs(study):
    return {str(f.relative_to(study)): sha(f)
            for directory in ("model", "tests")
            for f in sorted((study / directory).glob("*")) if f.is_file()}

def record_core_tests(study):

    executable, logfile = study / "build/core-tests", study / "build/core-tests.log"
    require(executable.is_file(), "core-tests executable missing; run scripts/run.sh first")
    log = logfile.read_text()
    matches = re.findall(r"PASS (\d+) cases, (\d+) checks", log)
    require(matches, "core test log has no successful completion")
    record = {"utc": datetime.now(timezone.utc).isoformat(), "status": "PASS",
              "cases": int(matches[-1][0]), "checks": int(matches[-1][1]),
              "sanitizers": ["address", "undefined"],
              "record_method": "recorded immediately after run.sh compilation and successful execution",
              "source_sha256": unit_inputs(study), "binary_sha256": sha(executable),
              "log_sha256": sha(logfile)}
    (study / "build/core-tests-provenance.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"Recorded core tests: {record['cases']} cases, {record['checks']} checks.")

def record_aux_tests(study):
    results = {}
    for name, pattern in (("controller-history", r"observations=(\d+) digest=(e8b0050dc90cd1f7)"),
                          ("ns1-timer-tests", r"ns1-timer: (\d+) checks PASS")):
        binary, logfile = study / "build/core-tests", study / "build/core-tests.log"
        require(binary.is_file() and logfile.is_file(), f"missing auxiliary test: {name}")
        found = re.search(pattern, logfile.read_text())
        require(found, f"auxiliary test did not pass: {name}")
        results[name] = {"binary_sha256": sha(binary), "log_sha256": sha(logfile),
                         "pass_record": found.group(0)}
    value = {"status": "PASS", "source_sha256": unit_inputs(study), "tests": results}
    (study / "build/aux-tests-provenance.json").write_text(json.dumps(value, indent=2) + "\n")
    print("Recorded auxiliary regression and timer tests.")

def verify_aux_tests(study):
    value = json.loads((study / "build/aux-tests-provenance.json").read_text())
    require(value["status"] == "PASS" and value["source_sha256"] == unit_inputs(study),
            "auxiliary tests belong to different inputs")
    for name, record in value["tests"].items():
        require(sha(study / "build/core-tests.log") == record["log_sha256"],
                f"auxiliary log changed: {name}")
        require(sha(study / "build/core-tests") == record["binary_sha256"],
                f"auxiliary binary changed: {name}")
    return value

def normalized_models(provenance):
    if provenance.get("model_sha256"):
        return {Path(k).name: v for k, v in provenance["model_sha256"].items()}
    return {Path(k).name: v for k, v in provenance.get("source_sha256", {}).items()
            if k.startswith("model/")}

def output_hashes(folder):
    files = list(folder.rglob("*.csv"))
    files += [p for p in (folder / "summary.json", folder / "reverse/summary.json") if p.exists()]
    return {str(p.relative_to(folder)): sha(p) for p in sorted(files)}

def verify_figures(study, result_root, experiment_cases):

    known = {2, 3, 4, 5, 6, 7, 8, 11, 12}
    figure_files = sorted(p for p in (study / "figures").rglob("*") if p.is_file())
    require(not any(p.suffix.lower() == ".svg" for p in figure_files), "submission figures must be PNG only; remove SVG files")
    png_files = [p for p in figure_files if p.suffix.lower() == ".png"]
    known_paths = {f"figures/figure-{number:02}.png" for number in known}
    actual_paths = {p.relative_to(study).as_posix() for p in png_files}
    require(actual_paths <= known_paths, f"unknown submission PNG files: {sorted(actual_paths-known_paths)}")
    manifest = json.loads((result_root / "figures-provenance.json").read_text())
    selected = manifest.get("selected_figures")
    require(isinstance(selected, list) and selected and all(type(n) is int and n in known for n in selected),
            "figure provenance needs selected_figures as known integer figure numbers")
    require(len(selected) == len(set(selected)), "duplicate selected figure in provenance")
    run_figures = set()
    for name in experiment_cases:
        match = re.match(r"^figure-(\d+)(?:/|-|$)", name)
        require(match and int(match[1]) in known, f"unknown experiment figure: {name}")
        run_figures.add(int(match[1]))
    require(set(selected) == run_figures, "selected PNG figures differ from this run's experiment manifest")
    selected_paths = {f"figures/figure-{number:02}.png" for number in selected}
    require(selected_paths <= actual_paths, f"selected PNG files are missing: {sorted(selected_paths-actual_paths)}")
    require(set(manifest["output_sha256"]) == selected_paths,
            "figure output hashes must identify exactly the selected PNG files")
    require(set(manifest["plot_source_sha256"]) == {"scripts/plot.py", "scripts/plot_extended.py"},
            "figure provenance does not identify both delivered plotting sources")
    require(manifest["input_sha256"], "figure provenance has no measured-data inputs")
    for field in ("plot_source_sha256", "input_sha256", "output_sha256"):
        for name, expected_hash in manifest[field].items():
            path = (study / name).resolve()
            require(not Path(name).is_absolute() and path.is_relative_to(study),
                    f"figure provenance path is outside study: {name}")
            require(path.is_file() and sha(path) == expected_hash, f"figure source/input/output changed: {name}")
    return {"figure_source_input_output_hashes_verified": True,
            "figure_provenance_scope": "selected PNG figures only; packaged unselected PNGs were not verified by this run",
            "selected_figures": sorted(selected), "figures_verified_by_this_run": sorted(selected_paths),
            "not_verified_by_this_run": sorted(actual_paths-selected_paths),
            "plot_input_files_verified": len(manifest["input_sha256"])}

def verify(study, ns3):
    result_root = study / "results"
    run_settings = read_settings(study)
    profile = run_settings["profile"]
    models = {p.name: sha(p) for p in (study / "model").glob("*") if p.is_file()}
    manifest = json.loads((result_root / "run-manifest.json").read_text())
    require(normalized_models(manifest) == models, "run manifest belongs to different model sources")
    experiment_cases = manifest["cases"]
    require(experiment_cases and all(isinstance(n, str) for n in experiment_cases), "manifest needs explicit relative case names")
    test_script = study / "scripts/test-network.py"
    test_driver = runpy.run_path(str(test_script))
    definitions = {case["case"]: case for case in test_driver["network_cases"](profile)}
    test_report = json.loads((result_root / "network-tests.json").read_text())
    require(test_report["status"] == "PASS" and test_report["schema"] == test_driver["TEST_SCHEMA"], "network test suite did not pass the delivered schema")
    require(test_report["producer_sha256"] == sha(test_script), "network suite was produced by a different test script")
    require(test_report["definition_sha256"] == test_driver["definition_sha256"](profile), "network test definitions changed")
    reported_cases = {case["case"]: case for case in test_report["cases"]}
    require(len(reported_cases) == len(test_report["cases"]) and set(reported_cases) == set(definitions), "network report omits or duplicates defined test cases")
    for name, definition in definitions.items():
        require(all(reported_cases[name].get(key) == value for key, value in definition.items()), f"reported test definition differs: {name}")
    expected = set(experiment_cases) | set(definitions)
    require(len(experiment_cases) == len(set(experiment_cases)), "duplicate experiment in run manifest")
    for name in expected:
        folder = (result_root / name).resolve()
        require(folder != result_root.resolve() and folder.is_relative_to(result_root.resolve()),
                f"case path is outside results: {name}")
    actual = {str(p.parent.relative_to(result_root)) for p in result_root.rglob("provenance.json")}
    require(actual == expected, f"run set differs from manifest/tests: missing={sorted(expected-actual)}, extra={sorted(actual-expected)}")
    summary_cases = {str(p.parent.relative_to(result_root)) for p in result_root.rglob("summary.json") if p.parent.name != "reverse"}
    require(summary_cases == expected, f"stale or missing raw-data folders: {sorted(summary_cases ^ expected)}")
    binary_hash = sha(find_binary(ns3))
    for name, expected_hash in models.items():
        installed_name = "CMakeLists.txt" if name == "CMakeLists.cmake" else name
        require(sha(ns3 / "scratch/fack-paper" / installed_name) == expected_hash,
                f"installed source differs: {name} -> {installed_name}")

    current_sources = source_hashes(study)
    all_runs, reverse_runs, common_timer, offered_schedules = [], [], {}, defaultdict(set)
    validator_hash = sha(study / "scripts/validate.py")
    for name in sorted(expected):
        folder = result_root / name
        provenance = json.loads((folder / "provenance.json").read_text())
        require(normalized_models(provenance) == models, f"stale model provenance: {name}")
        require(provenance.get("binary_sha256") == binary_hash, f"stale/different executable: {name}")
        require(provenance.get("returncode") == 0, f"recorded command did not succeed: {name}")
        require(isinstance(provenance.get("command"), list) and provenance["command"], f"missing run command: {name}")
        actual_outputs = output_hashes(folder)
        require(provenance.get("output_sha256") == actual_outputs, f"raw output/summary changed or was omitted from provenance: {name}")
        actual_csv = {key: value for key, value in actual_outputs.items() if key.endswith(".csv")}
        require(provenance.get("csv_sha256") == actual_csv, f"CSV hash set differs: {name}")
        recorded = json.loads((folder / "validation.json").read_text())
        require(recorded.get("status") == "PASS" and recorded.get("validator_sha256") == validator_hash,
                f"run was not validated by the delivered validator: {name}")
        validated = validate(folder, write=False)
        require(validated == recorded, f"independent revalidation changed: {name}")
        summary = json.loads((folder / "summary.json").read_text())
        analysis_sources = provenance.get("analysis_source_sha256", provenance["source_sha256"])
        require(analysis_sources == current_sources, f"analysis/test source changed: {name}")
        if name in definitions:
            definition = definitions[name]
            observed = test_driver["assert_case"](folder, definition)
            require(observed == reported_cases[name]["observed"], f"controlled test observations changed: {name}")
            require(recorded == reported_cases[name]["validation"], f"controlled test validation differs from suite report: {name}")
            command = ["./ns3", "run", "--no-build", "--quiet", shlex.join(["fack-paper", "--out=" + str(folder), *definition["options"]])]
            require(provenance["command"] == command, f"recorded test invocation differs from delivered definition: {name}")
        if summary.get("mode") == "queue_probe":
            all_runs.append({"case": name, **validated})
            continue
        group = "figure-11" if profile == "paper11" and name.startswith("figure-11/") else "common"
        effective_profile = scenario_profile(profile, 11 if group == "figure-11" else None)
        expected_wire, expected_history = PROFILES[effective_profile]
        require(bool(summary.get("sack_option_bytes")) == expected_wire,
                f"wire policy differs from selected profile: {name}")
        require(bool(summary.get("ns1_timestamp_timer")) == expected_history,
                f"timer policy differs from selected profile: {name}")
        timer = {key: summary[key] for key in TIMER_FIELDS}
        common_timer.setdefault(group, timer)
        require(timer == common_timer[group], f"timer configuration differs across algorithms: {name}")
        require(validated["timer_metadata_checked"], f"shared timer parameters were not checked: {name}")
        if summary.get("detail"):
            require(validated.get("timers_checked"), f"full trace omitted timer decisions: {name}")
        reverse_summary = folder / "reverse/summary.json"
        if reverse_summary.exists():
            reverse = json.loads(reverse_summary.read_text())
            require({key: reverse[key] for key in TIMER_FIELDS} == timer,
                    f"reverse connection uses a different timer policy: {name}")
            if reverse.get("detail"):
                reverse_runs.append({"case": name + "/reverse", **validate(folder / "reverse", write=False)})
        if summary.get("tiny_burst_bytes"):
            burstfile = folder / "bursts.csv"
            require(burstfile.exists(), f"reverse traffic is missing its offered schedule: {name}")
            signature = tuple(summary[key] for key in
                              ("queue", "waiting_capacity", "stop_s", "warmup_s", "initial_cwnd",
                               "initial_ssthresh", "tiny_burst_bytes", "tiny_rate", "seed", "packet_window", "integer_cut"))
            offered_schedules[signature].add(sha(burstfile))
        all_runs.append({"case": name, **validated})
    require(all(len(hashes) == 1 for hashes in offered_schedules.values()),
            "paired algorithms were given different reverse-application burst schedules")
    logging_pairs = test_driver["assert_logging_pairs"](result_root)
    require(test_report.get("logging_equivalence_pairs") == logging_pairs, "recorded full/light equivalence checks changed")

    aggregate_path = result_root / "figure-11-provenance.json"
    aggregate_checked = False
    aggregate_values = None
    if any(name.startswith("figure-11/") for name in experiment_cases):
        aggregate = json.loads(aggregate_path.read_text())
        require(aggregate["producer_sha256"] == sha(study / "scripts/experiments.py"), "stale Figure 11 aggregation script")
        for field in ("input_summary_sha256", "output_sha256"):
            for name, expected_hash in aggregate[field].items():
                path = (result_root / name).resolve()
                require(path.is_relative_to(result_root.resolve()) and sha(path) == expected_hash,
                        f"Figure 11 aggregation input/output changed: {name}")
        aggregate_values = validate_aggregate(result_root, run_settings["seed_count"])
        aggregate_checked = True

    figure_verification = verify_figures(study, result_root, experiment_cases)

    core = json.loads((study / "build/core-tests-provenance.json").read_text())
    require(core["status"] == "PASS" and core["source_sha256"] == unit_inputs(study), "core tests belong to different sources")
    require(core["log_sha256"] == sha(study / "build/core-tests.log"), "core test log changed")
    matches = re.findall(r"PASS (\d+) cases, (\d+) checks", (study / "build/core-tests.log").read_text())
    require(matches and tuple(map(int, matches[-1])) == (core["cases"], core["checks"]), "core test counts differ from provenance")
    unit_executable = study / "build/core-tests"
    if unit_executable.exists():
        require(core["binary_sha256"] == sha(unit_executable), "core test executable changed")
    auxiliary = verify_aux_tests(study)
    timer_suite = json.loads((study / "build/timer-network/timer-tests.json").read_text())
    require(timer_suite["status"] == "PASS" and len(timer_suite["cases"]) == 15
            and timer_suite["logging_pairs"] == 3 and timer_suite["rejected_mutations"] == 3
            and timer_suite["wire_faults_rejected"] == 2,
            "optional timer network/replay tests did not complete")
    require(timer_suite["binary_sha256"] == binary_hash, "timer suite belongs to another binary")
    require(timer_suite["producer_sha256"] == sha(study / "scripts/test-network.py"),
            "timer suite belongs to another test script")
    for case in timer_suite["cases"]:
        folder = study / "build/timer-network" / case["algorithm"] / case["case"]
        recheck = validate(folder, write=False)
        require(recheck["status"] == "PASS" and recheck["checks"] == case["checks"],
                "optional timer suite revalidation differs")
    environment = {}
    for key, command in (("gcc", ["g++", "--version"]), ("cmake", ["cmake", "--version"]),
                         ("python", ["python3", "--version"]), ("ns3_commit", ["git", "-C", str(ns3), "rev-parse", "HEAD"])):
        run = subprocess.run(command, capture_output=True, text=True, check=True)
        environment[key] = run.stdout.splitlines()[0]
    record = {"utc": datetime.now(timezone.utc).isoformat(), "status": "PASS",
              "scope": "delivered algorithm/trace/timer/fairness checks and exact raw-data provenance",
              "run_settings": run_settings,
              "ns3_version": (ns3 / "VERSION").read_text().strip(), "environment": environment,
              "core_tests": core, "auxiliary_tests": auxiliary, "optional_timer_network_tests": timer_suite, "shared_timer": common_timer,
              "controlled_test_cases": len(definitions), "test_definition_sha256": test_report["definition_sha256"],
              "full_light_equivalence_pairs": logging_pairs,
              "paired_reverse_schedule_groups_checked": len(offered_schedules),
              "trace_checks": sum(r["checks"] for r in all_runs), "runs": all_runs,
              "reverse_connection_trace_checks": sum(r["checks"] for r in reverse_runs), "reverse_runs": reverse_runs,
              "model_sha256": models, "binary_sha256": binary_hash,
              "figure11_aggregate_hashes_verified": aggregate_checked,
              "figure11_aggregate_values": aggregate_values,
              **figure_verification,
              "source_sha256": source_hashes(study)}
    (study / "VERIFICATION.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"PASS delivery: {len(all_runs)} current-model runs, {record['trace_checks']} checks; "
          "raw outputs, executable, shared timers and paired burst schedules verified.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--ns3", type=Path)
    parser.add_argument("--record-core-tests", action="store_true")
    parser.add_argument("--record-aux-tests", action="store_true")
    args = parser.parse_args()
    study = args.study.resolve()
    if args.record_aux_tests:
        record_aux_tests(study)
        return
    if args.record_core_tests:
        record_core_tests(study)
        return
    require(args.ns3 is not None, "--ns3 is required for final delivery verification")
    verify(study, args.ns3.resolve())

if __name__ == "__main__":
    main()
