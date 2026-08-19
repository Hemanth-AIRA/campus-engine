#!/usr/bin/env python3
"""
load_test_monitor.py
====================
Resource monitor for AIRA concurrent interview load testing.

Usage:
    # Terminal 1 — start the monitor BEFORE launching interviews
    python load_test_monitor.py --label "10-interview-test" --pname "aira_3_webrtc"

    # Terminal 2 — launch interviews normally (or use --simulate below)
    python aira_3_webrtc_orchestration.py

    # Or run both together:
    python load_test_monitor.py --label "10-test" --pname "aira_3_webrtc" &
    python aira_3_webrtc_orchestration.py

Simulate mode (no real interviews needed — just stresses the system):
    python load_test_monitor.py --simulate 10 --duration 120

Output:
    logs/load_test_<label>_<timestamp>/
        summary.log          ← human-readable second-by-second table
        metrics.csv          ← machine-readable for graphing
        final_report.txt     ← peak/avg/p95 stats printed at end
"""

import argparse
import csv
import os
import signal
import sys
import time
import threading
import multiprocessing as mp
from datetime import datetime
from pathlib import Path

try:
    import psutil
except ImportError:
    print("❌ psutil not found. Install: pip install psutil")
    sys.exit(1)


# ─── Config ────────────────────────────────────────────────────────────────────

SAMPLE_INTERVAL_S = 1.0          # How often to sample (seconds)
PROCESS_MATCH_NAMES = [          # Process name fragments to track individually
    "aira_3_webrtc",
    "python",
    "orchestrat",
]


# ─── Helpers ───────────────────────────────────────────────────────────────────

def get_matching_procs(name_fragments):
    """Return list of psutil.Process matching any fragment."""
    matched = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "status"]):
        try:
            cmdline = " ".join(p.info["cmdline"] or [])
            name = p.info["name"] or ""
            if any(f in cmdline or f in name for f in name_fragments):
                matched.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return matched


def format_bytes(b):
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TB"


# ─── Monitor loop ──────────────────────────────────────────────────────────────

