"""perf script Python callback for the fixed-period, single-process cpu-clock collector."""

import json
import os

PID = int(os.environ["SPE_FIXTURE_PID"])


def process_event(event):
    sample = event.get("sample", {})
    if sample.get("pid") != PID or not event.get("ev_name", "").startswith("cpu-clock"):
        raise ValueError("unexpected perf event or PID")
    ip = sample["ip"]
    stack = [
        entry["ip"]
        for entry in event.get("callchain", [])
        if 0 < entry["ip"] < (1 << 63)
    ]
    if not stack or stack[0] != ip:
        stack.insert(0, ip)
    if len(stack) > 32:
        raise ValueError("perf stack depth exceeded")
    print(json.dumps(stack))
