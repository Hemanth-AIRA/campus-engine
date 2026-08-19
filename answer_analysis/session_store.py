import json
import tempfile
import os
from pathlib import Path
from typing import Dict
from datetime import datetime

from .models import AnswerAssistanceSignal

# -----------------------------
# Configuration
# -----------------------------

SESSION_DIR = Path("sessions")
SESSION_DIR.mkdir(exist_ok=True)


# -----------------------------
# Session Loader
# -----------------------------

def load_session(session_id: str) -> Dict:
    """
    Load an interview session.

    Session schema is intentionally simple and append-only.
    No judgments, only stored signals.
    """

    filepath = SESSION_DIR / f"session_{session_id}.json"

    if not filepath.exists():
        return {
            "session_id": session_id,
            "answers": [],            # raw answers (for semantic context)
            "questions": [],          # question text
            "signals": [],            # AnswerAssistanceSignal dicts
            "durations": [],          # answer durations
            "mute_events": [],        # Track mute/silence events per question
            "metadata": {
                "created_at": datetime.utcnow().isoformat(),
                "last_updated": None
            }
        }

    with open(filepath, "r", encoding="utf-8") as f:
        session = json.load(f)
        # Ensure mute_events exists for backward compatibility
        if "mute_events" not in session:
            session["mute_events"] = []
        if "questions" not in session:
            session["questions"] = []
        return session


# -----------------------------
# Append Signal (CORE WRITE PATH)
# -----------------------------

def append_signal(
    session_id: str,
    signal: AnswerAssistanceSignal,
    answer_text: str,
    answer_duration: float,
    question_text: str = ""
) -> None:
    """
    Persist a single answer's assistance signal.

    IMPORTANT:
    - No probability aggregation here
    - No AI claims
    - No confidence labels
    """

    session = load_session(session_id)

    # -----------------------------
    # Append raw answer (for future semantic context)
    # -----------------------------
    session["answers"].append(answer_text)
    
    # -----------------------------
    # Append question text
    # -----------------------------
    session["questions"].append(question_text)

    # -----------------------------
    # Append assistance signal
    # -----------------------------
    session["signals"].append({
        "answer_signal": round(signal.answer_signal, 4),
        "semantic_result": (
            signal.semantic_result.to_dict()
            if signal.semantic_result is not None
            else None
        ),
        "anomaly_flags": signal.anomaly_flags,
        "timestamp": datetime.utcnow().isoformat()
    })


    # -----------------------------
    # Track durations (for anomaly guards)
    # -----------------------------
    session["durations"].append(answer_duration)

    # -----------------------------
    # Update metadata
    # -----------------------------
    session["metadata"]["last_updated"] = datetime.utcnow().isoformat()

    # -----------------------------
    # Atomic write
    # -----------------------------
    _atomic_write(session_id, session)


# -----------------------------
# Append Mute Event
# -----------------------------

def append_mute_event(
    session_id: str,
    question_number: int,
    silence_duration_sec: float,
    event_type: str = "MUTE"
) -> None:
    """
    Track when a candidate is muted or has extended silence.
    
    Args:
        session_id: Interview session ID
        question_number: Which question this occurred on
        silence_duration_sec: How long they were silent
        event_type: "MUTE" or "EXIT"
    """
    session = load_session(session_id)
    
    # Ensure mute_events exists
    if "mute_events" not in session:
        session["mute_events"] = []
    
    session["mute_events"].append({
        "question_number": question_number,
        "silence_duration_sec": round(silence_duration_sec, 1),
        "event_type": event_type,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    session["metadata"]["last_updated"] = datetime.utcnow().isoformat()
    
    _atomic_write(session_id, session)


# -----------------------------
# Seed prior-round answers (cross-round authenticity)
# -----------------------------

def seed_prior_answers(session_id: str, prior_answers: list) -> None:
    """
    Pre-load prior round answers into a session so Round 2+
    has a baseline for authenticity analysis from Q1.
    
    Must be called BEFORE the first append_signal for this session.
    """
    if not prior_answers:
        return

    session = load_session(session_id)

    # Only seed if the session is fresh (no answers yet)
    if session["answers"]:
        return

    session["answers"] = list(prior_answers)
    session["metadata"]["seeded_from_prior_rounds"] = True
    session["metadata"]["seeded_answer_count"] = len(prior_answers)
    session["metadata"]["last_updated"] = datetime.utcnow().isoformat()

    _atomic_write(session_id, session)


# -----------------------------
# Helpers
# -----------------------------

def _atomic_write(session_id: str, session_data: Dict) -> None:
    """
    Atomic write to avoid corruption on concurrent writes.
    """

    filepath = SESSION_DIR / f"session_{session_id}.json"

    fd, temp_path = tempfile.mkstemp(
        dir=SESSION_DIR,
        prefix=f".tmp_session_{session_id}_",
        suffix=".json"
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(session_data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, filepath)

    except Exception:
        try:
            os.unlink(temp_path)
        except Exception:
            pass
        raise

