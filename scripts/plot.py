#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import os
import textwrap
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

COLORS = {2: "#b33e37", 3: "#d47b17", 4: "#2166ac", 7: "#7951a0", 8: "#2166ac"}

PAPER_CAPTIONS = {
    2: "Figure 2: Reno behavior during Slow-start.",
    3: "Figure 3: Reno+SACK behavior during Slow-start.",
    4: "Figure 4: FACK behavior during Slow-start.",
    5: "Figure 5: SACK and FACK loss recovery details.",
    6: "Figure 6: SACK recovery detail under greater than 1/2 window of loss.",
    7: "Figure 7: Behavior of FACK with Overdamping.",
    8: "Figure 8: FACK behavior with (bottom) and without (top) Rampdown. Overdamping is utilized in both cases.",
    11: "Figure 11: Comparison of FACK, Reno, and Reno+SACK",
    12: "Figure 12: Reno and Fack with jitter",
}
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.linewidth": .7})

def paper_caption(fig, number):
    width = 62 if fig.get_figwidth() <= 6 else 100
    title = textwrap.fill(PAPER_CAPTIONS[number], width=width,
                          break_long_words=False, break_on_hyphens=False)

    fig.supxlabel(title, fontsize=11)

def load(folder, name):
    with (folder / name).open(newline="") as f:
        return list(csv.DictReader(f))

def xy(rows, x, y, event=None):
    return np.array([(float(r[x]), float(r[y])) for r in rows if event is None or r["event"] == event]).reshape(-1, 2)

def style(ax, xlim, ylim):
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.tick_params(direction="out", length=3, width=.7, labelsize=8)

def segments(ax, data, xlim, ylim, color):
    for event, alpha in (("enqueue", .85), ("dequeue", .45)):
        d = xy(data, "time_s", "segment", event)
        if len(d): ax.plot(d[:, 0], d[:, 1], ".", color=color, ms=1.4, alpha=alpha, mew=0)
    d = xy(data, "time_s", "segment", "drop")
    if len(d): ax.plot(d[:, 0], d[:, 1], "x", color="#202020", ms=3.5, mew=.6)
    style(ax, xlim, ylim)

