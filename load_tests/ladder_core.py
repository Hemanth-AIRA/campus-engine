"""
load_tests/ladder_core.py
Core ladder-benchmark engine used by all magic-number finders.

A "ladder" test:
  1. Defines a list of concurrency steps  [1, 2, 5, 10, 15, 20, 25, 30, 40, 50]
  2. At each step N, fires N async calls simultaneously
  3. Records p50 / p90 / p95 / p99 / max per step
  4. After all steps, finds the "magic number" = last step where
       p95 <= baseline_p95 * DEGRADATION_FACTOR
  5. Generates a Markdown + PNG report

Usage (internal):
    from load_tests.ladder_core import LadderBenchmark
    bench = LadderBenchmark("Deepgram TTFA", steps=[1,2,5,10,20,30,50])
    await bench.run(my_async_callable)
    bench.save_report("reports/deepgram_magic.md")
"""

import asyncio, statistics, time, json, logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Awaitable

import numpy as np

logger = logging.getLogger("ladder")

# ── Thresholds ─────────────────────────────────────────────────────────
# Magic number = last step where p95 < baseline * factor
DEGRADATION_FACTOR = 2.0   # 2× baseline p95 = "noticeably worse"
WARN_FACTOR        = 1.5   # 1.5× = "getting warm"

# Error is counted if latency > this many seconds (treat as timeout)
TIMEOUT_CAP_S      = 60.0


@dataclass
class StepResult:
    n:          int
    samples:    List[float] = field(default_factory=list)   # latency ms, successes only
    errors:     int = 0
    error_msgs: List[str] = field(default_factory=list)

    @property
    def total(self):  return len(self.samples) + self.errors
    @property
    def success_rate(self): return len(self.samples) / self.total if self.total else 0

    def stat(self, pct):
        if not self.samples: return 0.0
        return float(np.percentile(self.samples, pct))

    @property
    def p50(self):  return self.stat(50)
    @property
    def p90(self):  return self.stat(90)
    @property
    def p95(self):  return self.stat(95)
    @property
    def p99(self):  return self.stat(99)
    @property
    def mean(self): return statistics.mean(self.samples) if self.samples else 0.0
    @property
    def max(self):  return max(self.samples) if self.samples else 0.0


