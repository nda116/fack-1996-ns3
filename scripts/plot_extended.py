#!/usr/bin/env python3

import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

RENO = "#b33e37"
SACK = "#cf7c20"
FACK = "#2166ac"
QUEUE = "#777777"
INJECTED = "#ce7918"

def _array_sha256(values):

    return hashlib.sha256(np.ascontiguousarray(values, dtype="<f8").tobytes()).hexdigest()

def _helpers():

    from plot import load, xy, style, segments, save
    return load, xy, style, segments, save

def _summary(folder):
    return json.loads((Path(folder) / "summary.json").read_text())

def _upper(values, minimum, step):
    from plot import upper_bound
    return upper_bound(values, minimum, step)

def _caption(fig, number):
    from plot import paper_caption
    paper_caption(fig, number)

def _window(ax, events, xy, style, color, xlim, ylim):
    data = xy(events, "time_s", "cwnd")
    if len(data):
        ax.plot(data[:, 0], data[:, 1], ".", color=color, ms=1.25, mew=0)
    style(ax, xlim, ylim)
    ax.tick_params(labelbottom=False)
    ax.set_ylabel("segments")

def figure5(root, destination):

    root, destination = Path(root), Path(destination)
    load, xy, style, segments, save = _helpers()
    fig = plt.figure(figsize=(5.6, 7.5), layout="constrained")
    gs = fig.add_gridspec(4, 1, height_ratios=[.8, 1.35, .8, 1.35])
    measured = []
    for name in ("reno-sack", "fack"):
        folder = root / "figure-05" / name
        measured.append((load(folder, "events.csv"), load(folder, "network.csv"), _summary(folder)))
    summaries = [s for _, _, s in measured]
    _caption(fig, 5)
    duration = max(float(s["stop_s"]) for s in summaries)
    window_max = _upper([float(r["cwnd"]) for e, _, _ in measured for r in e], 42, 10)
    segment_max = _upper([float(r["segment"]) for _, n, _ in measured for r in n], 150, 50)
    for row, (name, label, color) in enumerate((
            ("reno-sack", "Reno+SACK", SACK), ("fack", "FACK", FACK))):
        events, network, summary = measured[row]
        window, packet = fig.add_subplot(gs[2 * row]), fig.add_subplot(gs[2 * row + 1])
        _window(window, events, xy, style, color, (0, duration), (0, window_max))
        q = xy(network, "time_s", "waiting_packets")
        if len(q):
            window.plot(q[:, 0], q[:, 1], ".", color=QUEUE, ms=1.0, alpha=.75, mew=0)
        window.set_yticks(np.arange(0, window_max + .1, 10))
        window.text(.57, .79, "Congestion window", transform=window.transAxes, fontsize=8)
        window.text(.73, .17, "Waiting queue", color=QUEUE,
                    transform=window.transAxes, fontsize=8)
        segments(packet, network, (0, duration), (0, segment_max), color)
        packet.set_yticks(np.arange(0, segment_max + .1, 50))
        packet.text(.035, .86, label + " Segments", transform=packet.transAxes)
        packet.text(.78, .13, f"Q={summary['queue']}", transform=packet.transAxes)
        packet.set_ylabel("segment number")
        packet.set_xticks(np.arange(0, duration + .01, .25))
        if row:
            packet.set_xlabel("Time (seconds)")
        else:
            packet.tick_params(labelbottom=False)
    save(fig, destination, "figure-05")

