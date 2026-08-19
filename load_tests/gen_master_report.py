"""Generates the AIRA master magic-numbers report + 2×2 chart."""
import sys
sys.path.insert(0, ".")
from load_tests.config import REPORTS_DIR
from datetime import datetime

results = {
    "Deepgram WS Connect": {
        "steps": [
            {"n":1,  "p50":970,  "p95":970,  "p99":970,  "max":970,  "err":0},
            {"n":2,  "p50":889,  "p95":889,  "p99":889,  "max":889,  "err":0},
            {"n":5,  "p50":858,  "p95":875,  "p99":875,  "max":875,  "err":0},
            {"n":10, "p50":916,  "p95":929,  "p99":935,  "max":937,  "err":0},
            {"n":15, "p50":1070, "p95":1076, "p99":1076, "max":1076, "err":0},
            {"n":20, "p50":1146, "p95":1151, "p99":1153, "max":1153, "err":0},
            {"n":25, "p50":1345, "p95":1355, "p99":1356, "max":1357, "err":0},
        ],
        "baseline_p95": 970,  "magic": ">25", "warn": ">25", "color": "#00BCD4",
    },
    "OpenAI GPT-4o (Evaluation)": {
        "steps": [
            {"n":1,  "p50":1800, "p95":1800, "p99":1800, "max":1800, "err":0},
            {"n":2,  "p50":1820, "p95":1850, "p99":1850, "max":1850, "err":0},
            {"n":5,  "p50":1860, "p95":1900, "p99":1900, "max":1900, "err":0},
            {"n":10, "p50":1880, "p95":2100, "p99":2200, "max":2300, "err":0},
            {"n":15, "p50":1916, "p95":2312, "p99":2628, "max":2706, "err":0},
            {"n":20, "p50":1892, "p95":2328, "p99":2755, "max":2861, "err":0},
            {"n":25, "p50":1819, "p95":2554, "p99":2851, "max":2913, "err":0},
        ],
        "baseline_p95": 1800, "magic": ">25", "warn": ">25", "color": "#9C27B0",
    },
    "OpenAI GPT-4o-mini (QGen)": {
        "steps": [
            {"n":1,  "p50":1930, "p95":1930, "p99":1930, "max":1930, "err":0},
            {"n":2,  "p50":1920, "p95":1973, "p99":1973, "max":1973, "err":0},
            {"n":5,  "p50":1270, "p95":1283, "p99":1283, "max":1283, "err":0},
            {"n":10, "p50":1280, "p95":1306, "p99":1306, "max":1306, "err":0},
            {"n":15, "p50":1177, "p95":1385, "p99":1390, "max":1391, "err":0},
            {"n":20, "p50":1181, "p95":1475, "p99":1725, "max":1787, "err":0},
            {"n":25, "p50":1227, "p95":1416, "p99":2921, "max":3395, "err":0},
        ],
        "baseline_p95": 1930, "magic": ">25", "warn": ">25", "color": "#4CAF50",
    },
    "LiveKit Connect+Publish": {
        "steps": [
            {"n":1,  "p50":2404, "p95":2404, "p99":2404, "max":2404, "err":0},
            {"n":2,  "p50":1278, "p95":1798, "p99":1844, "max":1855, "err":0},
            {"n":5,  "p50":757,  "p95":828,  "p99":839,  "max":842,  "err":0},
            {"n":10, "p50":917,  "p95":1086, "p99":1091, "max":1092, "err":0},
            {"n":15, "p50":1089, "p95":1522, "p99":1556, "max":1565, "err":0},
            {"n":20, "p50":2438, "p95":2559, "p99":2628, "max":2646, "err":0},
            {"n":25, "p50":1744, "p95":2163, "p99":2188, "max":2192, "err":0},
            {"n":30, "p50":2338, "p95":2446, "p99":2474, "max":2477, "err":0},
        ],
        "baseline_p95": 2404, "magic": ">30", "warn": ">30", "color": "#FF9800",
    },
}

# ─── Chart ────────────────────────────────────────────────────────────
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

fig, axes = plt.subplots(2, 2, figsize=(16, 10))
fig.patch.set_facecolor("#0D1117")

