import asyncio
import logging
import multiprocessing
import os
import sys
import subprocess
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
from pathlib import Path
from io import StringIO
from contextlib import redirect_stdout, redirect_stderr

# CRITICAL: Suppress all logging BEFORE importing any custom modules
logging.basicConfig(level=logging.CRITICAL, force=True)
for logger_name in ['webrtc_interview', 'deepgram', 'root']:
    logging.getLogger(logger_name).setLevel(logging.CRITICAL)
    logging.getLogger(logger_name).propagate = False

import pymysql
import integrations.aira_sql as airadb
from fastapi import FastAPI, HTTPException, Query

# Import your helpers with stdout/stderr suppressed to block print() statements
_devnull = StringIO()
with redirect_stdout(_devnull), redirect_stderr(_devnull):
    from aira_3_webrtc_orchestration import get_db_connection, get_room_participants

app = FastAPI(title="AIRA Interview Orchestration APIs", version="2.0")

# ---- Configuration ----
IST = ZoneInfo("Asia/Kolkata")
from env_config import DEPLOYMENT, CRON_JOB_TIMING
logger_init = logging.getLogger("aira.orchestrator.init")
if DEPLOYMENT == "PRODUCTION":
    logger_init.info("🚀 PRODUCTION CODE RUNNING.....")
else:
    logger_init.info("🧪 DEVELOPMENT CODE RUNNING....")
# Environment detection and configuration.
# In k8s/container these are injected by the deployment env (helm values:
#   LOG_BASE_DIR=/app/logs  (mounted on the aira-engine-logs PVC → logs are safe),
#   VENV_PYTHON=python3, WORKING_DIR=/app).
# Env always wins; the per-platform values below are only fallbacks for a bare
# local (mac) or VM run. VENV_PYTHON defaults to the interpreter running app.py
# (sys.executable) so the spawned aira_1/2/3 subprocesses always use the right
# python regardless of where we run.
import platform
if platform.system() == "Darwin":       # macOS (local development)
    _DEF_LOG, _DEF_WD = "/Users/sridharkotha/Aira-Engine/logs", str(Path.cwd())
else:                                    # Linux (container or VM)
    _DEF_LOG, _DEF_WD = "/app/logs", "/app"
LOG_BASE_DIR = Path(os.environ.get("LOG_BASE_DIR", _DEF_LOG))
VENV_PYTHON  = os.environ.get("VENV_PYTHON", sys.executable)
WORKING_DIR  = os.environ.get("WORKING_DIR", _DEF_WD)


# Job configurations
# Campus engine: only the interview orchestration runs. ATS ingestion (aira_1) and
# scheduling reminders (aira_2) are JobDiva-hiring-only and were removed.
JOBS = {
    "aira_3": {
        "name": "WebRTC Orchestration",
        "script": "aira_3_webrtc_orchestration.py",
        "log_prefix": "aira_3",
        "interval": CRON_JOB_TIMING,  # seconds
        "process": None,
        "running": False
    }
}

# Status tracking
LIVE_TABLE = "interviews"
HISTORY_TABLE = "interview_history"
HISTORY_TIME_COL = "archived_at"
UPCOMING_STATUS = "Not Started"
LIVE_EXACT = {"Queued"}
LIVE_LIKE_PREFIX = "InProgress-"

# ---- Logging Setup (ORCHESTRATOR TO CONSOLE + FILE, SCRIPTS TO FILES ONLY) ----
# Create orchestrator log file
ORCHESTRATOR_LOG = LOG_BASE_DIR / f"orchestrator_{datetime.now(IST).strftime('%Y-%m-%d')}.log"
LOG_BASE_DIR.mkdir(parents=True, exist_ok=True)

# Configure root logger to suppress everything by default
logging.root.setLevel(logging.CRITICAL)

# Create orchestrator logger that writes to BOTH console and file
logger = logging.getLogger("aira.orchestrator")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    # Console handler for orchestrator messages
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(asctime)s [ORCHESTRATOR] %(message)s"))
    logger.addHandler(console_handler)
    
    # File handler for orchestrator messages
    file_handler = logging.FileHandler(ORCHESTRATOR_LOG, mode='a')
    file_handler.setFormatter(logging.Formatter("%(asctime)s [ORCHESTRATOR] %(message)s"))
    logger.addHandler(file_handler)

