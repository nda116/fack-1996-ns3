#!/usr/bin/env python3

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import statistics
import subprocess
import sys

from validate import validate
from ns3_paths import find_binary
from ns3_paths import DEFAULT_SEEDS, PROFILES, profile_options, record_settings, scenario_profile

ASSESSMENT_SCHEMA = 3

def figure11_cases(seed_count=DEFAULT_SEEDS):
    cases = []
    for burst in range(0, 6001, 1000):
        for algorithm in ("reno", "reno-sack", "fack"):
            for seed in range(1, seed_count + 1):
                options = [f"--algorithm={algorithm}", "--queue=20", "--stop=120",
                           "--warmup=20", f"--detail={'true' if seed == 1 else 'false'}", f"--tinyBurstBytes={burst}",
                           "--tinyRate=2", f"--seed={seed}"]
                if algorithm == "fack":
                    options += ["--overdamping=true", "--rampdown=true"]
                cases.append((f"figure-11/burst{burst}/{algorithm}/seed{seed}", options))
    return cases

BASE_CASES = {
    2: [("figure-02", ["--algorithm=reno"])],
    3: [("figure-03", ["--algorithm=reno-sack"])],
    4: [("figure-04", ["--algorithm=fack"])],
    5: [("figure-05/reno-sack", ["--algorithm=reno-sack", "--queue=10", "--ssthresh=35", "--stop=1.5"]),
        ("figure-05/fack", ["--algorithm=fack", "--queue=10", "--ssthresh=35", "--stop=1.5"])],
    6: [("figure-06", ["--algorithm=reno-sack", "--queue=17", "--stop=2", "--sackStarvationFallback=true", "--extraDrops=73,75,77,79"])],
    7: [("figure-07", ["--algorithm=fack", "--overdamping=true"])],
    8: [("figure-08-without", ["--algorithm=fack", "--overdamping=true", "--queue=6", "--stop=2"]),
        ("figure-08-with", ["--algorithm=fack", "--overdamping=true", "--rampdown=true", "--queue=6", "--stop=2"])],
    11: figure11_cases(),
    12: [("figure-12/reno", ["--algorithm=reno", "--queue=10", "--tinyBurstBytes=6000", "--tinyRate=2", "--seed=1", "--stop=15"]),
         ("figure-12/fack", ["--algorithm=fack", "--overdamping=true", "--rampdown=true", "--queue=10", "--tinyBurstBytes=6000", "--tinyRate=2", "--seed=1", "--stop=15"])],
}
SEED_COUNT = DEFAULT_SEEDS

def make_cases(seed_count=DEFAULT_SEEDS, profile="paper11"):
    result = {number: [(name, list(options)) for name, options in values]
              for number, values in BASE_CASES.items()}
    result[11] = figure11_cases(seed_count)
    return {number: [(name, options + profile_options(scenario_profile(profile, number))) for name, options in values]
            for number, values in result.items()}

CASES = make_cases()

GENERATED = ("events.csv", "network.csv", "delivery.csv", "acks.csv", "receiver.csv",
             "link-events.csv", "link-busy.csv", "bursts.csv", "timers.csv", "summary.json",
             "validation.json", "provenance.json", "run.log", "timer-profile.csv")

def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def utc():
    return datetime.now(timezone.utc).isoformat()

def source_hashes(study):
    return {str(f.relative_to(study)): sha256(f)
            for directory in ("model", "tests", "scripts")
            if (study / directory).exists()
            for f in sorted((study / directory).glob("*")) if f.is_file()}

def output_hashes(folder):
    files = list(folder.rglob("*.csv"))
    files += [f for f in (folder / "summary.json", folder / "reverse/summary.json") if f.exists()]
    return {str(f.relative_to(folder)): sha256(f) for f in sorted(files)}

def reusable(folder, command, sources, binary_hash):

    try:
        p = json.loads((folder / "provenance.json").read_text())
        models = {k: v for k, v in sources.items() if k.startswith("model/")}
        old_models = {k: v for k, v in p["source_sha256"].items() if k.startswith("model/")}
        if p["command"] != command or models != old_models or p.get("returncode", 0) != 0:
            return False
        if p.get("binary_sha256") != binary_hash:
            return False
        expected = p.get("output_sha256", {})
        if not expected or not (folder / "summary.json").exists():
            return False
        return expected == output_hashes(folder)
    except (OSError, KeyError, ValueError, TypeError):
        return False

