# REPO BRAIN — AIRA Engine (AI Recruitment Assistant)
> Last updated: 2026-06-23
> Auto-maintained by Claude Code. Update this file immediately after any critical change.
> ⚠️ The root `README.md` is largely aspirational/inaccurate (describes GraphQL, Celery, Docker/K8s, Prometheus, "89% coverage" that DO NOT exist). Trust THIS document over the README.

---

## 1. Project Identity
- **What this project is**: An autonomous, end-to-end AI interviewer. It pulls candidates/jobs from the JobDiva ATS, schedules video interviews, runs an AI-led **voice interview** (LiveKit native bot), scores answers, detects AI-assisted/cheating signals, generates HTML→PDF reports, and emails recruiters.
- **Tech stack**:
  - **Language**: Python 3.12/3.13 (backend). React 18 + Vite (Demo_Launcher frontend only).
  - **Web/API**: FastAPI ([app.py](app.py), [Demo_Launcher/backend/demo_launcher.py](Demo_Launcher/backend/demo_launcher.py)). Served via Gunicorn + UvicornWorker.
  - **DB**: MySQL/MariaDB via SQLAlchemy 2.0 ORM + PyMySQL. Dual-mode: direct DB on Mac, HTTP→SpringBoot Engine API on Linux.
  - **AI**: OpenAI GPT-4o / GPT-4o-mini (planning, scoring, semantic analysis). Deepgram nova-2 (STT). Cartesia (TTS, primary) / ElevenLabs / OpenAI TTS (fallback).
  - **Media**: LiveKit SFU (current voice transport, native Python bot — no browser). Selenium + Zoom (legacy fallback paths).
  - **Reporting**: Jinja2 templates → Playwright (headless Chromium) HTML→PDF. pdfkit fallback.
  - **Email/Auth**: MSAL (Azure AD / MS365), yagmail, SMTP. ICS for calendar invites.
  - **CV**: OpenCV DNN (Caffe face-detection models) for face extraction.
- **Architecture style**: Process-oriented monolith. A FastAPI orchestrator spawns one OS process per concurrent interview; each process runs a synchronous interview engine bridged to an async LiveKit bot. NOT microservices despite README claims.
- **Deployment**: Linux VMs (EC2/bare metal) via systemd services + nginx reverse proxy. `JenkinsFile` is a stub (echoes a build message only). Demo_Launcher has its own `deploy.sh`. Local dev runs on Mac with direct MySQL.

---

## 2. Directory Map

