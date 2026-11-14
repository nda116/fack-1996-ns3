#!/usr/bin/env python3

import bisect
import csv
import hashlib
import json
import math
from collections import Counter, deque
from pathlib import Path

NS1_POLICY = "ns1-timestamp-fixedpoint-partial"

def validate_ns1_timer(folder, summary, check):
    folder = Path(folder)
    tick = float(summary["timer_tick_s"])
    check(bool(summary.get("ns1_timestamp_timer")), "historical policy flag is missing")
    check(summary["timer_policy"] == NS1_POLICY, "historical timer policy is mislabeled")
    check(summary.get("timer_idle_policy") == "cancel-fully-acked", "unknown historical idle policy")
    check(abs(summary["initial_rto_s"] - max(2*tick, int(3 / tick)*tick)) < 1e-9,
          "incorrect historical effective initial RTO")
    check(abs(summary["min_rto_s"] - 2 * tick) < 1e-9 and summary["max_rto_s"] == 2000,
          "historical floor/overflow guard metadata differs")
    check(summary.get("timer_profile_trace") == 1, "historical full run is missing its audit flag")
    with (folder / "timer-profile.csv").open(newline="") as f:
        trace = list(csv.DictReader(f))
    with (folder / "acks.csv").open(newline="") as f:
        acks = {r["time_s"]: r for r in csv.DictReader(f)}
    with (folder / "events.csv").open(newline="") as f:
        core = list(csv.DictReader(f))
    core_times = [float(r["time_s"]) for r in core]
    recovery_times = {r["time_s"] for r in core if r["event"] in ("recovery-enter", "reno-fast-retransmit")}
    state = dict(rtt_ticks=0, srtt_scaled=0, variance_scaled=int(3 / tick), backoff=1,
                 active=0, probe_sequence=-1, max_sequence=-1)
    counts = dict(sample=0, duplicate=0, partial=0, backoff=0, expiry=0, forced=0)
    deadline, previous_time, context, pending_backoff = None, -1, None, None
    tx_times, ack_times, decisions = set(), [], []
    for r in trace:
        t, event, reason = float(r["time_s"]), r["event"], r["reason"]
        check(t >= previous_time, "historical audit time moved backwards")
        seq = int(r["sequence"])
        if event == "tx":
            tx_times.add(round(t * 1e9))
            if seq > state["max_sequence"]:
                state["max_sequence"] = seq
                if not state["active"]:
                    state["active"] = 1
                    state["probe_sequence"] = max(state["probe_sequence"], seq)
        elif event == "ack":
            if context is not None:
                check(context["done"], "previous ACK timer decision was omitted")
            check(r["time_s"] in acks and seq == int(acks[r["time_s"]]["una"]), "audit ACK differs from ACK trace")
            before_index = bisect.bisect_left(core_times, t) - 1
            check(before_index >= 0, "ACK has no preceding controller state")
            for k in ("una", "recovery", "recover_point"):
                check(int(r[k]) == int(float(core[before_index][k])), "audit ACK pre-state differs from controller trace: " + k)
            una = int(r["una"])
            advanced = seq > una
            eligible = seq >= una and (advanced or summary["algorithm"] == "fack")
            partial = summary["algorithm"] == "reno-sack" and int(r["recovery"]) and advanced and seq < int(r["recover_point"])
            context = dict(row=r, kind="partial" if partial else "sample", armed=False,
                           done=not eligible, eligible=eligible, partial_seen=False)
            ack_times.append(r["time_s"])
        elif event == "partial-ack":
            check(context is not None and context["eligible"] and context["kind"] == "partial", "unexpected SACK partial-ACK timer path")
            check(not context["partial_seen"] and seq == int(context["row"]["sequence"]), "duplicate/mismatched partial ACK")
            context["partial_seen"] = True
            state["backoff"] = 1
            counts["partial"] += 1
        elif event == "arm":
            if reason in ("ack", "sack-partial"):
                check(context is not None and context["eligible"] and not context["armed"], "unexpected ACK timer arm")
                check((reason == "sack-partial") == (context["kind"] == "partial"), "wrong partial/normal ACK arm")
                if reason == "sack-partial":
                    check(context["partial_seen"], "partial ACK failed to reset before arming")
                    context["done"] = True
                context["armed"] = True
            elif reason == "fast-retransmit":
                check(r["time_s"] in recovery_times, "fast-retransmit timer has no recovery-entry event")
                pending_backoff = reason
            elif reason in ("rto", "forced-restart"):
                check(pending_backoff == reason, "restart arm has no expiry/forced event")
            else:
                check(reason == "send", "unknown timer arm cause")
            base = ((state["srtt_scaled"] >> 3) + state["variance_scaled"]) * tick
            delay = max(2 * tick, base * state["backoff"])
            check(delay <= 2000 and abs(float(r["delay_s"]) - delay) < 3e-9, "historical armed deadline differs from integer estimator")
            deadline = t + delay
            decisions.append((r["time_s"], "arm", float(r["delay_s"])))
        elif event == "sample":
            check(context is not None and context["eligible"] and context["kind"] == "sample" and context["armed"] and not context["done"], "RTT update occurred before arm or for wrong ACK class")
            check(seq == int(context["row"]["sequence"]) and r["echo_s"] == context["row"]["echo_s"], "RTT sample does not belong to this ACK")
            echo, sample = float(r["echo_s"]), float(r["sample_s"])
            check(round(echo * 1e9) in tx_times and abs(sample - (t - echo)) < 3e-9 and sample >= 0,
                  "RTT sample does not match an echoed actual transmission")
            if state["active"] and seq > state["probe_sequence"]:
                state["active"], state["backoff"] = 0, 1
            lo = max(1, int((sample - 1.1e-9) / tick + .5))
            hi = max(1, int((sample + 1.1e-9) / tick + .5))
            rounded = int(r["rtt_ticks"])
            check(lo <= rounded <= hi, "RTT was not rounded to nearest historical tick")
            state["rtt_ticks"] = rounded
            if state["srtt_scaled"]:
                delta = rounded - (state["srtt_scaled"] >> 3)
                state["srtt_scaled"] = max(1, state["srtt_scaled"] + delta)
                state["variance_scaled"] = max(1, state["variance_scaled"] + abs(delta) - (state["variance_scaled"] >> 2))
            else:
                state["srtt_scaled"], state["variance_scaled"] = rounded << 3, rounded << 1
            counts["sample"] += 1
            counts["duplicate"] += seq == int(context["row"]["una"])
            context["done"] = True
        elif event == "backoff":
            check(pending_backoff == reason, "backoff was not preceded by its old-factor arm")
            state["backoff"] = min(64, 2 * state["backoff"])
            if state["backoff"] > 8:
                state["srtt_scaled"] = 0
            state["active"] = 0
            counts["backoff"] += 1
            pending_backoff = None
        elif event in ("expiry", "forced"):
            if event == "expiry":
                check(deadline is not None and abs(t - deadline) < 3e-9, "historical RTO did not fire at its current deadline")
                pending_backoff = "rto"
                counts["expiry"] += 1
                decisions.append((r["time_s"], "timeout", 0))
            else:
                pending_backoff = "forced-restart"
                counts["forced"] += 1
                decisions.append((r["time_s"], "forced-timeout", 0))
        elif event == "restart":
            check(pending_backoff == "forced-restart", "forced restart has no preserved-factor arm")
            state["active"] = 0
            pending_backoff = None
        elif event == "cancel":
            deadline = None
        else:
            check(False, "unknown historical timer audit event: " + event)
        for k, expected in state.items():
            check(int(r[k]) == expected, "historical state differs from independent replay: " + k)
        previous_time = t
    check(context is None or context["done"], "last ACK timer update is missing")
    check(ack_times == list(acks), "historical timer audit does not cover every received ACK")
    with (folder / "timers.csv").open(newline="") as f:
        ordinary = list(csv.DictReader(f))
    check(len(ordinary) == len(decisions), "ordinary/historical timer trace lengths differ")
    for row, (time, event, delay) in zip(ordinary, decisions):
        check(row["time_s"] == time and row["event"] == event and abs(float(row["delay_s"]) - delay) < 3e-9,
              "ordinary timer trace differs from independently replayed profile")
    for key, event in (("ns1_timer_samples", "sample"), ("ns1_duplicate_ack_samples", "duplicate"),
                       ("ns1_partial_sack_acks", "partial"), ("ns1_timer_backoffs", "backoff"),
                       ("rto_expiries", "expiry"), ("forced_timeouts", "forced")):
        check(summary[key] == counts[event], "historical summary counter differs: " + key)
    return counts