class ResourceMonitor:
    def __init__(self, label: str, pname_fragments: list, out_dir: Path):
        self.label = label
        self.pname_fragments = pname_fragments
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.summary_path = out_dir / "summary.log"
        self.csv_path     = out_dir / "metrics.csv"
        self.report_path  = out_dir / "final_report.txt"

        self._stop = threading.Event()
        self._rows = []          # All sampled rows for final stats
        self._lock = threading.Lock()
        self._start_time = time.time()

        # Pre-call to warm up CPU percent (first call returns 0.0)
        psutil.cpu_percent(interval=None)

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"📊 Monitor started → {self.out_dir}")

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self._write_final_report()
        print(f"\n✅ Monitor stopped. Report: {self.report_path}")

    # ── Main sampling loop ────────────────────────────────────────────────────

    def _loop(self):
        # Write CSV header
        with open(self.csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "elapsed_s", "timestamp",
                "sys_cpu_pct", "sys_ram_used_mb", "sys_ram_pct",
                "sys_swap_mb",
                "aira_proc_count", "aira_cpu_total_pct", "aira_ram_total_mb",
                "aira_threads_total",
                "per_proc_detail",
            ])

        # Write summary header
        with open(self.summary_path, "w") as f:
            f.write(f"AIRA Load Test Monitor — {self.label}\n")
            f.write(f"Started: {datetime.now().isoformat()}\n")
            f.write("=" * 120 + "\n")
            f.write(
                f"{'Elapsed':>8}  {'SysCPU':>7}  {'SysRAM':>10}  "
                f"{'Procs':>6}  {'AIRAmCPU':>9}  {'AIRAm RAM':>10}  "
                f"{'Threads':>8}  Detail\n"
            )
            f.write("-" * 120 + "\n")

        tick = 0
        while not self._stop.is_set():
            t0 = time.time()
            self._sample(tick)
            tick += 1
            # Sleep remainder of interval
            elapsed = time.time() - t0
            sleep_for = max(0.0, SAMPLE_INTERVAL_S - elapsed)
            self._stop.wait(timeout=sleep_for)

    def _sample(self, tick: int):
        elapsed_s = time.time() - self._start_time
        ts = datetime.now().strftime("%H:%M:%S")

        # ── System-wide ──
        sys_cpu   = psutil.cpu_percent(interval=None)
        mem       = psutil.virtual_memory()
        swap      = psutil.swap_memory()
        sys_ram_mb  = mem.used / 1024 / 1024
        sys_ram_pct = mem.percent
        sys_swap_mb = swap.used / 1024 / 1024

        # ── AIRA processes ──
        procs = get_matching_procs(self.pname_fragments)
        proc_count     = len(procs)
        aira_cpu_total = 0.0
        aira_ram_total = 0.0
        aira_threads   = 0
        proc_details   = []

        for p in procs:
            try:
                with p.oneshot():
                    cpu  = p.cpu_percent(interval=None)
                    ram  = p.memory_info().rss / 1024 / 1024
                    thr  = p.num_threads()
                    cmd  = " ".join(p.cmdline())[-60:]
                    aira_cpu_total += cpu
                    aira_ram_total += ram
                    aira_threads   += thr
                    proc_details.append(
                        f"PID={p.pid} cpu={cpu:.1f}% ram={ram:.0f}MB thr={thr}"
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        detail_str = " | ".join(proc_details) if proc_details else "no matching procs"

        # ── Write CSV row ──
        row = {
            "elapsed_s": round(elapsed_s, 1),
            "timestamp": ts,
            "sys_cpu_pct": sys_cpu,
            "sys_ram_used_mb": round(sys_ram_mb, 1),
            "sys_ram_pct": sys_ram_pct,
            "sys_swap_mb": round(sys_swap_mb, 1),
            "aira_proc_count": proc_count,
            "aira_cpu_total_pct": round(aira_cpu_total, 1),
            "aira_ram_total_mb": round(aira_ram_total, 1),
            "aira_threads_total": aira_threads,
            "per_proc_detail": detail_str,
        }

        with open(self.csv_path, "a", newline="") as f:
            w = csv.writer(f)
            w.writerow([row[k] for k in [
                "elapsed_s", "timestamp",
                "sys_cpu_pct", "sys_ram_used_mb", "sys_ram_pct",
                "sys_swap_mb",
                "aira_proc_count", "aira_cpu_total_pct", "aira_ram_total_mb",
                "aira_threads_total",
                "per_proc_detail",
            ]])

        # ── Write summary line ──
        summary_line = (
            f"{elapsed_s:>7.1f}s  {sys_cpu:>6.1f}%  "
            f"{sys_ram_mb:>7.0f}MB ({sys_ram_pct:.0f}%)  "
            f"{proc_count:>5}  {aira_cpu_total:>8.1f}%  "
            f"{aira_ram_total:>7.0f}MB  "
            f"{aira_threads:>7}  "
            f"{detail_str[:60]}\n"
        )
        with open(self.summary_path, "a") as f:
            f.write(summary_line)

        # Print to stdout every 5s
        if tick % 5 == 0:
            print(
                f"[{ts}] +{elapsed_s:.0f}s | "
                f"CPU={sys_cpu:.1f}% RAM={sys_ram_mb:.0f}MB({sys_ram_pct:.0f}%) | "
                f"AIRA procs={proc_count} cpu={aira_cpu_total:.1f}% ram={aira_ram_total:.0f}MB "
                f"threads={aira_threads}"
            )

        with self._lock:
            self._rows.append(row)

    # ── Final report ─────────────────────────────────────────────────────────

    def _write_final_report(self):
        rows = self._rows
        if not rows:
            return

        def col(key):
            return [r[key] for r in rows]

        def pct(lst, p):
            s = sorted(lst)
            idx = int(len(s) * p / 100)
            return s[min(idx, len(s) - 1)]

        total_s    = rows[-1]["elapsed_s"]
        peak_procs = max(col("aira_proc_count"))
        peak_ram   = max(col("aira_ram_total_mb"))
        avg_ram    = sum(col("aira_ram_total_mb")) / len(rows)
        peak_cpu   = max(col("sys_cpu_pct"))
        avg_cpu    = sum(col("sys_cpu_pct")) / len(rows)
        p95_cpu    = pct(col("sys_cpu_pct"), 95)
        p95_ram    = pct(col("aira_ram_total_mb"), 95)
        peak_thr   = max(col("aira_threads_total"))

        lines = [
            f"AIRA Load Test — Final Report",
            f"Label     : {self.label}",
            f"Duration  : {total_s:.0f}s ({total_s/60:.1f} min)",
            f"Samples   : {len(rows)}",
            f"",
            f"── System CPU ─────────────────────────────",
            f"  Peak      : {peak_cpu:.1f}%",
            f"  Avg       : {avg_cpu:.1f}%",
            f"  p95       : {p95_cpu:.1f}%",
            f"",
            f"── AIRA Process RAM ────────────────────────",
            f"  Peak total: {peak_ram:.0f} MB  ({peak_ram/1024:.2f} GB)",
            f"  Avg total : {avg_ram:.0f} MB",
            f"  p95       : {p95_ram:.0f} MB",
            f"  Per-proc  : {(peak_ram/peak_procs):.0f} MB avg at peak procs={peak_procs}" if peak_procs > 0 else f"  Per-proc  : N/A (no AIRA procs tracked)",
            f"",
            f"── AIRA Processes ──────────────────────────",
            f"  Peak procs: {peak_procs}",
            f"  Peak thrs : {peak_thr}",
            f"",
            f"── Capacity Estimate (16 GB VM) ────────────",
        ]

        per_proc_mb = (peak_ram / peak_procs) if peak_procs > 0 else 0
        usable_mb   = 16 * 1024 * 0.80   # 80% of 16GB usable
        est_max     = int(usable_mb / per_proc_mb) if per_proc_mb > 0 else 0
        lines += [
            f"  RAM / process (peak): {per_proc_mb:.0f} MB",
            f"  Usable RAM (80%):     {usable_mb:.0f} MB",
            f"  Est. max interviews : {est_max}",
            f"",
            f"Output files:",
            f"  Summary : {self.summary_path}",
            f"  CSV     : {self.csv_path}",
        ]

        report_text = "\n".join(lines)
        with open(self.report_path, "w") as f:
            f.write(report_text + "\n")

        print("\n" + "=" * 60)
        print(report_text)
        print("=" * 60)


# ─── Simulate mode ─────────────────────────────────────────────────────────────

def _burn(args):
    """Simulate one interview-like worker (module-level so mp.Pool can pickle it)."""
    i, duration = args
    import time, os
    print(f"  🔥 Sim worker {i} started (PID={os.getpid()})")
    buf = bytearray(150 * 1024 * 1024)   # 150 MB RAM per worker
    end = time.time() + duration
    x = 0.0
    while time.time() < end:
        x = (x + 1.23456) ** 0.99999     # light CPU burn
    del buf
    print(f"  ✅ Sim worker {i} done")


def _simulate_worker(n: int, duration: int):
    """Simulate N concurrent CPU+RAM-burning processes for `duration` seconds."""
    with mp.Pool(processes=n) as pool:
        pool.map(_burn, [(i, duration) for i in range(n)])


# ─── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AIRA Load Test Resource Monitor")
    parser.add_argument("--label",    default="test",   help="Label for this test run")
    parser.add_argument("--pname",    default="aira_3_webrtc", help="Process name fragment to track")
    parser.add_argument("--outdir",   default="logs/load_tests", help="Output directory root")
    parser.add_argument("--simulate", type=int, default=0,
                        help="Simulate N concurrent interview-like processes (no real interviews)")
    parser.add_argument("--duration", type=int, default=120,
                        help="Simulation duration in seconds (only with --simulate)")
    args = parser.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.outdir) / f"{args.label}_{ts}"

    fragments = [args.pname, "aira_3_webrtc", "orchestrat"]

    monitor = ResourceMonitor(
        label=args.label,
        pname_fragments=fragments,
        out_dir=out_dir,
    )

    # Graceful Ctrl+C
    def _sig(sig, frame):
        print("\n⚠️  Interrupted — writing final report...")
        monitor.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    monitor.start()

    if args.simulate > 0:
        print(f"\n🧪 Simulate mode: {args.simulate} workers × {args.duration}s")
        print("   Each worker uses ~150 MB RAM + light CPU burn\n")
        time.sleep(2)  # Let monitor log baseline
        _simulate_worker(args.simulate, args.duration)
        time.sleep(3)  # Final samples after workers exit
    else:
        print(f"\n👀 Watching for '{args.pname}' processes...")
        print("   Start your interviews now. Press Ctrl+C to stop.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    monitor.stop()


if __name__ == "__main__":
    main()