```
AIRA_ENGINE_REPORT/
├── app.py                          # FastAPI orchestration API — job/interview control plane
├── aira_1_ats_integration*.py      # JobDiva ATS sync (base / _India / _US variants — heavy duplication)
├── aira_2_zoom_interview_reminder.py  # Pre-interview reminder dispatcher
├── aira_3_webrtc_orchestration.py  # ★ CURRENT main interview orchestrator (LiveKit path)
├── aira_3_zoom_orchestration.py    # Legacy Zoom orchestrator
├── aira_4_feedback.py              # Post-interview feedback collection
├── aira_bot_client.py              # ★ Native LiveKit bot client (audio I/O, Deepgram WS)
├── bot_audio_adapter.py            # Sync↔async bridge: webrtc_interview ↔ AiraBotClient
├── cartesia_tts.py                 # Cartesia streaming TTS (primary)
├── webrtc_interview.py             # ★ MONOLITH (9,319 lines) — core Q/A loop, audio, eval, HR flow
├── zoom_interview.py               # Legacy Zoom interview engine
├── responses_client.py             # Question planner + answer evaluator (158KB)
├── aira_sql.py                     # ★ DB layer: 16 SQLAlchemy models + dual-mode DAO (122KB)
├── MSEmailClient.py                # Email client + HTML→PDF (Playwright) (65KB)
├── interview_template_handler.py   # Report data assembly + Jinja2 rendering
├── sentinel_report_data_processor.py  # Sentinel monitoring report builder
├── env_config.py                   # ★ Single source of truth for AIRA_ENV + service URLs
├── config_loader.py / config.json  # App config (emails, thresholds, skill ratios)
├── interview_config.py             # InterviewConfig dataclass + adaptive engine
├── hr_interview_*.py               # HR behavioral interview engine + config
├── evaluation_engine.py            # Per-answer LLM scoring
├── reflex_engine.py / difficulty_policy.py / skill_adaptation.py  # Adaptive difficulty
├── resume_matcher.py               # TFIDF + sentence-transformers resume↔JD matching
├── livekit_egress.py / local_recorder.py / livekit_bot_presence.py  # Recording + presence
│
├── answer_analysis/                # ★ AI-assistance / authenticity detection package
│   ├── analyze_answer.py           #   per-answer entry point
│   ├── semantic_analyzer.py        #   GPT-4o disfluency/polish analysis (primary signal)
│   ├── answer_scoring.py           #   authenticity signal (0-1)
│   ├── aggregator.py               #   multi-signal aggregation → confidence band
│   ├── session_store.py            #   append-only JSON session persistence
│   ├── llm_interpreter.py          #   optional structural-regularity LLM pass
│   └── models.py, *.py             #   feature extraction, temporal/continuity analysis
├── answer_analysis_integration.py  # Async fire-and-forget trigger from interview engine
│
├── database/                       # schema.sql, schema_updates.sql, migrations/
├── migrations/                     # mandatory-skills migration (SQL + Python runner)
├── calender_v1/google_sync/        # Google Calendar sync (service account)
│
├── Demo_Launcher/                  # ★ SEPARATE app — demo booking portal
│   ├── backend/demo_launcher.py    #   FastAPI (Sentry, Prometheus, rate limiting)
│   ├── frontend/                   #   React + Vite SPA
│   └── infrastructure/, *.sh       #   systemd units, nginx confs, deploy scripts
│
├── *.html.j2 / *.pebble            # Report templates (Jinja2 active; .pebble = DEAD code)
├── email_*.html                    # 17 email templates
├── models/ + *.caffemodel + deploy.prototxt  # OpenCV face-detection models
├── load_tests/ + tests/            # Load harness + pytest suites
├── archive/                        # Legacy docs, sample outputs, old utilities
├── recordings/ best_faces/ captured_tiles/ sessions/ logs/ reports/ interview_reports/  # Runtime artifacts (mostly gitignored)
└── temp.py, temp3.py, boom_bhoom.txt  # ⚠️ junk — should be deleted
```

---

## 3. Critical Files Reference

| File | Purpose | Key symbols / entry points |
|------|---------|----------------------------|
| [app.py](app.py) | FastAPI control plane — start/stop jobs, query live/upcoming/history | `app`, `/jobs/*`, `/api/interviews/*` |
| [aira_3_webrtc_orchestration.py](aira_3_webrtc_orchestration.py) | Current main orchestrator; claims interview, picks LiveKit vs Selenium, runs lifecycle | `orchestrate_interview()` (L764), `_claim_ready_interviews()` (L1287) |
| [webrtc_interview.py](webrtc_interview.py) | Monolithic engine: Q/A loop, audio, eval, HR flow | `ask_questions_in_webrtc()` (L4663) |
| [aira_bot_client.py](aira_bot_client.py) | Native LiveKit bot: PCM capture, VAD, Deepgram WS, audio publish | `AiraBotClient`, `record_audio()` |
| [bot_audio_adapter.py](bot_audio_adapter.py) | Bridges sync engine to async bot | `create_adapter_from_env()` (L298), `record_transcribe()`, `speak_cartesia()` |
| [responses_client.py](responses_client.py) | Question planning, difficulty scaling, answer eval | `ResponsesClient` |
| [aira_sql.py](aira_sql.py) | DB layer; 16 ORM models; dual local/API mode | `_api_call()` (L37), `generate_authorship_summary()` (L1989), `insert_or_get_*`, `update_interview()` |
| [answer_analysis/analyze_answer.py](answer_analysis/analyze_answer.py) | Per-answer authenticity analysis | `analyze_answer()` (L10) |
| [answer_analysis/semantic_analyzer.py](answer_analysis/semantic_analyzer.py) | GPT-4o human-vs-AI signal | `analyze_semantic_deviation()` (L127) |
| [interview_template_handler.py](interview_template_handler.py) | Report data assembly + Jinja2 render | `InterviewReportTemplate.render()` (~L1090) |
| [MSEmailClient.py](MSEmailClient.py) | Email + HTML→PDF (Playwright) | `convert_interview_html_to_pdf()` (L336), `send_interview_email()` (L474) |
| [env_config.py](env_config.py) | Env detection + service URLs (single source of truth) | `AIRA_ENV`, `ENGINE_API_BASE`, `CONNECT_BASE_URL` |
| [aira_1_ats_integration_US.py](aira_1_ats_integration_US.py) | JobDiva US sync (timezone-aware) | OAuth → token, `log_jobdiva_note()` |
| [cartesia_tts.py](cartesia_tts.py) | Streaming TTS | `cartesia_speak_text_streaming()` (L53) |

