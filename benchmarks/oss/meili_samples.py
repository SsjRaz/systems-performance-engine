"""Linux x86-64 gperftools capture for the pinned, local Meilisearch study."""

from collections import Counter
from contextlib import contextmanager
from functools import lru_cache
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import time


HZ = 250
OTHER = ("[other]", "", 0)


class Sampler:
    def __init__(self, library, prefix):
        self.library = library.resolve(strict=True)
        self.prefix = prefix
        self.raw = prefix.with_name(prefix.name + ".0")
        if self.raw.exists() or prefix.exists():
            raise ValueError("capture already exists")

    def environment(self):
        return dict(
            LD_PRELOAD=str(self.library),
            CPUPROFILE=str(self.prefix),
            CPUPROFILESIGNAL=str(int(signal.SIGUSR2)),
            CPUPROFILE_FREQUENCY=str(HZ),
            TCMALLOC_STACKTRACE_METHOD="libunwind",
        )

    @contextmanager
    def active(self, pid, log):
        caught = re.search(
            r"^SigCgt:\s*(\w+)$", Path(f"/proc/{pid}/status").read_text(), re.M
        )
        if not caught or not int(caught[1], 16) & (1 << (signal.SIGUSR2 - 1)):
            raise ValueError("profiler toggle signal has no handler")
        os.kill(pid, signal.SIGUSR2)
        try:
            await_condition(self.raw.exists, "profiler did not start")
            yield
        finally:
            os.kill(pid, signal.SIGUSR2)

        def flushed():
            return re.search(
                rb"PROFILE: interrupts/evictions/bytes = (\d+)/\d+/\d+",
                log.read_bytes(),
            )

        self.interrupts = int(await_condition(flushed, "profiler did not flush")[1])


def await_condition(check, error):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.01)
    raise ValueError(error)


def raw_profile(raw, expected):
    if not 40 < raw.stat().st_size <= 8 * 1024**2:
        raise ValueError("unexpected capture size")
    data = raw.read_bytes()
    if struct.unpack_from("<5Q", data) != (0, 3, 0, 1_000_000 // HZ, 0):
        raise ValueError("unexpected gperftools header or frequency")
    pos, total = 40, 0
    rows = []
    while pos + 24 <= len(data):
        weight, depth = struct.unpack_from("<2Q", data, pos)
        pos += 16
        if not 1 <= depth <= 256 or pos + depth * 8 > len(data):
            raise ValueError("invalid gperftools stack")
        pcs = struct.unpack_from(f"<{depth}Q", data, pos)
        pos += depth * 8
        if weight == 0 and pcs == (0,):
            if total != expected or not 0 < total <= 100000:
                raise ValueError("sample weights differ from profiler interrupts")
            return rows, data[pos:].decode("utf-8")
        total += weight
        if weight < 1 or total > 100000 or not all(pcs):
            raise ValueError("invalid sample count or PC")
        # The native format stores the interrupted PC and unadjusted caller return addresses.
        rows.append((weight, (pcs[0], *(pc - 1 for pc in pcs[1:]))))
    raise ValueError("missing gperftools trailer")


@lru_cache(maxsize=4)
def segments(binary):
    output = subprocess.check_output(["readelf", "-lW", binary], text=True)
    result = []
    for row in output.splitlines():
        fields = row.split()
        if fields[:1] == ["LOAD"] and "E" in fields[6:-1]:
            offset, vma, size = (int(fields[i], 16) for i in (1, 2, 4))
            result.append((offset, offset + size, vma))
    if not result:
        raise ValueError("no executable ELF load segment")
    return result


def addresses(binary, mappings, pcs):
    ranges = []
    for row in mappings.splitlines():
        # gperftools can concatenate a wide inode field and the absolute pathname.
        item = re.fullmatch(
            r"([0-9a-f]+)-([0-9a-f]+)\s+([\w-]+)\s+([0-9a-f]+)\s+[\da-f]+:[\da-f]+\s+\d+\s*(/.*)",
            row,
        )
        if item and "x" in item[3] and item[5] == str(binary):
            ranges.append((int(item[1], 16), int(item[2], 16), int(item[4], 16)))
    if not ranges:
        raise ValueError("sampled ELF absent from executable mappings")
    result = {}
    for pc in pcs:
        for begin, end, offset in ranges:
            if begin <= pc < end:
                file_offset = pc - begin + offset
                for lo, hi, vma in segments(binary):
                    if lo <= file_offset < hi:
                        result[pc] = vma + file_offset - lo
                        break
    return result


def symbolize(binary, vmas):
    unique = sorted(set(vmas))
    if not unique:
        return {}
    out = subprocess.run(
        ["addr2line", "-fCe", binary],
        input="".join(f"{pc:x}\n" for pc in unique),
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    ).stdout.splitlines()
    if len(out) != 2 * len(unique):
        raise ValueError("incomplete addr2line export")
    result = {}
    for pc, symbol, source in zip(unique, out[::2], out[1::2]):
        symbol = re.sub(r"::h[0-9a-f]{16}(?=$|::)", "", symbol)
        src = re.search(r"(?:^|/)(crates/[\w./-]+\.rs):([1-9][0-9]*)", source, re.ASCII)
        if symbol == "??" or not re.fullmatch(r"[ -~]{1,512}", symbol):
            result[pc] = OTHER
        else:
            result[pc] = (symbol, src[1] if src else "", int(src[2]) if src else 0)
    return result


def compact(rows, maps, binary):
    # Keep the interrupted PC and three actual callers. Weight always stays with its original leaf.
    pcs = {pc for _, trace in rows for pc in trace[:4]}
    vmas = addresses(binary, maps, pcs)
    symbols = symbolize(binary, vmas.values())
    decoded = [
        (weight, tuple(symbols.get(vmas.get(pc), OTHER) for pc in trace[:4]))
        for weight, trace in rows
    ]
    # Represent the most frequent exact frames under a fixed byte budget. All omitted frames
    # become [other], including their original weight in the denominator.
    frequency = Counter()
    for weight, trace in decoded:
        for frame in trace:
            frequency[frame] += weight
    frames, index, size = [OTHER], {OTHER: 0}, 0
    for frame, _ in frequency.most_common():
        cost = 32 + len(frame[0]) + len(frame[1])
        if frame != OTHER and len(frames) < 400 and size + cost <= 32000:
            index[frame] = len(frames)
            frames.append(frame)
            size += cost
    stacks = Counter()
    for weight, trace in decoded:
        stacks[tuple(index.get(frame, 0) for frame in trace)] += weight
    # For infrequent stacks keep the true leaf, with callers omitted.
    common = Counter(dict(stacks.most_common(450)))
    retained = set(common)
    for trace, weight in stacks.items():
        if trace not in retained:
            common[(trace[0],)] += weight
    return frames, [[weight, list(trace)] for trace, weight in sorted(common.items())]