def validate_wire(folder, summary):
    folder = Path(folder)
    enabled = bool(summary.get("sack_option_bytes", False))
    log = folder / "link-events.csv"
    if not log.exists():
        if summary.get("detail", True) and not summary.get("reverse_connection", False):
            raise AssertionError(f"{folder.name}: full primary run is missing its physical wire journal")
        return {"wire_trace_checked": False, "wire_checks": 0,
                "wire_scope": "packet ledger is absent in light mode or checked at the parent connection"}
    checks = 0
    sizes, blocks_seen, last = Counter(), set(), {}

    def check(condition, message):
        nonlocal checks
        checks += 1
        if not condition:
            raise AssertionError(f"{folder.name}: {message}")

    with log.open(newline="") as stream:
        reader = csv.DictReader(stream)
        check(not enabled or "sack_blocks" in reader.fieldnames,
              "SACK wire mode needs block counts in physical traces")
        for row in reader:
            is_ack = bool(int(row["is_ack"]))
            count = int(row.get("sack_blocks", 0))
            actual = int(row["wire_bytes"])
            check(0 <= count <= 3, "physical SACK count outside receiver limit")
            check(is_ack or count == 0, "DATA packet carries a SACK count")
            expected = 40 + 8 * count if is_ack and enabled else 40 if is_ack else 1000
            check(actual == expected, "physical size disagrees with DATA/ACK/SACK model")
            if is_ack:
                sizes[actual] += 1
                blocks_seen.add(count)
            if row["event"] == "dequeue":
                direction = int(row["direction"])
                now = round(float(row["time_s"]) * 1e9)
                if direction in last:
                    time, previous_bytes = last[direction]
                    service = round(previous_bytes * 8e9 / 1.5e6)
                    check(now - time >= service - 2,
                          "bottleneck dequeued before prior packet serialization completed")
                last[direction] = (now, actual)
    return {"wire_trace_checked": True, "wire_checks": checks,
            "ack_size_events": {str(k): v for k, v in sorted(sizes.items())},
            "sack_block_counts_seen": sorted(blocks_seen),
            "wire_scope": "actual PPP-inclusive physical sizes and both-direction serialization"}