---

## 4. Architecture & Data Flow

```
                      ┌─────────────────────────────────────────────────────┐
   JobDiva ATS ──────▶│ aira_1_ats_integration_{US,India}.py  (cron-style)   │
   (OAuth REST)       │  fetch candidates+jobs → insert into DB → schedule   │
                      └───────────────────┬─────────────────────────────────┘
                                          ▼
                      ┌───────────────────────────────────┐
                      │ scheduleZoomMeeting / Connect API  │ → email invite (MSEmailClient + ICS)
                      └───────────────────┬───────────────┘
                                          ▼
   app.py (FastAPI) ──spawns process──▶ aira_3_webrtc_orchestration.orchestrate_interview()
                                          │
                  AIRA_USE_LIVEKIT=true ──┤── else ──▶ Selenium Chrome (legacy WebRTC) / Zoom
                                          ▼
                      ┌──────────────────────────────────────────────────────┐
                      │ webrtc_interview.ask_questions_in_webrtc()            │
                      │   loop: plan Q (responses_client) → TTS (Cartesia)    │
                      │         → capture answer (LiveKit→Deepgram STT)       │
                      │         → evaluate (evaluation_engine, GPT-4o-mini)   │
                      │         → fire async authenticity analysis ───────────┼──▶ answer_analysis/*
                      │   bot driven by aira_bot_client via bot_audio_adapter │      → sessions/*.json
                      └───────────────────┬──────────────────────────────────┘
                                          ▼ (interview ends)
   generate_authorship_summary (aira_sql) ─▶ InterviewReportTemplate.render (Jinja2 HTML)
                                          ─▶ MSEmailClient HTML→PDF (Playwright) ─▶ email recruiter
                                          ─▶ persist scores/Q&A/report to DB
```

- **Request handling**: `app.py` exposes a control API; the real work runs in spawned OS processes (`AIRA_MAX_PARALLEL` concurrency). There is no per-HTTP-request interview handling — interviews are long-lived background processes.
- **Auth/sessions**: No user auth on the engine API itself. Inter-service calls use `AIRA_API_KEY` / `AIRA_BOT_JWT`. Demo_Launcher backend uses CORS + slowapi rate limiting. Microsoft 365 email uses MSAL OAuth. JobDiva uses OAuth (basic → bearer). Zoom uses OAuth.
- **DB access**: All through `aira_sql.py`. `_IS_LOCAL = platform.system()=="Darwin"` → direct SQLAlchemy on Mac, else HTTP to `ENGINE_API_BASE` (SpringBoot). ⚠️ fragile prod/dev schism.
- **Background work**: ATS sync runs on a cron-like timer (`CRON_JOB_TIMING`). Answer analysis runs in daemon threads (`answer_analysis_integration.py`). Recording via `local_recorder.py` or `livekit_egress.py` (S3).
- **External services**: see §9.
- **"Sessions"**: per-answer authenticity signals are appended to `sessions/session_*.json` (atomic writes), later aggregated into the report.

---

