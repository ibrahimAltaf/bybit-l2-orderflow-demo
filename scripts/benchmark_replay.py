"""Benchmark the real-data replay: wall time, throughput, peak memory, hot spots.

    python scripts/benchmark_replay.py               # timed full-day replay
    python scripts/benchmark_replay.py --profile N   # cProfile the first N events instead

Writes into a temporary directory; data/evidence/ is not touched. Peak memory
is the process peak resident set (Windows: PeakWorkingSetSize, POSIX:
ru_maxrss), read at the end of the run.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import platform
import pstats
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

from bybit_demo.config import load_config  # noqa: E402
from bybit_demo.pipeline import run_replay  # noqa: E402


def peak_rss_mb() -> float | None:
    if sys.platform == "win32":
        import ctypes  # noqa: PLC0415 - platform-specific
        from ctypes import wintypes  # noqa: PLC0415

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        c = Counters()
        c.cb = ctypes.sizeof(c)
        kernel32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        ok = psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return c.PeakWorkingSetSize / 2**20 if ok else None
    import resource  # noqa: PLC0415 - POSIX only

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / 2**20 if sys.platform == "darwin" else rss / 2**10


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", type=int, help="cProfile only the first N events")
    args = ap.parse_args()
    cfg = load_config(Path("config/demo.yaml"))
    if args.profile:
        cfg = cfg.model_copy(
            update={"replay": cfg.replay.model_copy(update={"max_events": args.profile})}
        )
    with tempfile.TemporaryDirectory() as tmp:
        prof = cProfile.Profile() if args.profile else None
        t0 = time.perf_counter()
        if prof:
            prof.enable()
        res = run_replay(cfg, cfg.primary(), Path(tmp))
        if prof:
            prof.disable()
        wall = time.perf_counter() - t0
    s = res.summary
    out = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "events": s.events,
        "evaluations": s.evaluations,
        "wall_time_s": round(wall, 2),
        "events_per_s": round(s.events / wall),
        "peak_rss_mb": round(peak_rss_mb() or 0.0, 1),
        "profiled": bool(prof),
    }
    print(json.dumps(out, indent=2))
    if prof:
        buf = io.StringIO()
        pstats.Stats(prof, stream=buf).sort_stats("tottime").print_stats(15)
        print(buf.getvalue())
    return 0


if __name__ == "__main__":
    sys.exit(main())