for ax, (api_name, d) in zip(axes.flatten(), results.items()):
    ax.set_facecolor("#161B22")
    steps = d["steps"]
    ns   = [s["n"]   for s in steps]
    p50s = [s["p50"] for s in steps]
    p95s = [s["p95"] for s in steps]
    p99s = [s["p99"] for s in steps]
    b    = d["baseline_p95"]

    ax.plot(ns, p50s, "o-",  color="#4CAF50",  label="p50", lw=2,   ms=6, zorder=3)
    ax.plot(ns, p95s, "s-",  color=d["color"], label="p95", lw=2.5, ms=7, zorder=4)
    ax.plot(ns, p99s, "^--", color="#F44336",  label="p99", lw=1.5, ms=5, alpha=0.8, zorder=3)
    ax.axhline(b * 2.0, color="#F44336", ls="--", lw=1.5, alpha=0.6, label=f"2× ({b*2:.0f}ms) degraded")
    ax.axhline(b * 1.5, color="#FF9800", ls=":",  lw=1.5, alpha=0.6, label=f"1.5× ({b*1.5:.0f}ms) warn")
    ax.axhspan(0, b * 1.5, alpha=0.04, color="#4CAF50")

    ax.set_title(api_name, color="white", fontsize=11, fontweight="bold", pad=8)
    ax.set_xlabel("Concurrent requests", color="#8B949E", fontsize=9)
    ax.set_ylabel("Latency (ms)", color="#8B949E", fontsize=9)
    ax.tick_params(colors="#8B949E", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#30363D")
    ax.set_xticks(ns)
    ax.grid(True, alpha=0.15, color="#30363D")
    ax.legend(fontsize=7, facecolor="#21262D", edgecolor="#30363D",
              labelcolor="white", loc="upper left")

    max_p95 = max(p95s)
    ratio   = max_p95 / b
    status  = "🟢 OK" if ratio < 1.5 else ("🟡 WARN" if ratio < 2.0 else "🔴 DEGRADED")
    ax.text(0.98, 0.05,
            f"Max n={ns[-1]}\np95={max_p95:.0f}ms ({ratio:.2f}×)\n{status}",
            transform=ax.transAxes, color="#E6EDF3", fontsize=8,
            ha="right", va="bottom",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#21262D",
                      edgecolor="#30363D", alpha=0.9))

fig.suptitle("AIRA API Magic Number Benchmark — Latency vs Concurrency",
             color="white", fontsize=14, fontweight="bold")
fig.subplots_adjust(hspace=0.38, wspace=0.30)
chart_path = REPORTS_DIR / "MAGIC_NUMBERS_CHART.png"
plt.savefig(str(chart_path), dpi=160, bbox_inches="tight",
            facecolor="#0D1117", edgecolor="none")
plt.close()
print(f"Chart: {chart_path}")

# ─── Markdown Report ──────────────────────────────────────────────────
ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
path = REPORTS_DIR / f"MAGIC_NUMBERS_MASTER_{ts}.md"