## 5. Database / Data Models
MySQL/MariaDB. 16 SQLAlchemy models in [aira_sql.py](aira_sql.py) (line refs in parens). Base schema in [database/schema.sql](database/schema.sql).

- **Candidate** (L115): id, candidate_jd_id, email (unique). Has many Interviews.
- **Job** (L125): id, job_jd_ref_number (unique), job_jd_id (unique), job_title, job_description (LONGTEXT), company_name, job_status. Has many Interviews.
- **Interview** (L140): id, candidate_id (FK), job_id (FK) — **unique(candidate_id, job_id)**; interview_time, interview_timezone, interview_status (string, not enum), resume_match_score, interview_score, zoom_host_url/join_url, is_scheduled/is_email_sent/is_reminder_email_sent/is_jd_*_updated flags.
- **InterviewQuestion** (L275) / **interviewqa** table: interview_id (FK), question, answer, analysis, score.
- **InterviewAnalysis** (L188), **InterviewTimeline** (L201), **InterviewHistory** (L214): analysis blocks, conversation timeline, historical snapshots.
- **Recruiter** (L261): recruiter_jd_id, recruiter_name, is_active.
- **InterviewProject** (L304): projects extracted from answers.
- **CandidateSkillProfile** (L329): per-candidate skill baselines (EMA-updated).
- **EvaluationDimension** (L351) / **InterviewDimension** (L364): scoring dimensions attached to interviews.
- **InterviewEvaluationReport** (L378) / **InterviewEvaluationResult** (L391): structured eval outputs.
- **InterviewReportData** (L411) / **HRInterviewReportData** (L473): persisted report payloads for regeneration.
- **interviews_log** table: audit trail populated by triggers on the interviews table.

---

## 6. API / Route Map

### Engine orchestration API — [app.py](app.py) (FastAPI, title "AIRA Interview Orchestration APIs")
| Method | Path | What it does |
|--------|------|--------------|
| POST | `/jobs/{job_id}/start` | Start a named orchestration job |
| POST | `/jobs/{job_id}/stop` | Stop a job |
| GET | `/jobs/{job_id}/status` | Status of one job |
| GET | `/jobs/process-tree` | Inspect spawned process tree |
| POST | `/jobs/start-all` | Start all jobs |
| POST | `/jobs/stop-all` | Stop all jobs |
| GET | `/jobs/status-all` | Status of all jobs |
| GET | `/api/interviews/live` | Currently running interviews |
| GET | `/api/interviews/upcoming` | Scheduled interviews |
| GET | `/api/interviews/history` | Completed interviews |
| GET | `/api/interviews/summary` | Aggregate summary |
| GET | `/api/system/health` | Health check |

### Demo Launcher backend — [Demo_Launcher/backend/demo_launcher.py](Demo_Launcher/backend/demo_launcher.py) (separate app)
| Method | Path | What it does |
|--------|------|--------------|
| GET | `/health` | Health check |
| GET | `/` | Root |
| GET | `/api/direct/demo/jobs` | List demo jobs |
| GET | `/api/direct/demo/job/{job_id}` | Get one job |
| POST | `/api/direct/demo/create` | Create a demo interview |
| POST | `/api/direct/demo/start/{interview_id}` | Start a demo interview |
| GET | `/api/direct/demo/status/{interview_id}` | Demo interview status |

> Note: external services consumed (not defined here): Connect room API (`/api/rooms/`, `/api/meeting/`, `/api/proctor-log/`), SpringBoot Engine API (`ENGINE_API_BASE`), Aira Hub slots, Echo screening API.

---

## 7. Environment Variables
Set in `.env` (⚠️ currently committed to git — see §10). Values resolved in [env_config.py](env_config.py) and inline `os.getenv` across modules.