def save(fig, folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    fig.savefig(folder / f"{name}.png", dpi=200, facecolor="white")
    plt.close(fig)

def upper_bound(values, minimum, step):

    values = np.asarray(values, dtype=float)
    if not len(values):
        return minimum
    if not np.all(np.isfinite(values)):
        raise ValueError("cannot plot non-finite measured values")
    maximum = float(np.max(values))
    return minimum if maximum <= minimum else float(np.ceil(maximum / step) * step)

def write_figures_provenance(results, output, selected, cases, artist_data_checks=None):

    study = results.resolve().parent
    source_directory = Path(__file__).resolve().parent
    inputs = set()
    for number in selected:
        if number == 11:
            inputs.add(results / "figure-11-data.csv")

            inputs.update(p for p in (results / "figure-11").rglob("summary.json")
                          if p.parent.name.startswith("seed"))
        else:
            inputs.update(results / folder / name for folder in cases[number]
                          for name in ("events.csv", "network.csv", "summary.json"))
    images = [output / "figures" / f"figure-{number:02}.png" for number in selected]
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    relative = lambda path: Path(os.path.relpath(path.resolve(), study)).as_posix()
    manifest = {
        "plot_source_sha256": {f"scripts/{name}": digest(source_directory / name)
                               for name in ("plot.py", "plot_extended.py")},
        "input_sha256": {relative(path): digest(path) for path in sorted(inputs)},
        "output_sha256": {relative(path): digest(path) for path in sorted(images)},
        "selected_figures": list(selected),
    }
    if artist_data_checks:
        manifest["artist_data_checks"] = artist_data_checks
    (results / "figures-provenance.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

def triple(root, destination, number):
    folder = root / f"figure-{number:02}"
    e, n = load(folder, "events.csv"), load(folder, "network.csv")
    summary = json.loads((folder / "summary.json").read_text())
    label = {2: "Reno", 3: "Reno+SACK", 4: "FACK", 7: "FACK + Overdamping"}[number]
    color = COLORS[number]
    fig = plt.figure(figsize=(5.4, 6.6), layout="constrained")
    paper_caption(fig, number)
    gs = fig.add_gridspec(3, 1, height_ratios=[1, 1.75, 1.25])
    a, b, c = [fig.add_subplot(gs[i]) for i in range(3)]
    d = xy(e, "time_s", "cwnd")
    a.plot(d[:, 0], d[:, 1], ".", color=color, ms=1.2, mew=0)
    duration = float(summary["stop_s"])
    window_max = upper_bound(d[:, 1], 66, 20)
    style(a, (0, duration), (0, window_max))
    a.set_yticks(np.arange(0, window_max + .1, 20))
    a.tick_params(labelbottom=False)
    a.text(.47, .65, label + " Window", transform=a.transAxes, fontsize=9)
    a.set_ylabel("segments")
    segment_data = xy(n, "time_s", "segment")
    segment_max = upper_bound(segment_data[:, 1], 700, 100)
    segments(b, n, (0, duration), (0, segment_max), color)
    b.set_yticks(np.arange(0, segment_max + .1, 100))
    b.text(.035, .88, label + " Segments", transform=b.transAxes)
    b.text(.76, .12, f"Q={summary['queue']}", transform=b.transAxes)
    b.set_ylabel("segment number")
    zoom = (0, 1) if number == 2 else (.4, 1.4)
    zoom_values = segment_data[(segment_data[:, 0] >= zoom[0]) & (segment_data[:, 0] <= zoom[1]), 1]
    zoom_max = upper_bound(zoom_values, 150 if number in (2, 7) else 200, 50)
    segments(c, n, zoom, (0, zoom_max), color)
    c.set_xticks(np.round(np.linspace(*zoom, 6), 2))
    c.set_yticks(np.arange(0, c.get_ylim()[1] + 1, 50))
    c.text(.035, .86, "Enlarged", transform=c.transAxes)
    c.set_xlabel("Time (seconds)")
    c.set_ylabel("segment number")
    save(fig, destination, f"figure-{number:02}")

def rampdown(root, destination):
    fig = plt.figure(figsize=(5.4, 7.3), layout="constrained")
    gs = fig.add_gridspec(4, 1, height_ratios=[.85, 1.4, .85, 1.4])
    measured = []
    for name in ("figure-08-without", "figure-08-with"):
        folder = root / name
        measured.append((load(folder, "events.csv"), load(folder, "network.csv"),
                         json.loads((folder / "summary.json").read_text())))
    window_max = upper_bound([float(r["effective_window"]) for e, _, _ in measured for r in e], 32, 10)
    segment_max = upper_bound([float(r["segment"]) for _, n, _ in measured for r in n], 225, 50)
    paper_caption(fig, 8)
    for row, name in enumerate(("figure-08-without", "figure-08-with")):
        e, n, summary = measured[row]
        a, b = fig.add_subplot(gs[row * 2]), fig.add_subplot(gs[row * 2 + 1])
        color = "#7951a0" if row == 0 else "#20784c"
        d = xy(e, "time_s", "effective_window")
        a.plot(d[:, 0], d[:, 1], ".", color=color, ms=1.2, mew=0)
        if row:
            base = xy(e, "time_s", "cwnd")
            a.plot(base[:, 0], base[:, 1], ".", color="#aaaaaa", ms=.6, alpha=.5)
            a.plot(d[:, 0], d[:, 1], ".", color=color, ms=1.2, mew=0)
        style(a, (0, summary["stop_s"]), (0, window_max))
        a.set_yticks(np.arange(0, window_max + .1, 10)); a.tick_params(labelbottom=False)
        a.text(.63, .77, "cwnd + wintrim", transform=a.transAxes, fontsize=8)
        a.set_ylabel("segments")
        segments(b, n, (0, summary["stop_s"]), (0, segment_max), color)
        b.text(.035, .87, "With Rampdown" if row else "Without Rampdown", transform=b.transAxes)
        b.text(.77, .12, f"Q={summary['queue']}", transform=b.transAxes)
        b.set_ylabel("segment number")
        if row: b.set_xlabel("Time (seconds)")
        else: b.tick_params(labelbottom=False)
    save(fig, destination, "figure-08")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--figures", default="all", help="all complete measured scenarios, or comma-separated numbers")
    a = p.parse_args()
    cases = {2: ("figure-02",), 3: ("figure-03",), 4: ("figure-04",),
             5: ("figure-05/reno-sack", "figure-05/fack"), 6: ("figure-06",),
             7: ("figure-07",), 8: ("figure-08-without", "figure-08-with"),
             11: (), 12: ("figure-12/reno", "figure-12/fack")}
    def missing(number):
        if number == 11:
            return [] if (a.results / "figure-11-data.csv").is_file() else ["figure-11-data.csv"]
        return [str(Path(folder) / name) for folder in cases[number]
                for name in ("events.csv", "network.csv", "summary.json")
                if not (a.results / folder / name).is_file()]
    if a.figures == "all":
        selected = [number for number in cases if not missing(number)]
    else:
        try:
            selected = list(dict.fromkeys(int(part) for part in a.figures.split(",")))
        except ValueError:
            p.error("--figures requires all or comma-separated figure numbers")
        if not selected or any(number not in cases for number in selected):
            p.error("supported figures: 2,3,4,5,6,7,8,11,12")
        for number in selected:
            absent = missing(number)
            if absent:
                p.error(f"Figure {number} measured inputs missing: {', '.join(absent)}")
    if not selected:
        p.error("no complete measured figure inputs found")
    target = a.output / "figures"
    artist_data_checks = {}
    for number in selected:
        if number == 8: rampdown(a.results, target)
        elif number in (2,3,4,7): triple(a.results, target, number)
        else:
            from plot_extended import figure5,figure6,figure11,figure12
            checks = {5:figure5,6:figure6,11:figure11,12:figure12}[number](a.results,target)
            if checks is not None:
                artist_data_checks[str(number)] = checks
        print(target / f"figure-{number:02}.png")
    write_figures_provenance(a.results, a.output, selected, cases, artist_data_checks)

if __name__ == "__main__": main()
