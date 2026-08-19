"""
load_tests/metrics.py  —  Metric collection + Markdown/JSON report generation.
"""
import time, json, statistics, threading
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Sample:
    label: str
    latency_ms: float
    success: bool
    error: Optional[str] = None
    meta: dict = field(default_factory=dict)


class MetricCollector:
    """Thread-safe metric collector for a single test."""

    def __init__(self, test_name: str):
        self.test_name  = test_name
        self.started_at = datetime.utcnow()
        self._lock      = threading.Lock()
        self._samples: List[Sample] = []

    # ── Recording ─────────────────────────────────────────────────────

    def record(self, label: str, latency_ms: float, success: bool,
               error: str = None, **meta):
        with self._lock:
            self._samples.append(Sample(label, latency_ms, success, error, meta))

    def timer(self, label: str, **meta):
        """Context manager that auto-records latency."""
        return _Timer(self, label, meta)

    # ── Analysis ──────────────────────────────────────────────────────

    def summary(self) -> dict:
        with self._lock:
            samples = list(self._samples)

        if not samples:
            return {"error": "no samples"}

        ok  = [s for s in samples if s.success]
        err = [s for s in samples if not s.success]
        lats = sorted(s.latency_ms for s in ok) if ok else [0]

        def pct(lst, p):
            if not lst: return 0
            idx = int(len(lst) * p / 100)
            return lst[min(idx, len(lst)-1)]

        return {
            "test"         : self.test_name,
            "total"        : len(samples),
            "success"      : len(ok),
            "errors"       : len(err),
            "success_rate" : f"{len(ok)/len(samples)*100:.1f}%",
            "latency_ms"   : {
                "min"  : round(min(lats), 1),
                "mean" : round(statistics.mean(lats), 1),
                "p50"  : round(pct(lats, 50), 1),
                "p90"  : round(pct(lats, 90), 1),
                "p95"  : round(pct(lats, 95), 1),
                "p99"  : round(pct(lats, 99), 1),
                "max"  : round(max(lats), 1),
            },
            "errors_detail": [
                {"label": s.label, "error": s.error} for s in err
            ][:10],
        }

    # ── Reporting ─────────────────────────────────────────────────────

    def save_report(self, reports_dir: Path) -> Path:
        s   = self.summary()
        ts  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        slug = self.test_name.lower().replace(" ", "_")

        json_path = reports_dir / f"{slug}_{ts}.json"
        md_path   = reports_dir / f"{slug}_{ts}.md"

        # JSON
        with open(json_path, "w") as f:
            json.dump(s, f, indent=2)

        # Markdown
        lat = s.get("latency_ms", {})
        errs = s.get("errors_detail", [])
        md = f"""# {self.test_name}
**Run:** {self.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}

## Results

| Metric | Value |
|--------|-------|
| Total requests | {s['total']} |
| Successful | {s['success']} |
| Errors | {s['errors']} |
| **Success rate** | **{s['success_rate']}** |

## Latency (ms) — successful requests only

| P50 | P90 | P95 | P99 | Max | Mean |
|-----|-----|-----|-----|-----|------|
| {lat.get('p50')} | {lat.get('p90')} | {lat.get('p95')} | {lat.get('p99')} | {lat.get('max')} | {lat.get('mean')} |

## Error Details
{"None 🎉" if not errs else chr(10).join(f"- `{e['label']}`: {e['error']}" for e in errs)}

---
*Report saved: {md_path.name}*
"""
        with open(md_path, "w") as f:
            f.write(md)

        return md_path

    def print_summary(self):
        s = self.summary()
        lat = s.get("latency_ms", {})
        print(f"\n{'='*60}")
        print(f"  {self.test_name}")
        print(f"{'='*60}")
        print(f"  Total:        {s['total']}")
        print(f"  Success:      {s['success']}  ({s['success_rate']})")
        print(f"  Errors:       {s['errors']}")
        print(f"  Latency p50:  {lat.get('p50')} ms")
        print(f"  Latency p95:  {lat.get('p95')} ms")
        print(f"  Latency max:  {lat.get('max')} ms")
        if s.get("errors_detail"):
            print("  Top errors:")
            for e in s["errors_detail"][:3]:
                print(f"    [{e['label']}] {e['error']}")
        print(f"{'='*60}\n")


class _Timer:
    def __init__(self, collector, label, meta):
        self._c   = collector
        self._lbl = label
        self._meta = meta
        self._t0  = None
        self._err = None

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def fail(self, msg: str):
        self._err = msg

    def __exit__(self, exc_type, exc_val, exc_tb):
        ms  = (time.perf_counter() - self._t0) * 1000
        ok  = exc_type is None and self._err is None
        err = self._err or (str(exc_val) if exc_val else None)
        self._c.record(self._lbl, ms, ok, err, **self._meta)
        return False   # don't suppress exceptions