def clear_generated(folder, root):

    resolved, base = folder.resolve(), root.resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise ValueError(f"case output outside results directory: {resolved}")
    for directory in (folder, folder / "reverse"):
        for name in GENERATED:
            f = directory / name
            if f.is_file():
                f.unlink()

def run_case(a, root, name, options, sources, binary_hash):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    simulation = ["fack-paper", "--packetWindow=true", "--integerCut=false", "--out=" + str(folder), *options]
    command = ["./ns3", "run", "--no-build", "--quiet", shlex.join(simulation)]
    reused = a.reuse and reusable(folder, command, sources, binary_hash)
    if not reused:
        clear_generated(folder, root)
        completed = subprocess.run(command, cwd=a.ns3, capture_output=True, text=True)
        (folder / "run.log").write_text(completed.stdout + completed.stderr)
        provenance = {"utc": utc(), "ns3_root": str(a.ns3), "ns3_version": a.version,
                      "command": command, "returncode": completed.returncode,
                      "binary_sha256": binary_hash, "source_sha256": sources,
                      "output_sha256": output_hashes(folder)}
        (folder / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        completed.check_returncode()
    else:
        provenance = json.loads((folder / "provenance.json").read_text())
    try:
        result = validate(folder)
    except Exception as error:
        provenance["validation"] = {"status": "FAIL", "error": str(error)}
        (folder / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        raise
    provenance["validation"] = result
    provenance["last_validated_utc"] = utc()
    provenance["analysis_source_sha256"] = sources
    provenance["csv_sha256"] = {str(f.relative_to(folder)): sha256(f) for f in sorted(folder.rglob("*.csv"))}
    provenance["output_sha256"] = output_hashes(folder)
    (folder / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    summary = json.loads((folder / "summary.json").read_text())
    return name, summary, bool(reused)

def aggregate_figure11(root):
    per_seed = []
    for name, _ in CASES[11]:
        s = json.loads((root / name / "summary.json").read_text())
        per_seed.append({"algorithm": s["algorithm"], "burst_bytes": s["tiny_burst_bytes"],
                         "seed": s["seed"], "reverse_percent": 100 * s["reverse_utilization"],
                         "forward_percent": 100 * s["forward_utilization"],
                         "forward_goodput_mbps": s["goodput_mbps"],
                         "timeouts": s["timeouts"], "duplicate_segments": s["duplicate_segments"],
                         "forced_timeouts": s["forced_timeouts"], "folder": name})
    with (root / "figure-11-seeds.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(per_seed[0]))
        writer.writeheader(); writer.writerows(per_seed)
    aggregate = []
    for algorithm in ("reno", "reno-sack", "fack"):
        for burst in range(0, 6001, 1000):
            group = [r for r in per_seed if r["algorithm"] == algorithm and r["burst_bytes"] == burst]
            if {r["seed"] for r in group} != set(range(1, SEED_COUNT + 1)) or len(group) != SEED_COUNT:
                raise ValueError(f"incomplete Figure 11 group: {algorithm}, {burst}")
            forward = [r["forward_percent"] for r in group]
            goodput = [r["forward_goodput_mbps"] for r in group]
            aggregate.append({"algorithm": algorithm, "burst_bytes": burst,
                              "reverse_percent": statistics.mean(r["reverse_percent"] for r in group),
                              "forward_percent": statistics.mean(forward),
                              "forward_std_percent": statistics.stdev(forward),
                              "forward_goodput_mbps": statistics.mean(goodput),
                              "goodput_std_mbps": statistics.stdev(goodput), "seeds": len(group)})
    with (root / "figure-11-data.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(aggregate[0]))
        writer.writeheader(); writer.writerows(aggregate)
    (root / "figure-11-provenance.json").write_text(json.dumps({
        "utc": utc(), "producer_sha256": sha256(__file__),
        "calculation": "arithmetic mean of measured seed values; sample standard deviation (ddof=1)",
        "input_summary_sha256": {name + "/summary.json": sha256(root / name / "summary.json") for name, _ in CASES[11]},
        "output_sha256": {name: sha256(root / name) for name in ("figure-11-data.csv", "figure-11-seeds.csv")},
    }, indent=2) + "\n")
    return aggregate

def assess(root, number):

    names = [name for name, _ in CASES[number]]
    evidence = [{"case": name, **json.loads((root / name / "summary.json").read_text())} for name in names]
    fields = ("queue", "waiting_capacity", "stop_s", "warmup_s", "initial_cwnd", "initial_ssthresh",
              "packet_window", "integer_cut", "receiver_window", "timer_policy", "initial_rto_s",
              "min_rto_s", "max_rto_s", "timer_tick_s", "tiny_rate")

    for item in evidence[1:]:
        for key in fields:
            if item[key] != evidence[0][key]:
                raise ValueError(f"Figure {number}: inconsistent comparison parameter {key}")
    for name in names:
        if json.loads((root / name / "validation.json").read_text())["status"] != "PASS":
            raise ValueError(f"Figure {number}: unvalidated input {name}")
    if number in (11, 12):
        groups = [(burst, seed, [f"figure-11/burst{burst}/{alg}/seed{seed}" for alg in ("reno", "reno-sack", "fack")])
                  for burst in range(1000, 6001, 1000) for seed in range(1, SEED_COUNT + 1)] if number == 11 else [(6000, 1, names)]
        for burst, seed, group in groups:
            if len({(root / name / "bursts.csv").read_bytes() for name in group}) != 1:
                raise ValueError(f"Figure {number}: reverse offers differ at burst={burst}, seed={seed}")
    if number == 6 and evidence[0]["injected_drops"] != 4:
        raise ValueError("Figure 6: not all four configured original-copy losses occurred")
    result = {"assessment_schema": ASSESSMENT_SCHEMA, "decision": "VALIDATED", "cases": names,
              "issues": [], "evidence": evidence,
              "meaning": "Passed implemented invariant/trace/provenance checks; not a formal proof or historical curve match."}
    if number == 11:
        result["evidence"] = aggregate_figure11(root)
    return result

def write_comparison(root, comparison):
    (root / "comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")
    cases = [name for number in sorted(comparison, key=int) for name in comparison[number]["cases"]]
    (root / "run-manifest.json").write_text(json.dumps({"cases": cases, "figures": sorted(map(int, comparison)),
        "model_sha256": {f.name: sha256(f) for f in sorted((root.parent / "model").glob("*")) if f.is_file()},
        "purpose": "Correct algorithm mechanisms and controlled comparisons, not historical curve matching"}, indent=2) + "\n")
    text = ["# Kết quả mô phỏng đã kiểm tra", "",
            "Mục tiêu: kiểm tra cơ chế FACK và so sánh với classic Reno/Reno+SACK trong mô hình được công bố. "
            "Số figure chỉ ánh xạ kịch bản của bài báo. Không đặt ngưỡng để ép đường đo giống hình gốc.", "",
            "VALIDATED nghĩa là kiểm tra đã cài đặt đều qua; không phải chứng minh hình thức hoặc chứng nhận TCP stack đầy đủ.", "",
            "| Figure | Lượt chạy | Segment nhận đúng thứ tự / RTO hết hạn / forced restart / segment trùng |", "|---|---:|---|"]
    for number, item in sorted(comparison.items(), key=lambda x: int(x[0])):
        if number == "11":
            last = [r for r in item["evidence"] if r["burst_bytes"] == 6000]
            counts = "; ".join(f"{r['algorithm']}: busy {r['forward_percent']:.3f}% ± SD {r['forward_std_percent']:.3f}%; goodput {r['forward_goodput_mbps']:.4f} ± SD {r['goodput_std_mbps']:.4f} Mb/s" for r in last)
        else:
            counts = "; ".join(f"{r['case']}: {r['delivered_segments']} / {r['rto_expiries']} / {r['forced_timeouts']} / {r['duplicate_segments']}" for r in item["evidence"])
        text.append(f"| {number} | {len(item['cases'])} | {counts} |")
    text += ["", "## Cách đọc", "",
             "- Các bộ đếm lấy từ executable; hàng Figure 11 trình bày điểm burst 6 kB, mean và sample SD của các seed. SD không phải khoảng tin cậy.",
             "- RTO expiry và forced restart được đếm riêng. Chỉ timer hết hạn tăng backoff; forced restart giữ backoff. Duplicate và restart là kết quả thực đo.",
             "- Cùng topology, serialization size, queue, receiver window, cấu hình timer và lịch ứng dụng trong từng phép so sánh. Các mở rộng FACK được ghi rõ.",
             "- Reno là classic Reno; Reno+SACK là ACK-counted Sack1 được mô tả trong báo cáo, không gọi là NewReno hoặc RFC 6675.",
             "- Figure 6 bật riêng fallback starvation tái dựng; nó không bật trong baseline Reno+SACK của các phép so sánh khác.",
             "- Figure 9 không triển khai vì chưa xác định đủ baseline của lost opportunity; không dùng dữ liệu tự tạo để thay thế.",
             "- Chi tiết mô hình, giả định và cách kiểm tra nằm trong báo cáo đi kèm. Mọi ảnh đo nằm ở figures/.", ""]
    (root.parent / "COMPARISON.md").write_text("\n".join(text), encoding="utf-8")

def main():
    global CASES, SEED_COUNT
    p = argparse.ArgumentParser()
    p.add_argument("--ns3", type=Path, required=True)
    p.add_argument("--study", type=Path, required=True)
    p.add_argument("--figures", choices=["all", *map(str, CASES)], default="all")
    p.add_argument("--profile", choices=PROFILES, default="paper11")
    p.add_argument("--seed-count", type=int, default=DEFAULT_SEEDS)
    p.add_argument("--jobs", type=int, default=2, help="parallel simulations for Figure 11 only")
    p.add_argument("--reuse", action="store_true", help="reuse cases whose model/binary/command/output hashes match")
    a = p.parse_args()
    if a.jobs < 1: p.error("--jobs must be positive")
    a.ns3, a.study = a.ns3.resolve(), a.study.resolve()
    a.version = (a.ns3 / "VERSION").read_text().strip()
    binary = find_binary(a.ns3)
    if not binary.is_file(): p.error(f"build fack-paper first; executable missing: {binary}")
    binary_hash = sha256(binary)
    root = a.study / "results"; root.mkdir(exist_ok=True)
    selected = list(CASES) if a.figures == "all" else [int(a.figures)]
    record_settings(a.study, a.profile, a.seed_count)
    SEED_COUNT = a.seed_count
    CASES = make_cases(a.seed_count, a.profile)
    sources = source_hashes(a.study)
    models = {k: v for k, v in sources.items() if k.startswith("model/")}
    script_hash = sha256(__file__)
    old = json.loads((root / "comparison.json").read_text()) if (root / "comparison.json").exists() else {}

    comparison = {k: v for k, v in old.items()
                  if v.get("assessment_schema") == ASSESSMENT_SCHEMA
                  and v.get("model_sha256") == models
                  and v.get("binary_sha256") == binary_hash}
    for number in selected:
        cases = CASES[number]
        print(f"Figure {number}: {len(cases)} simulation(s)", flush=True)
        if number == 11:
            with ThreadPoolExecutor(max_workers=a.jobs) as pool:
                futures = [pool.submit(run_case, a, root, name, options, sources, binary_hash) for name, options in cases]
                try:
                    for index, future in enumerate(as_completed(futures), 1):
                        name, summary, reused = future.result()
                        print(f"[{index}/{len(cases)}] {'Reused' if reused else 'Ran'} {name}: "
                              f"forward={100*summary['forward_utilization']:.3f}% "
                              f"reverse={100*summary['reverse_utilization']:.3f}%", flush=True)
                except Exception:
                    for future in futures: future.cancel()
                    raise
        else:
            for name, options in cases:
                name, summary, reused = run_case(a, root, name, options, sources, binary_hash)
                print(f"{'Reused' if reused else 'Ran'} {name}: delivered={summary['delivered_segments']} "
                      f"duplicates={summary['duplicate_segments']} timeouts={summary['timeouts']}", flush=True)
        comparison[str(number)] = assess(root, number)
        comparison[str(number)].update({"model_sha256": models,
                                        "assessment_script_sha256": script_hash,
                                        "binary_sha256": binary_hash})
        write_comparison(root, comparison)
        print(f"Figure {number}: {comparison[str(number)]['decision']}", flush=True)
    for number in list(comparison):
        if int(number) in selected:
            continue
        for name, options in CASES[int(number)]:
            simulation = ["fack-paper", "--packetWindow=true", "--integerCut=false", "--out=" + str(root / name), *options]
            command = ["./ns3", "run", "--no-build", "--quiet", shlex.join(simulation)]
            if not reusable(root / name, command, sources, binary_hash):
                raise ValueError(f"Existing case {name} is stale; run all after changing model or raw data")
            validate(root / name)
        comparison[number] = assess(root, int(number))
        comparison[number].update({"model_sha256": models, "assessment_script_sha256": script_hash,
                                   "binary_sha256": binary_hash})
    write_comparison(root, comparison)
    subprocess.run([sys.executable, str(a.study / "scripts/plot.py"), "--results", str(root), "--output", str(a.study)], check=True)
    print("Measured CSV and assessment:", root)
    print("Comparison:", a.study / "COMPARISON.md")

if __name__ == "__main__": main()