VALIDATION_SCHEMA = 6
TIMER_FIELDS = ("timer_policy", "initial_rto_s", "min_rto_s", "max_rto_s", "timer_tick_s")

def rows(path):
    with Path(path).open(newline="") as f:
        return list(csv.DictReader(f))

def validate_queue_probe(folder, summary, write=True):

    checks = 0

    def check(value, message):
        nonlocal checks
        checks += 1
        if not value:
            raise AssertionError(f"{folder.name}: {message}")

    trace = rows(folder / "queue-probe.csv")
    arrivals = [r for r in trace if r["event"] == "arrival"]
    dequeues = [r for r in trace if r["event"] == "dequeue"]
    drops = [r for r in trace if r["event"] == "drop"]
    check(all(r["event"] in ("arrival", "dequeue", "drop") for r in trace), "unknown queue probe event")
    check(len(arrivals) == summary["queue_probe_packets"], "probe did not inject every packet")
    check([int(r["segment"]) for r in arrivals] == list(range(len(arrivals))), "probe arrivals reordered or repeated")
    check(summary["waiting_capacity"] == summary["queue"] - 1, "queue convention does not include packet in service")
    service_ns = int(summary["service_ns"])
    check(service_ns > 0, "invalid probe serialization interval")
    pending, expected_dequeues, rejected = deque(), [], []
    completion = None
    last_time = -1
    for row in arrivals:
        t = round(float(row["time_s"]) * 1e9)
        sequence = int(row["segment"])
        check(t > last_time, "probe arrival times are not strictly increasing")
        while completion is not None and completion <= t:
            if pending:
                expected_dequeues.append((completion, pending.popleft()))
                completion += service_ns
            else:
                completion = None
        accepted = completion is None or len(pending) < summary["waiting_capacity"]
        check(int(row["expected_accept"]) == int(accepted), "C++ source-rule oracle disagrees with Python FIFO oracle")
        if not accepted:
            rejected.append(sequence)
        elif completion is None:
            expected_dequeues.append((t, sequence))
            completion = t + service_ns
        else:
            pending.append(sequence)
        last_time = t
    while pending:
        expected_dequeues.append((completion, pending.popleft()))
        completion += service_ns
    actual_dequeues = [(round(float(r["time_s"]) * 1e9), int(r["segment"])) for r in dequeues]
    check(actual_dequeues == expected_dequeues, "device FIFO departure order/time differs from Python queue-rule oracle")
    check([int(r["segment"]) for r in drops] == rejected, "device drop decisions differ from Python queue-rule oracle")
    check(summary["drops"] == summary["expected_drops"] == len(rejected), "probe summary drop count disagrees")
    for r in trace:
        check(0 <= int(r["waiting_packets"]) <= summary["waiting_capacity"], "probe exceeded waiting capacity")
    result = {"status": "PASS", "checks": checks, "schema": VALIDATION_SCHEMA, "mode": "queue_probe",
              "validator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "packet_ledger_checked": False, "queue_rule_oracle_checked": True,
              "scope": "actual NetDevice FIFO/drop/serialization trace versus Python reconstruction of documented ns-1 DropTail source rule; original ns-1 runtime not executed"}
    if write:
        (folder / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    return result

def validate(folder, write=True):
    folder = Path(folder)
    summary = json.loads((folder / "summary.json").read_text())
    if summary.get("mode") == "queue_probe":
        return validate_queue_probe(folder, summary, write=write)
    wire_info = validate_wire(folder, summary)
    full_detail = bool(summary.get("detail", True))
    events = rows(folder / "events.csv") if full_detail else []
    ack_rows = rows(folder / "acks.csv") if full_detail else []
    acks = {r["time_s"]: r for r in ack_rows}
    fack = summary["algorithm"] == "fack"
    sack = summary["algorithm"] == "reno-sack"
    retx, restart_copies, processed_ack_times = {}, {}, set()
    previous = None
    in_recovery_cwnd = None
    checks = 0

    def check(value, message):
        nonlocal checks
        checks += 1
        if not value:
            raise AssertionError(f"{folder.name}: {message}")

    def close(a, b):
        return abs(a - b) < 2e-7

    def finish(result):
        result.update(wire_info)
        result["checks"] += wire_info["wire_checks"]
        if write:
            (folder / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
        return result

    check(len(acks) == len(ack_rows), "multiple ACKs at one timestamp make ledger reconstruction ambiguous")
    check(summary["stop_s"] > summary.get("warmup_s", 0), "invalid measurement interval")
    check(summary["algorithm"] in ("reno", "reno-sack", "fack"), "unknown comparison algorithm")
    timer_metadata_available = any(k in summary for k in TIMER_FIELDS)
    if timer_metadata_available:
        check(all(k in summary for k in TIMER_FIELDS), "incomplete shared-timer metadata")
        policy=summary["timer_policy"]
        check(policy in ("shared-karn-jacobson", NS1_POLICY), "unknown timer estimator policy")
        check(bool(summary.get("ns1_timestamp_timer", False)) == (policy == NS1_POLICY),
              "timer mode flag and policy metadata disagree")
        check(0 < summary["min_rto_s"] <= summary["initial_rto_s"] <= summary["max_rto_s"], "invalid RTO bounds")
        check(summary["timer_tick_s"] > 0, "invalid timer granularity")
        if "forced_restart_backoff" in summary:
            check(summary["forced_restart_backoff"] == "preserve", "forced restart changes RTO backoff policy")
        if "rto_expiries" in summary:
            check(summary["rto_expiries"] == summary["timeouts"] - summary["forced_timeouts"],
                  "ordinary RTO count includes a forced controller restart")
    validator_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    check(summary["first_ack_s"] >= 0, "connection received no ACK")
    if not summary.get("reverse_connection", False):
        if summary.get("tiny_burst_bytes", 0) or summary.get("injected_ack_drops", 0):
            check(summary["first_ack_s"] >= .0872106666667 - 2e-9,
                  "cross traffic cannot make first ACK earlier than no-load topology")
        else:
            check(close(summary["first_ack_s"], .0872106666667),
                  "no-load ACK timing differs from topology serialization")

    busy_file = folder / "link-busy.csv"
    busy_checks_available = busy_file.exists()
    if busy_checks_available:
        busy_rows = rows(busy_file)
        forward_busy = reverse_data_busy = 0.0
        delivered_bins = unique_bins = 0
        prefix = "tiny_" if summary.get("reverse_connection", False) else ""
        warmup = float(summary.get("warmup_s", 0))
        stop = float(summary["stop_s"])
        check(warmup.is_integer(), "utilization reconstruction from bins requires integer warmup")
        expected_start = 0.0
        for raw in busy_rows:
            b = {k: float(v) for k, v in raw.items()}
            check(close(b["start_s"], expected_start), "link bins have a gap or overlap")
            duration = b["duration_s"]
            check(0 < duration <= 1, "invalid link-bin duration")
            check(b["start_s"] + duration <= stop + 2e-9, "link bin extends beyond simulation stop")
            for field in ("forward_busy_s", "reverse_busy_s", "bulk_data_s", "tiny_data_s"):
                check(-2e-9 <= b[field] <= duration + 2e-8, "link busy time exceeds bin capacity: " + field)
            check(b["bulk_data_s"] <= b["forward_busy_s"] + 2e-9, "bulk DATA exceeds total forward busy time")
            check(b["tiny_data_s"] <= b["reverse_busy_s"] + 2e-9, "tiny DATA exceeds total reverse busy time")
            for field in ("delivered_segments", "unique_received_segments", "tiny_delivered_segments", "tiny_unique_received_segments"):
                if field in b:
                    check(b[field] >= 0 and b[field].is_integer(), "invalid link-bin delivery count: " + field)
            if b["start_s"] >= warmup:
                forward_busy += b["forward_busy_s"]
                reverse_data_busy += b["tiny_data_s"]
                if prefix + "delivered_segments" in b:
                    delivered_bins += b[prefix + "delivered_segments"]
                    unique_bins += b[prefix + "unique_received_segments"]
            expected_start += duration
        check(close(expected_start, stop), "link bins do not cover full simulation")
        interval = stop - warmup
        check(abs(forward_busy / interval - summary["forward_utilization"]) < 1e-7,
              "forward utilization disagrees with independent link-bin sum")
        check(abs(reverse_data_busy / interval - summary["reverse_utilization"]) < 1e-7,
              "reverse tiny DATA utilization disagrees with independent link-bin sum")
        if "delivered_in_window" in summary:
            check(prefix + "delivered_segments" in b, "goodput counters are missing from link bins")
            check(delivered_bins == summary["delivered_in_window"], "delivered measurement-window count differs from bins")
            check(unique_bins == summary["unique_received_in_window"], "unique receive count differs from bins")
            check(summary["model_segment_bytes"] == 1000, "unexpected segment size in goodput conversion")
            check(abs(summary["goodput_mbps"] - delivered_bins * 1000 * 8 / interval / 1e6) < 1e-8,
                  "goodput differs from measurement-window delivered bytes")
    else:
        check(full_detail, "light mode is missing link-busy.csv")

    if not full_detail:
        check(summary.get("core_invariant_checks", 0) > 0,
              "light run did not record live controller invariant checks")
        result = {"status": "PASS", "checks": checks, "events": 0,
                  "detail": False, "schema": VALIDATION_SCHEMA,
                  "validator_sha256": validator_sha, "timer_metadata_checked": timer_metadata_available,
                  "packet_ledger_checked": False, "timer_profile_ledger_checked": False, "core_check_counter_checked": True,
                  "scope": "link-bin consistency/capacity and production-controller assertion counter; no packet ledger or independent controller implementation"}
        return finish(result)

    timer_file = folder / "timers.csv"
    timers_checked = timer_file.exists()
    if timer_metadata_available:
        check(timers_checked, "full trace is missing shared timer decisions")
    if timers_checked and summary["timer_policy"] == NS1_POLICY:
        validate_ns1_timer(folder, summary, check)
    elif timers_checked:
        timer_rows = rows(timer_file)
        deadline, previous_timer, previous_time = None, None, -1.0
        ordinary_expiries = forced_expiries = 0
        tick, minimum, maximum = (summary[k] for k in ("timer_tick_s", "min_rto_s", "max_rto_s"))

        def delay_for(rto, backoff):
            base = min(maximum, max(rto, minimum) * backoff)
            return min(maximum, math.ceil(base / tick - 1e-9) * tick)

        for raw in timer_rows:
            event = raw["event"]
            current_time = float(raw["time_s"])
            backoff = int(raw["backoff"])
            rto, delay = float(raw["rto_s"]), float(raw["delay_s"])
            check(current_time >= previous_time, "timer time moved backwards")
            check(backoff in (1, 2, 4, 8, 16, 32, 64), "invalid exponential timer backoff")
            check(rto > 0 and float(raw["srtt_s"]) >= 0 and float(raw["rttvar_s"]) >= 0,
                  "invalid RTT estimator values")
            if event == "arm":

                low = delay_for(max(0, rto - .51e-9), backoff)
                high = delay_for(rto + .51e-9, backoff)
                check(low - 2e-9 <= delay <= high + 2e-9, "armed delay differs from shared RTO clamp/tick policy")
                if previous_timer and previous_timer["event"] == "timeout":
                    check(backoff == min(64, int(previous_timer["backoff"]) * 2),
                          "timeout did not double the next timer backoff")
                elif previous_timer and previous_timer["event"] == "forced-timeout":
                    check(backoff == int(previous_timer["backoff"]),
                          "forced controller restart did not preserve the timer backoff")
                elif previous_timer:
                    check(backoff in (1, int(previous_timer["backoff"])),
                          "backoff changed without timeout or advancing ACK reset")
                else:
                    check(backoff == 1 and close(rto, summary["initial_rto_s"]),
                          "first timer differs from shared initial RTO")
                deadline = current_time + delay
            elif event == "timeout":
                ordinary_expiries += 1
                check(deadline is not None and abs(current_time - deadline) < 3e-9,
                      "timer expired before/after the last armed deadline")
                check(delay == 0, "timeout record has a spurious armed delay")
            elif event == "forced-timeout":
                forced_expiries += 1
                check(delay == 0, "forced timeout record has a spurious armed delay")
            else:
                check(False, "unknown timer event: " + event)
            previous_timer, previous_time = raw, current_time
        check(ordinary_expiries + forced_expiries == summary["timeouts"], "timer expiry count differs from summary")
        check(forced_expiries == summary["forced_timeouts"], "forced timeout count differs from summary")
        if "rto_expiries" in summary:
            check(ordinary_expiries == summary["rto_expiries"], "ordinary RTO trace count differs from summary")

    for raw in events:
        e = raw["event"]
        t = raw["time_s"]
        s = {k: float(v) for k, v in raw.items() if k != "event"}
        if previous:
            check(s["time_s"] >= previous["time_s"], "event time moved backwards")
        check(s["una"] <= s["nxt"] <= s["high_water"], "send-cursor sequence ordering")
        check(s["una"] <= s["fack"] <= s["high_water"], "received-evidence sequence ordering")
        if fack:
            check(s["fack"] <= s["nxt"], "FACK forward edge exceeds NXT")
            check(close(s["awnd"], s["nxt"] - s["fack"] + s["retran_data"]), "equation (2)")
            check(close(s["effective_window"], s["cwnd"] + s["wintrim"]), "effective window")
        elif sack:
            expected_pipe = s["sack_pipe"] if s["recovery"] else s["nxt"] - s["una"]
            check(close(s["awnd"], expected_pipe), "Reno+SACK uses wrong ACK-counted pipe")
            expected_window = min(summary.get("receiver_window", 100), s["cwnd"])
            check(close(s["effective_window"], expected_window), "Reno+SACK packet-window budget")
            if e == "recovery-enter" and previous:
                check(close(s["sack_pipe"], math.floor(previous["cwnd"]) - 3),
                      "Reno+SACK initial pipe is not previous whole window minus three ACKs")
            if s["recovery"] and e in ("send-new", "selective-retransmit") and previous:
                first_fast_retransmit = e == "selective-retransmit" and previous["event"] == "recovery-enter"
                increment = 0 if first_fast_retransmit else 1
                check(close(s["sack_pipe"], previous["sack_pipe"] + increment),
                      "Reno+SACK pipe did not count an injected packet correctly")
        else:
            check(close(s["awnd"], s["nxt"] - s["una"]), "classic Reno uses wrong outstanding estimate")
            if "reno_inflation" in s:
                expected_window = min(summary.get("receiver_window", 100), s["cwnd"] + s["reno_inflation"])
                check(close(s["effective_window"], expected_window), "classic Reno inflation window budget")
            if e == "reno-fast-retransmit":
                check(s["dupacks"] == 3 and s["segment"] == s["una"], "classic Reno Fast Retransmit trigger/segment")
                if previous:
                    expected_cwnd = min(previous["cwnd"], summary.get("receiver_window", 100)) / 2
                    if summary.get("integer_cut"):
                        expected_cwnd = math.floor(expected_cwnd)
                    expected_cwnd = max(1, expected_cwnd)
                    check(close(s["cwnd"], expected_cwnd), "classic Reno Fast Recovery window cut")
            if previous and previous["recovery"] and e == "ack":
                if s["una"] > previous["una"]:
                    check(not s["recovery"], "classic Reno stayed in recovery after an advancing ACK")
                else:
                    check(close(s["cwnd"], previous["cwnd"]), "classic Reno base cwnd changed on a recovery duplicate ACK")
        if e in ("timeout", "sack-fallback"):
            check(close(s["cwnd"], 1) and s["dupacks"] == 0 and not s["recovery"],
                  "timeout/fallback did not restart Slow-start")
        if fack or sack:
            if e in ("ack", "recovery-enter", "recovery-exit", "lost-retransmission", "sack-fallback") and t not in processed_ack_times:
                a = acks[t]
                if sack and previous and previous["recovery"] and e != "sack-fallback":
                    additional = int(int(a["una"]) > previous["una"] and int(a["una"]) < previous["recover_point"])
                    check(close(s["sack_pipe"], previous["sack_pipe"] - 1 - additional),
                          "Reno+SACK pipe did not count a returning ACK/partial ACK correctly")
                def reported_received(sequence):
                    return sequence < int(a["una"]) or any(
                        int(a[f"left{i}"]) <= sequence < int(a[f"right{i}"])
                        for i in (1, 2, 3) if int(a[f"left{i}"]) >= 0
                    )
                for sequence in list(retx):
                    if reported_received(sequence):
                        del retx[sequence]
                if fack:
                    for sequence in list(restart_copies):
                        if reported_received(sequence):
                            del restart_copies[sequence]
                        elif sequence < s["fack"]:
                            check(sequence not in retx, "restart copy promoted into ledger twice")
                            retx[sequence] = restart_copies.pop(sequence)
                processed_ack_times.add(t)
            if e in ("timeout", "sack-fallback"):
                retx.clear()
                restart_copies.clear()
                in_recovery_cwnd = None
            if sack and e == "recovery-exit":

                retx.clear()
            if fack and e == "restart-retransmit":
                sequence = int(s["segment"])
                check(sequence not in retx and sequence not in restart_copies,
                      "timeout restart sent an already outstanding copy twice")
                check(s["fack"] <= sequence < s["nxt"],
                      "timeout copy is not represented in the restarted NXT-FACK interval")
                restart_copies[sequence] = int(s["high_water"])
            if e == "selective-retransmit":
                sequence = int(s["segment"])
                check(sequence not in retx, "same outstanding copy retransmitted twice")
                retx[sequence] = int(s["high_water"])
                check(bool(s["recovery"]), "selective retransmission before recovery")
                check(s["una"] <= sequence < s["fack"], "retransmitted sequence is not an old hole")
            check(close(s["retran_data"], len(retx)), "ACK/transmission-trace retransmission ledger disagrees")
            if e == "recovery-enter":
                if fack:
                    check(s["fack"] - s["una"] > 3 or s["dupacks"] == 3, "section 3.1 trigger")
                else:
                    check(s["dupacks"] == 3, "Reno+SACK fast recovery requires three duplicate ACKs")
                boundary = s["high_water"] if fack else s["nxt"]
                check(close(s["recover_point"], boundary), "recovery boundary is not the true next-unsent edge")
                in_recovery_cwnd = s["cwnd"]
                if fack:
                    trim = (s["nxt"] - s["fack"]) * (1 - s["winmult"]) if summary["rampdown"] else 0
                    check(close(s["wintrim"], trim), "Rampdown equation (3)")
            if e == "recovery-exit":
                check(s["una"] >= s["recover_point"], "section 3.2 premature recovery exit")
                in_recovery_cwnd = None
            if in_recovery_cwnd is not None:
                check(close(s["cwnd"], in_recovery_cwnd), "cwnd changed during selective recovery")
            if fack and previous and previous["recovery"] and e == "ack" and s["recovery"]:
                trim = max(0, previous["wintrim"] - (s["fack"] - previous["fack"]) * (1 - s["winmult"]))
                check(close(s["wintrim"], trim), "Rampdown equation (4)")
            if e == "lost-retransmission":
                check(any(s["fack"] > captured for captured in retx.values()), "forced RTO without lost-copy evidence")
        if fack and e in ("send-new", "restart-retransmit", "selective-retransmit") and previous:
            budget = math.floor(previous["effective_window"]) if summary.get("packet_window") else previous["effective_window"]
            check(previous["awnd"] < budget, "sent outside selected whole-packet/strict window budget")
            check(s["awnd"] <= s["effective_window"] + 1, "pump overshoot exceeds one MSS")
        previous = {**s, "event": e}

    received, duplicate_count, next_segment = set(), 0, 0
    delivered_window = unique_window = 0
    for r in rows(folder / "receiver.csv"):
        sequence = int(r["segment"])
        previous_next = next_segment
        duplicate = sequence in received
        duplicate_count += duplicate
        check(int(r["duplicate"]) == duplicate, "receiver duplicate flag disagrees with independent set")
        received.add(sequence)
        while next_segment in received:
            next_segment += 1
        check(next_segment == int(r["cumulative_segments"]), "receiver cumulative ACK is incorrect")
        if summary.get("warmup_s", 0) <= float(r["time_s"]) < summary["stop_s"]:
            delivered_window += next_segment - previous_next
            unique_window += not duplicate
    check(duplicate_count == summary["duplicate_segments"], "summary duplicate count")
    check(next_segment == summary["delivered_segments"], "summary delivered count")
    if "delivered_in_window" in summary:
        check(delivered_window == summary["delivered_in_window"], "receiver cumulative advancement differs from measured goodput count")
        check(unique_window == summary["unique_received_in_window"], "receiver unique set differs from measured receive count")
    network = rows(folder / "network.csv")
    waiting, last_dequeue = Counter(), -1
    for r in network:
        e, sequence, t = r["event"], int(r["segment"]), float(r["time_s"])
        if e == "enqueue": waiting[sequence] += 1
        if e == "dequeue":
            check(waiting[sequence] > 0, "dequeue without accepted enqueue")
            waiting[sequence] -= 1
            check(last_dequeue < 0 or t - last_dequeue >= 1000 * 8 / 1.5e6 - 2e-9,
                  "bottleneck transmitted faster than 1.5 Mb/s")
            last_dequeue = t
        check(0 <= int(r["waiting_packets"]) <= summary["waiting_capacity"], "DropTail capacity violated")
    result = {"status": "PASS", "checks": checks, "events": len(events),
              "detail": True, "schema": VALIDATION_SCHEMA,
              "validator_sha256": validator_sha, "timer_metadata_checked": timer_metadata_available,
              "link_bins_checked": busy_checks_available, "timers_checked": timers_checked,
              "timer_profile_ledger_checked": timers_checked and summary["timer_policy"] == NS1_POLICY,
              "packet_ledger_checked": bool(fack or sack), "core_check_counter_checked": False,
              "scope": "emitted packet/state/timer trace consistency, receiver reconstruction and queue accounting; not an independent controller implementation"}
    return finish(result)

if __name__ == "__main__":
    import sys
    print(json.dumps(validate(sys.argv[1]), indent=2))