def figure6(root, destination):

    root, destination = Path(root), Path(destination)
    load, xy, style, segments, save = _helpers()
    folder = root / "figure-06"
    events, network = load(folder, "events.csv"), load(folder, "network.csv")
    summary = _summary(folder)
    fig = plt.figure(figsize=(5.6, 4.6), layout="constrained")
    _caption(fig, 6)
    gs = fig.add_gridspec(2, 1, height_ratios=[1, 1.65])
    window, packet = (fig.add_subplot(gs[i]) for i in range(2))
    duration = float(summary["stop_s"])
    window_max = _upper([float(r["cwnd"]) for r in events], 66, 20)
    segment_max = _upper([float(r["segment"]) for r in network], 250, 50)
    _window(window, events, xy, style, SACK, (0, duration), (0, window_max))
    window.set_yticks(np.arange(0, window_max + .1, 20))
    window.text(.59, .74, "Reno+SACK Window", transform=window.transAxes, fontsize=8)
    segments(packet, network, (0, duration), (0, segment_max), SACK)
    extra = xy(network, "time_s", "segment", "injected-drop")
    if len(extra):
        packet.plot(extra[:, 0], extra[:, 1], "^", color=INJECTED,
                    markerfacecolor="none", ms=5, mew=.8)
    packet.text(.035, .89, "Reno+SACK Segments", transform=packet.transAxes)
    packet.text(.78, .12, f"Q={summary['queue']}", transform=packet.transAxes)
    packet.set_yticks(np.arange(0, segment_max + .1, 50))
    packet.set_xticks(np.arange(0, duration + .01, .5))
    packet.set_ylabel("segment number")
    packet.set_xlabel("Time (seconds)")
    save(fig, destination, "figure-06")

def figure11(root, destination):

    root, destination = Path(root), Path(destination)
    _, _, style, _, save = _helpers()
    with (root / "figure-11-data.csv").open(newline="") as f:
        data = list(csv.DictReader(f))
    if not data:
        raise ValueError("Figure 11 requires nonempty measured figure-11-data.csv")
    required = {"algorithm", "burst_bytes", "reverse_percent", "forward_percent", "forward_std_percent", "seeds"}
    missing = sorted(required - data[0].keys())
    if missing:
        raise ValueError("Figure 11 measured aggregate is missing: " + ", ".join(missing))
    algorithms = ("reno", "reno-sack", "fack")
    if {r["algorithm"] for r in data} != set(algorithms):
        raise ValueError("Figure 11 requires measured Reno, Reno+SACK and FACK groups")
    keys = [(r["algorithm"], int(r["burst_bytes"])) for r in data]
    if len(keys) != len(set(keys)):
        raise ValueError("Figure 11 contains duplicate algorithm/burst groups")
    bursts = [{int(r["burst_bytes"]) for r in data if r["algorithm"] == name} for name in algorithms]
    if any(group != bursts[0] for group in bursts[1:]):
        raise ValueError("Figure 11 algorithms must cover the same offered burst sizes")
    seed_counts = {int(r["seeds"]) for r in data}
    if len(seed_counts) != 1 or min(seed_counts) < 2:
        raise ValueError("Figure 11 sample SD requires a common seed count of at least two")
    seed_count = next(iter(seed_counts))
    fig, ax = plt.subplots(figsize=(6.0, 4.5), layout="constrained")
    queues = sorted({_summary(p.parent)["queue"] for p in (root / "figure-11").rglob("summary.json")
                     if p.parent.name.startswith("seed")})
    queue_label = " / ".join(str(q) for q in queues)
    _caption(fig, 11)
    if queues:
        ax.text(.97, .70, f"Q={queue_label}", ha="right", va="top", transform=ax.transAxes, fontsize=9)

    xmax, ymin, ymax = 7.0, 50.0, 102.0
    artist_checks = {}
    for name, label, color, marker in (
            ("reno", "Reno", RENO, "D"),
            ("reno-sack", "Reno+SACK", SACK, "^"),
            ("fack", "FACK", FACK, "s")):
        values = sorted((r for r in data if r["algorithm"] == name),
                        key=lambda r: float(r["reverse_percent"]))
        x = np.array([float(r["reverse_percent"]) for r in values])
        y = np.array([float(r["forward_percent"]) for r in values])
        err = np.array([float(r["forward_std_percent"]) for r in values])
        if (not np.all(np.isfinite([x, y, err])) or np.any(err < 0)
                or np.any(x < 0) or np.any(y < 0) or np.any(y > 100 + 1e-8)):
            raise ValueError(f"Figure 11 has invalid measured utilization values for {name}")
        drawn = ax.errorbar(x, y, yerr=err, color=color, marker=marker, ms=4.2,
                           markerfacecolor="white", markeredgewidth=.9,
                           linewidth=.9, elinewidth=.65, capsize=2, label=label,
                           clip_on=False)
        before_mean = np.column_stack((x, y))
        before_bounds = np.stack((np.column_stack((x, y - err)),
                                  np.column_stack((x, y + err))), axis=1)
        after_mean = np.asarray(drawn.lines[0].get_xydata())
        after_bounds = np.asarray(drawn.lines[2][0].get_segments())
        if not (np.array_equal(before_mean, after_mean) and
                np.array_equal(before_bounds, after_bounds)):
            raise AssertionError("Figure 11 artists changed measured mean/SD coordinates")
        before = {"mean_xy": _array_sha256(before_mean), "sd_endpoints": _array_sha256(before_bounds)}
        after = {"mean_xy": _array_sha256(after_mean), "sd_endpoints": _array_sha256(after_bounds)}
        artist_checks[name] = {"before_sha256": before, "after_sha256": after,
                               "unchanged": before == after, "points": len(values), "seeds": seed_count}
        xmax = max(xmax, float(np.ceil(np.max(x))))
        if np.min(y - err) < ymin:
            ymin = min(0.0, float(np.floor(np.min(y - err) / 10) * 10))
        if np.max(y + err) > ymax:
            ymax = float(np.ceil(np.max(y + err) / 10) * 10)
    style(ax, (0, xmax), (ymin, ymax))
    ax.set_xticks(np.arange(0, xmax + .1, 1))
    ax.set_yticks(np.arange(ymin, ymax + .1, 10))
    ax.set_xlabel("Reverse tiny-DATA utilization (%)")
    ax.set_ylabel("Forward bottleneck busy time (%)")
    ax.legend(loc="lower left", frameon=False, fontsize=8.5)
    ax.grid(axis="y", color="#dddddd", linewidth=.45, alpha=.55)
    save(fig, destination, "figure-11")
    return {"meaning": "CSV mean/SD coordinates equal rendered artist coordinates; no paper data substituted.",
            "axes": {"x": [0, xmax], "y": [ymin, ymax]}, "algorithms": artist_checks}