class LadderBenchmark:
    """
    Runs a callable at increasing concurrency levels and finds
    the "magic number" — the safe operational concurrency limit.
    """

    def __init__(self, name: str, steps: List[int] = None,
                 cooldown_s: float = 2.0):
        self.name      = name
        self.steps     = steps or [1, 2, 5, 10, 15, 20, 25, 30, 40, 50]
        self.cooldown  = cooldown_s
        self.results: List[StepResult] = []
        self.started   = datetime.utcnow()

    # ── Run ────────────────────────────────────────────────────────────

    async def run(self, fn: Callable[[int], Awaitable[float]]):
        """
        fn(call_id: int) -> float
        Must return latency in milliseconds on success,
        or raise an exception on failure.
        """
        logger.info("┌─ Ladder: %s", self.name)
        logger.info("│  Steps: %s", self.steps)

        for step_n in self.steps:
            sr = await self._run_step(step_n, fn)
            self.results.append(sr)
            self._log_step(sr)

            if step_n > 1 and self.results:
                if self._should_stop_early(sr):
                    logger.warning("│  ⛔ Early stop: error rate too high at n=%d", step_n)
                    break

            if self.cooldown > 0 and step_n != self.steps[-1]:
                logger.info("│  (cooldown %.1fs)", self.cooldown)
                await asyncio.sleep(self.cooldown)

        logger.info("└─ Done.")
        return self

    async def _run_step(self, n: int, fn) -> StepResult:
        logger.info("│  ── n=%-3d …", n)
        sr = StepResult(n=n)

        async def one_call(call_id):
            t0 = time.perf_counter()
            try:
                latency_ms = await asyncio.wait_for(fn(call_id), timeout=TIMEOUT_CAP_S)
                sr.samples.append(latency_ms if latency_ms is not None
                                  else (time.perf_counter() - t0) * 1000)
            except asyncio.TimeoutError:
                sr.errors += 1
                sr.error_msgs.append(f"call-{call_id}: timeout >{TIMEOUT_CAP_S}s")
            except Exception as e:
                sr.errors += 1
                sr.error_msgs.append(f"call-{call_id}: {type(e).__name__}: {str(e)[:80]}")

        await asyncio.gather(*[one_call(i) for i in range(n)])
        return sr

    def _should_stop_early(self, sr: StepResult) -> bool:
        # Stop if >40% error rate
        return sr.total > 0 and sr.errors / sr.total > 0.40

    def _log_step(self, sr: StepResult):
        flag = ""
        if sr.errors / sr.total > 0.1 if sr.total else False:
            flag = "❌"
        elif self._is_degraded(sr):
            flag = "🔴"
        elif self._is_warm(sr):
            flag = "🟡"
        else:
            flag = "🟢"

        logger.info(
            "│  n=%-3d  p50=%6.0f  p95=%6.0f  p99=%6.0f  max=%6.0f  "
            "errors=%d/%d  %s",
            sr.n, sr.p50, sr.p95, sr.p99, sr.max,
            sr.errors, sr.total, flag
        )

    # ── Magic number analysis ──────────────────────────────────────────

    @property
    def baseline(self) -> Optional[StepResult]:
        return self.results[0] if self.results else None

    def _is_degraded(self, sr: StepResult) -> bool:
        if not self.baseline or self.baseline.p95 == 0:
            return False
        return sr.p95 > self.baseline.p95 * DEGRADATION_FACTOR

    def _is_warm(self, sr: StepResult) -> bool:
        if not self.baseline or self.baseline.p95 == 0:
            return False
        return sr.p95 > self.baseline.p95 * WARN_FACTOR

    @property
    def magic_number(self) -> int:
        """Last step where p95 < baseline * DEGRADATION_FACTOR and errors < 10%."""
        magic = 1
        for sr in self.results:
            ok_latency = not self._is_degraded(sr)
            ok_errors  = (sr.errors / sr.total < 0.10) if sr.total else True
            if ok_latency and ok_errors:
                magic = sr.n
            else:
                break
        return magic

    @property
    def warn_number(self) -> int:
        """Last step before latency starts warming up."""
        warn = 1
        for sr in self.results:
            if not self._is_warm(sr) and (sr.errors / sr.total < 0.05 if sr.total else True):
                warn = sr.n
            else:
                break
        return warn

    # ── Reports ───────────────────────────────────────────────────────

    def ascii_chart(self) -> str:
        """Render a simple bar chart of p95 latency vs concurrency."""
        if not self.results:
            return "(no data)"
        max_lat = max(r.p95 for r in self.results) or 1
        width   = 40
        lines   = [f"{'n':>4}  {'p95 latency':^42}  ms"]
        lines.append("-" * 60)
        for r in self.results:
            bar_len = int(r.p95 / max_lat * width)
            bar     = "█" * bar_len
            flag    = "← MAGIC" if r.n == self.magic_number else ""
            flag    = "← WARN"  if r.n == self.warn_number and r.n != self.magic_number else flag
            lines.append(f"{r.n:>4}  {bar:<42}  {r.p95:>6.0f}  {flag}")
        return "\n".join(lines)

    def save_chart_png(self, path: Path):
        """Save a matplotlib chart (if available)."""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            ns   = [r.n   for r in self.results]
            p50s = [r.p50 for r in self.results]
            p95s = [r.p95 for r in self.results]
            p99s = [r.p99 for r in self.results]

            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
            fig.suptitle(f"{self.name} — Latency vs Concurrency", fontsize=14, fontweight="bold")

            # Latency chart
            ax1.plot(ns, p50s, "o-", color="#4CAF50", label="p50", linewidth=2)
            ax1.plot(ns, p95s, "s-", color="#FF9800", label="p95", linewidth=2)
            ax1.plot(ns, p99s, "^-", color="#F44336", label="p99", linewidth=2)
            if self.baseline:
                ax1.axhline(self.baseline.p95 * DEGRADATION_FACTOR,
                            color="#F44336", linestyle="--", alpha=0.6,
                            label=f"2× baseline p95 ({self.baseline.p95*DEGRADATION_FACTOR:.0f}ms)")
                ax1.axhline(self.baseline.p95 * WARN_FACTOR,
                            color="#FF9800", linestyle="--", alpha=0.6,
                            label=f"1.5× baseline p95 ({self.baseline.p95*WARN_FACTOR:.0f}ms)")
            ax1.axvline(self.magic_number, color="#F44336", linestyle=":",
                        alpha=0.8, label=f"Magic # = {self.magic_number}")
            ax1.axvline(self.warn_number,  color="#FF9800", linestyle=":",
                        alpha=0.8, label=f"Warn # = {self.warn_number}")
            ax1.set_xlabel("Concurrent Requests")
            ax1.set_ylabel("Latency (ms)")
            ax1.set_title("Latency Percentiles")
            ax1.legend(fontsize=8)
            ax1.grid(True, alpha=0.3)

            # Error rate chart
            err_rates = [r.errors / r.total * 100 if r.total else 0 for r in self.results]
            ax2.bar(ns, err_rates, color=["#F44336" if e > 10 else "#4CAF50" for e in err_rates])
            ax2.axhline(10, color="#F44336", linestyle="--", label="10% error threshold")
            ax2.set_xlabel("Concurrent Requests")
            ax2.set_ylabel("Error Rate (%)")
            ax2.set_title("Error Rate vs Concurrency")
            ax2.legend(fontsize=8)
            ax2.grid(True, alpha=0.3)

            plt.tight_layout()
            plt.savefig(str(path), dpi=150, bbox_inches="tight")
            plt.close()
            return True
        except Exception as e:
            logger.warning("Chart PNG failed: %s", e)
            return False

    def to_dict(self) -> dict:
        return {
            "name"         : self.name,
            "run_at"       : self.started.isoformat(),
            "magic_number" : self.magic_number,
            "warn_number"  : self.warn_number,
            "baseline_p95" : self.baseline.p95 if self.baseline else 0,
            "steps"        : [
                {
                    "n"           : r.n,
                    "p50_ms"      : round(r.p50,  1),
                    "p90_ms"      : round(r.p90,  1),
                    "p95_ms"      : round(r.p95,  1),
                    "p99_ms"      : round(r.p99,  1),
                    "max_ms"      : round(r.max,  1),
                    "mean_ms"     : round(r.mean, 1),
                    "success"     : len(r.samples),
                    "errors"      : r.errors,
                    "success_pct" : round(r.success_rate * 100, 1),
                    "status"      : (
                        "🔴 DEGRADED" if self._is_degraded(r) else
                        "🟡 WARM"     if self._is_warm(r)     else
                        "🟢 OK"
                    ),
                }
                for r in self.results
            ],
        }

    def save_report(self, reports_dir: Path, extra_notes: str = "") -> Path:
        from tabulate import tabulate

        ts    = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        slug  = self.name.lower().replace(" ", "_").replace("/", "_")
        md_path  = reports_dir / f"{slug}_{ts}.md"
        json_path = reports_dir / f"{slug}_{ts}.json"
        png_path  = reports_dir / f"{slug}_{ts}.png"

        d = self.to_dict()

        # PNG chart
        chart_line = ""
        if self.save_chart_png(png_path):
            chart_line = f"\n![Latency Chart]({png_path.name})\n"

        # Table
        headers = ["n", "p50", "p90", "p95", "p99", "max", "ok%", "status"]
        rows    = [
            [s["n"], f"{s['p50_ms']}ms", f"{s['p90_ms']}ms",
             f"{s['p95_ms']}ms", f"{s['p99_ms']}ms", f"{s['max_ms']}ms",
             f"{s['success_pct']}%", s["status"]]
            for s in d["steps"]
        ]
        table = tabulate(rows, headers=headers, tablefmt="github")

        md = f"""# {self.name} — Magic Number Report
**Date:** {self.started.strftime('%Y-%m-%d %H:%M UTC')}

## 🎯 Magic Number: `{d['magic_number']}`

| Finding | Value |
|---------|-------|
| **Safe limit (magic #)** | **{d['magic_number']} concurrent** |
| Warn limit | {d['warn_number']} concurrent |
| Baseline p95 | {d['baseline_p95']:.0f} ms |
| Degradation threshold | >{d['baseline_p95']*DEGRADATION_FACTOR:.0f} ms (2× baseline) |

> **Recommendation:** Keep concurrent {self.name} calls ≤ **{d['magic_number']}**.
> Above {d['warn_number']}, latency starts climbing. Above {d['magic_number']}, p95 doubles.
{chart_line}
## Ladder Results

{table}

## ASCII Latency Chart

```
{self.ascii_chart()}
```

## Interpretation

- 🟢 **OK**: p95 within 1.5× baseline — full speed ahead
- 🟡 **WARN**: p95 between 1.5× and 2× baseline — approaching limit
- 🔴 **DEGRADED**: p95 > 2× baseline — throttle here

{extra_notes}

---
*Report: `{md_path.name}`*
"""
        md_path.write_text(md)
        json_path.write_text(json.dumps(d, indent=2))

        logger.info("Report saved: %s", md_path)
        return md_path