# Suppress ALL other loggers (including imported modules)
logging.getLogger("uvicorn").setLevel(logging.CRITICAL)
logging.getLogger("uvicorn.access").setLevel(logging.CRITICAL)
logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
logging.getLogger("fastapi").setLevel(logging.CRITICAL)
logging.getLogger("webrtc_interview").setLevel(logging.CRITICAL)
logging.getLogger("deepgram").setLevel(logging.CRITICAL)

# Suppress any other module logs during import
for name in list(logging.Logger.manager.loggerDict.keys()):
    if not name.startswith("aira.orchestrator"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


# ---- Helper Functions ----
def _close(conn_tunnel):
    """Close database connection and tunnel."""
    conn, tunnel = conn_tunnel
    try:
        conn.close()
    finally:
        if tunnel:
            tunnel.stop()


def _q(conn, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Execute query and return all results."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _one(conn, sql: str, params: tuple = ()) -> Optional[Any]:
    """Execute query and return single value."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        if not row:
            return None
        return list(row.values())[0]


def get_log_filename(job_id: str) -> str:
    """Generate daily log filename for a job."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    log_prefix = JOBS[job_id]["log_prefix"]
    return str(LOG_BASE_DIR / f"{log_prefix}_{today}.log")


def _execute_script(job_id: str, log_file: str):
    """Execute a single script instance (runs in separate process)."""
    job = JOBS[job_id]
    script_path = os.path.join(WORKING_DIR, job["script"])
    
    # NO console logging - only to orchestrator log file on critical errors
    exec_logger = logging.getLogger(f"aira.exec.{job_id}.{os.getpid()}")
    exec_logger.setLevel(logging.ERROR)
    exec_logger.propagate = False
    
    if not exec_logger.handlers:
        file_handler = logging.FileHandler(ORCHESTRATOR_LOG, mode='a')
        file_handler.setFormatter(
            logging.Formatter(f"%(asctime)s [{job_id.upper()}:PID{os.getpid()}] %(message)s")
        )
        exec_logger.addHandler(file_handler)
    
    process = None
    
    try:
        with open(log_file, 'a') as log_fp:
            process = subprocess.Popen(
                [VENV_PYTHON, script_path],
                cwd=WORKING_DIR,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True if platform.system() != "Windows" else False
            )
            
            returncode = process.wait()
            
            # Log failures to orchestrator file only
            if returncode != 0:
                exec_logger.error(f"Failed with return code {returncode}")
                
    except Exception as e:
        exec_logger.error(f"Exception: {e}")
        
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except:
                try:
                    process.kill()
                    process.wait()
                except:
                    pass
        
        try:
            with open(log_file, 'a') as log_fp:
                log_fp.write(f"\n[{datetime.now(IST).isoformat()}] EXCEPTION: {str(e)}\n")
        except:
            pass
    
    finally:
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except:
                try:
                    process.kill()
                except:
                    pass


def _cron_worker(job_id: str):
    """Worker function that spawns new script executions every interval (true cron behavior)."""
    job = JOBS[job_id]
    interval = job["interval"]
    
    # NO console logging - only to orchestrator file for critical issues
    worker_logger = logging.getLogger(f"aira.worker.{job_id}")
    worker_logger.setLevel(logging.ERROR)
    worker_logger.propagate = False
    
    if not worker_logger.handlers:
        file_handler = logging.FileHandler(ORCHESTRATOR_LOG, mode='a')
        file_handler.setFormatter(
            logging.Formatter(f"%(asctime)s [{job_id.upper()}-WORKER] %(message)s")
        )
        worker_logger.addHandler(file_handler)
    
    active_processes = []
    
    while True:
        try:
            # Clean up finished processes
            active_processes = [p for p in active_processes if p.is_alive()]
            for p in list(active_processes):
                if not p.is_alive():
                    p.join(timeout=0.1)
                    active_processes.remove(p)
            
            log_file = get_log_filename(job_id)
            
            exec_process = multiprocessing.Process(
                target=_execute_script,
                args=(job_id, log_file),
                daemon=False
            )
            exec_process.start()
            active_processes.append(exec_process)
            
            # Only log to file if serious issue (>10 processes)
            if len(active_processes) > 10:
                worker_logger.error(f"{len(active_processes)} processes active - potential issue")
            
        except Exception as e:
            worker_logger.error(f"Spawn error: {e}")
        
        import time
        time.sleep(interval)


# ---- Job Control APIs ----
@app.post("/jobs/{job_id}/start")
async def start_job(job_id: str):
    """Start a specific job."""
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    
    job = JOBS[job_id]
    
    if job["running"] and job["process"] and job["process"].is_alive():
        return {
            "status": "already running",
            "job_id": job_id,
            "job_name": job["name"],
            "pid": job["process"].pid,
            "timestamp": datetime.now(IST).isoformat()
        }
    
    # Start new process
    process = multiprocessing.Process(target=_cron_worker, args=(job_id,), daemon=False)
    process.start()
    
    job["process"] = process
    job["running"] = True
    
    logger.info(f"Started {job['name']} scheduler (PID: {process.pid})")
    
    return {
        "status": "started",
        "job_id": job_id,
        "job_name": job["name"],
        "scheduler_pid": process.pid,
        "log_file": get_log_filename(job_id),
        "interval_seconds": job["interval"],
        "timestamp": datetime.now(IST).isoformat()
    }


@app.get("/jobs/process-tree")
async def process_tree():
    """Show process tree for debugging (requires psutil)."""
    try:
        import psutil
        
        current_pid = os.getpid()
        current_process = psutil.Process(current_pid)
        
        tree = {
            "orchestrator": {
                "pid": current_pid,
                "status": current_process.status(),
                "children": []
            }
        }
        
        # Get all child processes recursively
        def get_children(proc, level=0):
            children = []
            try:
                for child in proc.children(recursive=False):
                    child_info = {
                        "pid": child.pid,
                        "name": child.name(),
                        "status": child.status(),
                        "level": level,
                        "children": get_children(child, level + 1)
                    }
                    children.append(child_info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            return children
        
        tree["orchestrator"]["children"] = get_children(current_process)
        
        return {
            "timestamp": datetime.now(IST).isoformat(),
            "tree": tree
        }
    except ImportError:
        raise HTTPException(
            status_code=501, 
            detail="psutil not installed. Run: pip install psutil"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    """Stop a specific job."""
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    
    job = JOBS[job_id]
    
    if not job["running"] or not job["process"]:
        return {
            "status": "not running",
            "job_id": job_id,
            "job_name": job["name"],
            "timestamp": datetime.now(IST).isoformat()
        }
    
    if job["process"].is_alive():
        logger.info(f"Stopping {job['name']} scheduler...")
        
        # First try graceful termination
        job["process"].terminate()
        job["process"].join(timeout=5)
        
        # Force kill if still alive
        if job["process"].is_alive():
            logger.warning(f"Scheduler didn't stop gracefully, forcing...")
            job["process"].kill()
            job["process"].join(timeout=2)
        
        # Clean up zombie
        if not job["process"].is_alive():
            try:
                job["process"].close()  # Available in Python 3.7+
            except AttributeError:
                pass
    
    job["running"] = False
    logger.info(f"Stopped {job['name']} scheduler")
    
    return {
        "status": "stopped",
        "job_id": job_id,
        "job_name": job["name"],
        "note": "Scheduler stopped. Active script executions will complete naturally.",
        "timestamp": datetime.now(IST).isoformat()
    }


@app.get("/jobs/{job_id}/status")
async def job_status(job_id: str):
    """Get status of a specific job."""
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    
    job = JOBS[job_id]
    
    is_alive = job["process"] and job["process"].is_alive() if job["running"] else False
    
    return {
        "job_id": job_id,
        "job_name": job["name"],
        "scheduler_running": is_alive,
        "scheduler_pid": job["process"].pid if is_alive else None,
        "script": job["script"],
        "interval_seconds": job["interval"],
        "log_file": get_log_filename(job_id),
        "timestamp": datetime.now(IST).isoformat()
    }


@app.post("/jobs/start-all")
async def start_all_jobs():
    """Start all jobs."""
    results = {}
    
    for job_id in JOBS.keys():
        try:
            result = await start_job(job_id)
            results[job_id] = result
        except Exception as e:
            results[job_id] = {"status": "error", "error": str(e)}
    
    return {
        "status": "completed",
        "results": results,
        "timestamp": datetime.now(IST).isoformat()
    }


@app.post("/jobs/stop-all")
async def stop_all_jobs():
    """Stop all jobs."""
    results = {}
    
    for job_id in JOBS.keys():
        try:
            result = await stop_job(job_id)
            results[job_id] = result
        except Exception as e:
            results[job_id] = {"status": "error", "error": str(e)}
    
    return {
        "status": "completed",
        "results": results,
        "timestamp": datetime.now(IST).isoformat()
    }


@app.get("/jobs/status-all")
async def status_all_jobs():
    """Get status of all jobs."""
    statuses = {}
    
    for job_id in JOBS.keys():
        try:
            status = await job_status(job_id)
            statuses[job_id] = status
        except Exception as e:
            statuses[job_id] = {"status": "error", "error": str(e)}
    
    return {
        "timestamp": datetime.now(IST).isoformat(),
        "jobs": statuses
    }


# ---- Interview Monitoring APIs ----
@app.get("/api/interviews/live")
async def live_interviews():
    """Get all live interviews with participant lists."""
    # ── API mode: call GET /dashboard/live ──
    if not airadb._IS_LOCAL:
        try:
            return airadb._api_call("GET", "/dashboard/live", process_id="app.py")
        except Exception as e:
            return {"error": str(e), "total_live_interviews": 0, "rooms": []}

    # ── LOCAL: direct DB (unchanged) ──
    conn_tunnel = get_db_connection()
    conn, _tunnel = conn_tunnel
    try:
        rows = _q(conn, f"""
            SELECT id, room_id, interview_status, interview_time
            FROM {LIVE_TABLE}
            WHERE interview_status = %s
               OR interview_status LIKE %s
        """, ("Queued", f"{LIVE_LIKE_PREFIX}%"))

        async def fetch(rid):
            if not rid:
                return rid, []
            parts = await asyncio.to_thread(get_room_participants, rid)
            return rid, parts or []

        participants_map = {}
        if rows:
            for rid, parts in await asyncio.gather(*(fetch(r["room_id"]) for r in rows)):
                participants_map[rid] = parts

        rooms = []
        now = datetime.now(IST)
        for r in rows:
            rid = r["room_id"]
            start_time = r.get("interview_time")
            running_for = None

            if start_time:
                if isinstance(start_time, str):
                    start_time = datetime.fromisoformat(start_time)
                delta = now - start_time
                total_seconds = int(delta.total_seconds())
                hours, remainder = divmod(total_seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                running_for = f"{hours:02}:{minutes:02}:{seconds:02}"

            parts = participants_map.get(rid, [])
            rooms.append({
                "room_id": rid,
                "interview_id": r["id"],
                "status": r["interview_status"],
                "participant_count": len(parts),
                "participants": parts,
                "scheduled_time": r["interview_time"].isoformat() if r.get("interview_time") else None,
                "running_for": running_for,
            })

        return {
            "timestamp": now.isoformat(),
            "total_live_interviews": len(rooms),
            "rooms": rooms
        }
    finally:
        _close(conn_tunnel)


@app.get("/api/interviews/upcoming")
def upcoming_interviews():
    """Get all scheduled but not started interviews."""
    conn_tunnel = get_db_connection()
    conn, _tunnel = conn_tunnel
    try:
        rows = _q(conn, f"""
            SELECT id, room_id, interview_time, recruiter_email, candidate_id
            FROM {LIVE_TABLE}
            WHERE interview_status = %s
            ORDER BY interview_time ASC
        """, (UPCOMING_STATUS,))
        return {
            "timestamp": datetime.now(IST).isoformat(),
            "count": len(rows),
            "upcoming": rows
        }
    finally:
        _close(conn_tunnel)


@app.get("/api/interviews/history")
def recent_history(hours: int = Query(24, ge=1, le=720)):
    """Get recent interview history."""
    since = datetime.now(IST) - timedelta(hours=hours)
    conn_tunnel = get_db_connection()
    conn, _tunnel = conn_tunnel
    try:
        rows = _q(conn, f"""
            SELECT room_id,
                   interview_status,
                   interview_time,
                   {HISTORY_TIME_COL} AS completed_at,
                   recruiter_email,
                   candidate_id
            FROM {HISTORY_TABLE}
            WHERE {HISTORY_TIME_COL} >= %s
            ORDER BY {HISTORY_TIME_COL} DESC
        """, (since,))
        return {
            "timestamp": datetime.now(IST).isoformat(),
            "window_hours": hours,
            "count": len(rows),
            "records": rows
        }
    finally:
        _close(conn_tunnel)


@app.get("/api/interviews/summary")
def interviews_summary(hours: int = Query(24, ge=1, le=720)):
    """Get summary statistics of interviews."""
    # ── API mode: call GET /dashboard/summary ──
    if not airadb._IS_LOCAL:
        try:
            return airadb._api_call("GET", "/dashboard/summary", params={"hours": hours}, process_id="app.py")
        except Exception as e:
            return {"error": str(e)}

    # ── LOCAL: direct DB (unchanged) ──
    now = datetime.now(IST)
    since = now - timedelta(hours=hours)
    conn_tunnel = get_db_connection()
    conn, _tunnel = conn_tunnel
    try:
        scheduled = _one(conn, f"SELECT COUNT(*) FROM {LIVE_TABLE} WHERE interview_status=%s", (UPCOMING_STATUS,)) or 0
        queued = _one(conn, f"SELECT COUNT(*) FROM {LIVE_TABLE} WHERE interview_status=%s", ("Queued",)) or 0
        inprog = _one(conn, f"SELECT COUNT(*) FROM {LIVE_TABLE} WHERE interview_status LIKE %s", (f"{LIVE_LIKE_PREFIX}%",)) or 0

        completed = _one(conn, f"""
            SELECT COUNT(*) FROM {HISTORY_TABLE}
            WHERE {HISTORY_TIME_COL} >= %s
              AND (interview_status='Completed' OR interview_status='Complete')
        """, (since,)) or 0

        terminated = _one(conn, f"""
            SELECT COUNT(*) FROM {HISTORY_TABLE}
            WHERE {HISTORY_TIME_COL} >= %s
              AND interview_status LIKE 'Terminated%%'
        """, (since,)) or 0

        failed = _one(conn, f"""
            SELECT COUNT(*) FROM {HISTORY_TABLE}
            WHERE {HISTORY_TIME_COL} >= %s
              AND interview_status LIKE 'Failed-%%'
        """, (since,)) or 0

        return {
            "timestamp": now.isoformat(),
            "window_hours": hours,
            "scheduled": int(scheduled),
            "queued": int(queued),
            "in_progress": int(inprog),
            "live_total": int(queued) + int(inprog),
            "history": {
                "completed": int(completed),
                "terminated": int(terminated),
                "failed": int(failed),
                "recent_total": int(completed) + int(terminated) + int(failed)
            }
        }
    finally:
        _close(conn_tunnel)


@app.get("/api/system/health")
def system_health():
    """Check system health and database connectivity."""
    try:
        conn_tunnel = get_db_connection()
        conn, tunnel = conn_tunnel
        conn.ping(reconnect=True)
        conn.close()
        if tunnel:
            tunnel.stop()
        
        # Check job statuses
        jobs_health = {}
        for job_id, job in JOBS.items():
            jobs_health[job_id] = {
                "scheduler_running": job["running"] and job["process"] and job["process"].is_alive(),
                "name": job["name"]
            }
        
        return {
            "status": "ok",
            "db": "connected",
            "jobs": jobs_health,
            "timestamp": datetime.now(IST).isoformat()
        }
    except Exception as e:
        logger.error("Health check failed: %s", e)
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now(IST).isoformat()
        }


# ---- Lifecycle Events ----
@app.on_event("startup")
async def startup():
    """Auto-start all jobs on application startup."""
    logger.info("Starting all job schedulers automatically...")
    
    await start_all_jobs()
    
    logger.info("All job schedulers started successfully")


@app.on_event("shutdown")
async def shutdown():
    """Stop all jobs on application shutdown."""
    logger.info("Stopping all job schedulers on application shutdown...")
    
    await stop_all_jobs()
    
    logger.info("All job schedulers stopped successfully")


# ---- Main Entry Point ----
if __name__ == "__main__":
    import uvicorn
    
    # Suppress module import logs before starting uvicorn
    logging.getLogger("webrtc_interview").setLevel(logging.CRITICAL)
    
    # Run uvicorn with minimal console logging
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8081,
        log_level="warning",  # Only show warnings/errors from uvicorn
        access_log=False  # Disable access logs in console
    )