def figure12(root, destination):

    root, destination = Path(root), Path(destination)
    load, xy, style, segments, save = _helpers()
    fig = plt.figure(figsize=(9.0, 6.7), layout="constrained")
    gs = fig.add_gridspec(4, 1, height_ratios=[.8, 1.45, .8, 1.45])
    measured = []
    for name in ("reno", "fack"):
        folder = root / "figure-12" / name
        measured.append((load(folder, "events.csv"), load(folder, "network.csv"), _summary(folder)))
    summaries = [s for _, _, s in measured]
    _caption(fig, 12)
    duration = max(float(s["stop_s"]) for s in summaries)
    window_max = _upper([float(r[field]) for e, _, _ in measured for r in e
                         for field in ("cwnd", "effective_window")], 42, 20)
    segment_max = _upper([float(r["segment"]) for _, n, _ in measured for r in n], 2500, 500)
    for row, (name, label, color) in enumerate((
            ("reno", "Reno", RENO), ("fack", "FACK + Overdamping + Rampdown", FACK))):
        events, network, summary = measured[row]
        window, packet = fig.add_subplot(gs[2 * row]), fig.add_subplot(gs[2 * row + 1])
        _window(window, events, xy, style, color, (0, duration), (0, window_max))
        real, effective = xy(events, "time_s", "cwnd"), xy(events, "time_s", "effective_window")
        different = np.abs(effective[:, 1] - real[:, 1]) > 1e-9
        window.plot(effective[different, 0], effective[different, 1], ".", ms=.9,
                    color="#888888", alpha=.65, mew=0)
        window.set_yticks(np.arange(0, window_max + .1, 20))
        window.text(.50, .75, label + " cwnd",
                    transform=window.transAxes, fontsize=8)
        segments(packet, network, (0, duration), (0, segment_max), color)
        packet.set_yticks(np.arange(0, segment_max + .1, 500))
        packet.set_xticks(np.arange(0, duration + .1, 2))
        packet.text(.035, .86, label + " Segments", transform=packet.transAxes, fontsize=8)
        packet.text(.87, .12, f"Q={summary['queue']}", transform=packet.transAxes)
        packet.set_ylabel("segment number")
        if row:
            packet.set_xlabel("Time (seconds)")
        else:
            packet.tick_params(labelbottom=False)
    save(fig, destination, "figure-12")