| Variable | Required | Description |
|----------|----------|-------------|
| `AIRA_ENV` | ✅ | `production` / `development` — drives all URL/timing defaults |
| `OPENAI_API_KEY` | ✅ | OpenAI (planning, scoring, semantic analysis) |
| `OPENAI_MODEL` | ⚠️ | Override default model |
| `DEEPGRAM_API_KEY` | ✅ | Speech-to-text |
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | ✅ (LiveKit path) | SFU media server + token gen |
| `AIRA_USE_LIVEKIT` | ⚠️ | `true` → native bot; else Selenium/Zoom fallback |
| `CARTESIA_API_KEY` / `CARTESIA_VOICE_ID` / `CARTESIA_MODEL` / `CARTESIA_SPEED` | ⚠️ | Primary TTS (falls back to OpenAI TTS if unset) |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | ⚠️ | Legacy TTS |
| `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_USER` / `MYSQL_PASS` / `MYSQL_DB` | ✅ (local/Mac) | Direct DB connection |
| `ENGINE_API_BASE` | ✅ (server/Linux) | SpringBoot DB API when not local |
| `SENTINEL_BASE_URL` / `CONNECT_BASE_URL` / `AIRA_SCHEDULER_URL` / `AIRA_HUB_URL` / `AIRA_HUB_SLOTS_URL` / `ECHO_API_BASE` | ✅ | Inter-service URLs |
| `AIRA_API_KEY` / `ADMIN_API_KEY` / `AIRA_BOT_JWT` | ✅ | Inter-service / bot auth |
| `JOBDIVA_CLIENT_ID` / `JOBDIVA_USERNAME` / `JOBDIVA_PASSWORD` / `JOBDIVA_API_BASE` / `JOBDIVA_AUTH_URL` / `JOBDIVA_TIMEZONE` | ✅ (ATS) | JobDiva India/base creds |
| `US_JOBDIVA_CLIENT_ID` / `US_JOBDIVA_USERNAME` / `US_JOBDIVA_PASSWORD` | ✅ (US ATS) | JobDiva US creds |
| `ZOOM_CLIENT_ID` / `ZOOM_CLIENT_SECRET` / `ZOOM_ACCOUNT_ID` / `ZOOM_USER_ID` | ⚠️ (legacy) | Zoom OAuth |
| `EMAIL_SENDER` / `EMAIL_PASSWORD` / `EMAIL_RECIPIENT` | ✅ | SMTP email |
| (MSAL) `MS_EMAIL_CLIENT_ID` / `MS_EMAIL_CLIENT_SECRET` / `MS_EMAIL_TENANT_ID` | ⚠️ | Microsoft 365 email |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `EGRESS_S3_BUCKET` / `EGRESS_S3_REGION` / `EGRESS_S3_PREFIX` | ⚠️ | LiveKit Egress recording to S3 |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_NUMBER` | ⚠️ | Telephony (Echo screening) |
| `AIRA_MAX_PARALLEL` | ⚠️ | Max concurrent interview processes |
| `CARTESIA_API_KEY` / `DEEPGRAM_API_KEY` / `OPENAI_API_KEY` | — | **Now accept a comma-separated LIST** of keys for the api_key_pool (single key still works) |
| `CARTESIA_MAX_CONCURRENCY` / `DEEPGRAM_MAX_CONCURRENCY` / `OPENAI_MAX_CONCURRENCY` | ⚠️ | Per-key concurrency ceiling ("magic number"). Defaults 5/5/10 |
| `AIRA_KEYPOOL_DIR` | ⚠️ | Shared state dir for cross-process key counters (default `<tmp>/aira_keypool`) |
| `AIRA_KEYPOOL_MAX_WAIT` / `AIRA_KEYPOOL_LEASE_TTL` / `AIRA_KEYPOOL_POLL` | ⚠️ | Keypool: saturation safety-valve secs (180), leaked-lease reclaim TTL (1800), poll interval (0.25) |
| `CRON_JOB_TIMING` | ⚠️ | ATS poll interval (30s prod / 60s dev) |
| `RECORDINGS_DIR` | ⚠️ | Local WAV output dir |
| `NUMBER_OF_QUESTIONS` / `RESUME_QUESTION_PERCENTAGE` / `MAX_INTERVIEW_DURATION_MINUTES` | ⚠️ | Interview shape |
| `DG_MODEL` / `DG_STREAMING` / `DG_*` | ⚠️ | Deepgram tuning |
| `REFLEX_MODEL` / `REFLEX_TEMPERATURE` / `REFLEX_MAX_TOKENS` / `ENABLE_LLM_INTERPRETER` | ⚠️ | Adaptive engine / analysis toggles |

---

## 8. Key Patterns & Conventions
- **Process-per-interview**: orchestrator spawns OS processes; concurrency capped by `AIRA_MAX_PARALLEL`. State coordinated via DB + JSON session files, not shared memory.
- **Feature-flag transport**: `AIRA_USE_LIVEKIT` and `CARTESIA_API_KEY` swap transport/TTS without code changes. LiveKit (current) vs Selenium/Zoom (legacy) coexist.
- **Sync↔async bridge**: the giant synchronous `webrtc_interview.py` talks to the async LiveKit bot only through [bot_audio_adapter.py](bot_audio_adapter.py) (runs async coroutines on a background event loop). Keep new bot interactions behind this adapter.
- **DB access dual-mode**: never hit MySQL directly outside `aira_sql.py`. It auto-routes local-DB vs API by platform.
- **Config indirection**: read service URLs/env flags from [env_config.py](env_config.py), not raw `os.getenv`, where one already exists.
- **Authenticity wording discipline**: answer-analysis code deliberately avoids accusatory language ("ai-generated", "plagiarized" are prohibited output terms — see [llm_interpreter.py](answer_analysis/llm_interpreter.py) L136-149). It reports an "assistance signal," not an accusation. Preserve this framing.
- **LLM safety fallbacks**: every LLM call has a safe default (e.g. `evaluate_answer` returns 0.5 on failure). Maintain this.
- **Session persistence**: append-only atomic JSON writes in [session_store.py](answer_analysis/session_store.py).
- **Reports**: Jinja2 (`*.html.j2`) is the active template engine; `*.pebble` files are dead. HTML→PDF via Playwright (headless Chromium), pdfkit fallback.
- **Naming**: pipeline scripts prefixed `aira_<N>_<stage>.py`. Regional ATS variants suffixed `_US` / `_India`. No import-path aliasing (flat module layout, all top-level).
- **Testing**: pytest under `tests/` (unit/integration/scoring/pdf) plus many root-level `test_*.py` and `verify_*.py` ad-hoc scripts. Coverage is NOT 89% (README claim is false).

---

## 9. External Dependencies & Integrations
| Service | Used for | Configured in | Gotchas |
|---------|----------|---------------|---------|
| **OpenAI** | Question planning, answer eval (gpt-4o-mini), semantic authenticity (gpt-4o) | `OPENAI_API_KEY`, scattered clients | Cost/throughput; temp 0.1 for analysis; JSON mode enforced |
| **Deepgram** | STT (nova-2) over WebSocket | `aira_bot_client.py` (URL hardcoded L34-38) | No STT abstraction; model/lang/rate fixed |
| **Cartesia** | TTS streaming (~300ms TTFA) | `cartesia_tts.py` | Falls back to OpenAI TTS if key unset |
| **ElevenLabs** | Legacy TTS | requirements + env | Largely superseded |
| **LiveKit** | SFU audio transport + native bot | `bot_audio_adapter.create_adapter_from_env()` | Token from Connect server or local gen; Egress optional |
| **Aira-Connect** | Room signaling, JWT, presence (Socket.IO), proctor log | `CONNECT_BASE_URL`, `livekit_bot_presence.py` | Dual presence verification (Socket.IO + participant poll) |
| **JobDiva ATS** | Candidate/job ingest, note logging | `aira_1_ats_integration*.py` | OAuth basic→bearer; separate US vs India creds; 3 duplicated impls |
| **Zoom** | Legacy meeting transport | `scheduleZoomMeeting.py`, `zoom_interview.py` | OAuth; legacy path |
| **Microsoft 365 (MSAL)** | Transactional email | `MSEmailClient.py` | OAuth client-credentials; Azure AD app |
| **SMTP / yagmail** | Email send | `MSEmailClient.py`, env | App password in `.env` |
| **AWS S3** | Egress recording storage | `livekit_egress.py` | Optional |
| **Twilio** | Telephonic screening (Echo) | env | Optional |
| **Google Calendar** | Calendar sync | `calender_v1/google_sync/` | Uses committed `service_account.json` ⚠️ |
| **SpringBoot Engine API** | DB ops in server mode | `ENGINE_API_BASE` | Only used when not on Mac |
| **Sentry / Prometheus / slowapi** | Demo_Launcher observability + rate limit | `Demo_Launcher/backend/` | Only in the demo app, not the engine |

---

## 10. Known Issues / Tech Debt / TODOs
- 🚨 **SECRETS COMMITTED TO GIT**: `.env` is tracked (in index AND history) despite being in `.gitignore` (gitignore is a no-op once tracked). Exposes OpenAI/Deepgram/ElevenLabs/LiveKit/Cartesia keys, JobDiva US+India creds, Zoom OAuth, Azure AD client secret, a GitHub PAT, Gmail app password, MySQL root password. Also `calender_v1/google_sync/service_account.json` is committed. **Action: rotate ALL credentials, then `git rm --cached .env` + scrub history.**
- **Monster files**: `webrtc_interview.py` (9,319 lines / 490KB), `responses_client.py` (158KB), `aira_sql.py` (122KB), `MSEmailClient.py` (65KB). Hard to navigate/test.
- **Junk in repo root**: `temp.py` (205KB), `temp3.py`, `boom_bhoom.txt`, committed `.log`/`.logs` files, `image.png`, sample reports, 22MB+ Caffe models — bloat the repo. Move models to LFS/external; delete temp files.
- **3× ATS duplication**: base / `_India` / `_US` are ~2,400 lines each, mostly copy-paste (~44% overlap). Should be one config-driven module.
- **4 overlapping TTS entry points** in `webrtc_interview.py` (`ai_speak`, `ai_speak_with_audio`, `ai_speak_streamed_text`, `ai_speak_streaming`) — unclear canonical path.
- **Dead code**: `*.pebble` templates never imported; duplicate `_generate_explanations()` in `answer_analysis/aggregator.py`. (`requirements.txt` duplicate block fixed 2026-06-23.)
- **DB dual-mode fragility**: `platform.system()=="Darwin"` decides direct-DB vs API — environment-coupled, surprising in prod.
- **No real CI**: `JenkinsFile` is a one-line stub.
- **README is fiction**: documents GraphQL/Celery/Docker/K8s/Prometheus/89%-coverage that don't exist.
- **TODO/FIXME/HACK markers**: only ~7 occurrences (all in `zoom_interview.py`) — low inline-TODO count, but large architectural debt above.
- **Hardcoded values**: emails in `config.json`; Deepgram WS params in `aira_bot_client.py`; CORS origins in Demo_Launcher backend.

---

## 11. Changelog (Critical Changes Log)
| Date | Change | Files affected |
|------|--------|----------------|
| 2026-06-23 | Initial repository analysis; created REPO_BRAIN.md | All (analysis only) |
| 2026-06-23 | Cleaned requirements.txt: removed duplicated dependency block (was lines ~114-186), added missing `cartesia` (imported by cartesia_tts.py), moved `spacy` under NLP | requirements.txt |
| 2026-06-23 | requirements.txt: added missing `fastapi` (imported by app.py) + `urllib3` (ATS files). Verified livekit/livekit-api cover all livekit.* imports. matplotlib/tabulate belong to load_tests only; flask/dateutil/twilio only in temp.py (junk) — intentionally not added | requirements.txt |
| 2026-06-24 | Recording rewritten as single wall-clock pacer (silence/black gap-fill, equal-length tracks); start moved to bot-join, stop at bot-exit. Connect-side recorder now redundant | local_recorder.py, aira_3_webrtc_orchestration.py |
| 2026-06-24 | STT: fixed ~800ms audio-drop after bot speech (DG queue created before handshake); pre-open DG socket during bot speech w/ KeepAlive + adopt at record time (`prewarm_dg`/`_dg_keepalive`/`_ws_open`) | aira_bot_client.py |
| 2026-06-24 | Closing flow: merged double "thank you" — suppress goodbye ack when Phase 4 Q&A runs (kickoff is single closing msg) | webrtc_interview.py |
| 2026-06-24 | Question variety: rebalanced format weights (was 17:3 scenario:conceptual) + downweight whole design-a-system family after one | responses_client.py |
| 2026-06-24 | Latency: enabled streaming-ack pipeline (was dead — `use_streaming_ack` never True) for live Cartesia acks → token→Cartesia instead of full-text. NEEDS LIVE TEST (pipeline never ran in prod) | webrtc_interview.py |
| 2026-06-27 | Best-face REPORT fix: report renderer (interview_template_handler._get_candidate_photo) was reading the DB-download path and ignoring the engine's local file → blank photo. Now prefers images/<room_id>_best_face.png (engine-written live), falls back to DB path for regen. Verified: returns real face base64 (136KB) not placeholder. Also: load harness now sets interview_config (questions_per_round + max_duration) per the sim so the bot count matches. KNOWN: sim end-to-end flaky — candidate leaves → bot's 5-min absent-wait loop → harness kills engine before report-gen (sim lifecycle issue, not report bug) | interview_template_handler.py, load_tests/run_load_sim.py |
| 2026-06-27 | Best-face capture strategy → two-tier: BASELINE = best face in first 20s of presence (auto, presence-driven); ANSWER = best face while answering Q1 & Q2 (engine opens window at recording-start, current_question_no<=2). best_face = answer face if any, else baseline. Throttled ~2.5 detections/s. Replaced the after-ack triggers. Logic unit-tested | face_detector.py, webrtc_interview.py |
| 2026-06-27 | Best-face: fixed capture on small frames (LiveKit downscales to 240x180; minSize 80→frame-relative ~40). Storage redesigned: engine writes `images/<room_id>_best_face.png` during interview → report embeds local file (no DB download) → persisted to DB via hub API (`update_interview` base64) right before `archive_and_delete_interview`. Removed mid-flow DB save. Verified on 240x180. (Old Sentinel path in MSEmailClient still runs+fails harmlessly on missing captured_tiles — cleanup pending) | face_detector.py, webrtc_interview.py, aira_3_webrtc_orchestration.py |
| 2026-06-27 | Best-face capture moved from Sentinel into the engine. FaceDetector now accumulates best candidate face (event-driven, after each ack — NOT the 2s presence loop); scored by area×sharpness. Wired: webrtc_interview `_capture_face_after_ack()` at 3 ack sites → bot video loop `maybe_capture_best_face()` → orchestrator saves `interviews.best_face` at interview end. Report reads best_face from DB unchanged. Logic unit-tested | face_detector.py, aira_bot_client.py, webrtc_interview.py, aira_3_webrtc_orchestration.py |
| 2026-06-26 | Load-test harness fixed: old load_tests flopped because candidate_sim streamed continuous audio (no turn-taking) + engine never auto-started. NEW load_tests/candidate_conversation.py (listens to bot audio RMS, answers with pre-gen clips per turn) + load_tests/run_load_sim.py (one command: reset rooms via reset_interview → start engine subprocess → drive N conversational candidates → report). Run with aira_env python. NEEDS LIVE RUN to tune BOT_SILENCE_MS | load_tests/candidate_conversation.py, load_tests/run_load_sim.py |
| 2026-06-24 | NEW: api_key_pool.py — cross-process (file-lock) API-key concurrency pool. Keys as .env lists + per-provider magic numbers; first-fit, wait-when-saturated, crash-safe lease reclaim. Cross-process limit enforcement unit-tested. Wired: Cartesia (cartesia_tts.py both fns), Deepgram (aira_bot_client.py prewarm+record, lease rides the socket). OpenAI NOT yet wired (stateful Responses API needs per-interview key pinning, not per-call rotation) | api_key_pool.py, cartesia_tts.py, aira_bot_client.py |
