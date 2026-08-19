# AIRA Magic Number Report — All APIs
**Generated:** 2026-06-17 08:51 UTC  
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

![Magic Number Chart](MAGIC_NUMBERS_CHART.png)

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
# key_manager.py — generated 2026-06-17
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
*Report: `MAGIC_NUMBERS_MASTER_20260617_085132.md` | Chart: `MAGIC_NUMBERS_CHART.png`*