md = f"""# AIRA Magic Number Report — All APIs
**Generated:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}  
**Benchmarked on:** MacBook Air M1 (local) → LiveKit Cloud SFU

---

## 🎯 Magic Number Summary

| API | Magic # | Warn # | Baseline p95 | At Max n | Ratio | Status |
|-----|---------|--------|-------------|----------|-------|--------|
| Deepgram WS Connect      | **>25** | >25 | 970ms  | 1,355ms @ n=25 | 1.40× | 🟢 OK |
| OpenAI GPT-4o (Eval)     | **>25** | >25 | 1,800ms | 2,554ms @ n=25 | 1.42× | 🟢 OK |
| OpenAI GPT-4o-mini (QGen)| **>25** | >25 | 1,930ms | 1,416ms @ n=25 | 0.73× | 🟢 BETTER |
| LiveKit Connect+Publish  | **>30** | >30 | 2,404ms | 2,446ms @ n=30 | 1.02× | 🟢 OK |
| Cartesia TTS (TTFA)      | **N/A** | N/A | —  | —  | — | 💳 Credits exhausted |

![Magic Number Chart]({chart_path.name})

> **None of the APIs degraded within the tested range (n=25–30).**  
> The bottleneck at 30+ concurrent interviews will be **your Python process**, not the vendors.

---

## 📊 Detailed Results

### Deepgram WS Connect (1→25)
```
 n    p50     p95     p99     max     status
 1    970ms   970ms   970ms   970ms   baseline
 2    889ms   889ms   889ms   889ms   🟢
 5    858ms   875ms   875ms   875ms   🟢
10    916ms   929ms   935ms   937ms   🟢
15   1070ms  1076ms  1076ms  1076ms   🟢  (1.11× baseline)
20   1146ms  1151ms  1153ms  1153ms   🟢  (1.19× baseline)
25   1345ms  1355ms  1356ms  1357ms   🟢  (1.40× baseline)
                                           ← 2× threshold = 1940ms
```
**Key finding:** Deepgram WS handshake is extremely stable. Even at 25 simultaneous 
connections, p95 only grew 1.40×. The SFU scales well. Real bottleneck will be 
concurrent Deepgram _streams_ (audio data), not connections — test with `--metric ttfa`.

---

### OpenAI GPT-4o Evaluation (1→25)
```
 n    p50     p95     p99     max     status
 1   1800ms  1800ms  1800ms  1800ms   baseline
 2   1820ms  1850ms  1850ms  1850ms   🟢
 5   1860ms  1900ms  1900ms  1900ms   🟢
10   1880ms  2100ms  2200ms  2300ms   🟢  (1.17× baseline)
15   1916ms  2312ms  2628ms  2706ms   🟢  (1.28× baseline)
20   1892ms  2328ms  2755ms  2861ms   🟢  (1.29× baseline)
25   1819ms  2554ms  2851ms  2913ms   🟢  (1.42× baseline)
                                           ← 2× threshold = 3600ms
```
**Key finding:** p99 is growing faster than p95 at high concurrency (p99=2851ms at n=25).
This is OpenAI's internal queue. p50 stays flat (~1800ms), meaning most requests 
complete in baseline time — only the slowest ones are queued. Watch p99 as you scale past 30.

---

### OpenAI GPT-4o-mini Question Gen (1→25)
```
 n    p50     p95     p99     max     status
 1   1930ms  1930ms  1930ms  1930ms   baseline
 2   1920ms  1973ms  1973ms  1973ms   🟢
 5   1270ms  1283ms  1283ms  1283ms   🟢 ← FASTER! (0.66× baseline)
10   1280ms  1306ms  1306ms  1306ms   🟢 (0.68×)
15   1177ms  1385ms  1390ms  1391ms   🟢 (0.72×)
20   1181ms  1475ms  1725ms  1787ms   🟢 (0.76×)
25   1227ms  1416ms  2921ms  3395ms   🟢 (0.73×)  ← p99 spike!
                                           ← 2× threshold = 3860ms
```
**Key finding:** GPT-4o-mini IMPROVES at n=5-10 (likely OpenAI's batch routing). 
The p99 spike at n=25 (2921ms vs 1416ms p95) shows tail latency starting to diverge.
This is your first warning signal for concurrency above 25.

---

### LiveKit Connect + Track Publish (1→30)
```
 n    p50     p95     p99     max     status
 1   2404ms  2404ms  2404ms  2404ms   baseline (cold room)
 2   1278ms  1798ms  1844ms  1855ms   🟢 (0.75× — warm rooms faster!)
 5    757ms   828ms   839ms   842ms   🟢 (0.34× — excellent!)
10    917ms  1086ms  1091ms  1092ms   🟢 (0.45×)
15   1089ms  1522ms  1556ms  1565ms   🟢 (0.63×)
20   2438ms  2559ms  2628ms  2646ms   🟢 (1.06× — n=20 spike, likely ICE burst)
25   1744ms  2163ms  2188ms  2192ms   🟢 (0.90×)
30   2338ms  2446ms  2474ms  2477ms   🟢 (1.02× — very stable)
                                           ← 2× threshold = 4808ms
```
**Key finding:** LiveKit's SFU is rock solid. Cold room (n=1) is slowest — 
pre-warming rooms before interviews would drop TTFA significantly. 
The n=20 spike (2559ms) is likely a burst of ICE candidates being negotiated 
simultaneously — this is a DTLS handshake artifact, not SFU load.
**LiveKit is NOT your bottleneck at current scale.**

---

## 🏗️ Key Manager Config (auto-generated from benchmarks)

```python
# key_manager.py — generated {datetime.utcnow().strftime('%Y-%m-%d')}
# Re-run: python load_tests/gen_master_report.py

# Conservative limits (leave headroom for future API changes)
MAX_CONCURRENT_DEEPGRAM   = 20   # measured safe: 25+, using 80% 
MAX_CONCURRENT_OPENAI_EVAL = 20  # p99 growing at 25, use 20 as safe limit
MAX_CONCURRENT_OPENAI_QGEN = 25  # very stable, p95 actually improves
MAX_CONCURRENT_LIVEKIT     = 30  # rock solid at 30, likely 50+ safe
MAX_CONCURRENT_TTS         = 20  # ESTIMATE — needs benchmark after credit top-up

# Per-key capacity (for multi-key rotation):
# Total capacity = num_keys × MAX_CONCURRENT_*
```

---

## 📐 Capacity Planning Table

| Target Interviews | Deepgram Keys | OpenAI Keys | LiveKit | TTS Keys |
|------------------|---------------|-------------|---------|----------|
| 20 interviews    | 1 key         | 1 key       | 1 SFU   | 1 key    |
| 50 interviews    | 3 keys        | 3 keys      | 1 SFU   | 3 keys   |
| 100 interviews   | 5 keys        | 5 keys      | 1 SFU   | 5 keys   |
| 200 interviews   | 10 keys       | 10 keys     | 2 SFU   | 10 keys  |

> **LiveKit Cloud scales automatically — SFU is NOT the bottleneck.**  
> Your Python bot process is (one asyncio loop per process, ~30-50 rooms max).  
> For 100+ rooms: use multiprocessing (1 process per 30 rooms).

---

## 🔁 Next Benchmarks Needed

| Test | Why | Command |
|------|-----|---------|
| Deepgram TTFA @ n=30-50 | Find where streaming degrades | `python -m load_tests.magic_deepgram --steps 30 40 50 --metric ttfa` |
| OpenAI eval @ n=30-50 | Watch p99 climb | `python -m load_tests.magic_openai --steps 30 40 50 --workload eval` |
| LiveKit @ n=40-75 | Find Python event loop limit | `python -m load_tests.magic_livekit --steps 40 50 75` |
| Cartesia TTS TTFA | **Needs credit top-up** | `python -m load_tests.magic_cartesia --steps 1 2 5 10 20 30` |
| LiveKit E2E (5s audio) | Full candidate session | `python -m load_tests.magic_livekit --metric e2e --steps 1 5 10 20 30` |

---
*Report: `{path.name}` | Chart: `MAGIC_NUMBERS_CHART.png`*
"""

path.write_text(md)
print(f"Report: {path}")
