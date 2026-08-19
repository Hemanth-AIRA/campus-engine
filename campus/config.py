"""
Central, override-able configuration for the CAMPUS engine.

Every campus behaviour is a single knob here, so an institution can tune the engine
without code changes:

  • mode                 — "practice" (formative, forgiving, coaching on) vs
                            "assessment" (summative, proctored, scored, coaching off)
  • hints / model answers — the coaching learning-loop
  • profile              — a target company/role preset (skills + difficulty)
  • personalized_retake  — seed difficulty from the student's prior mastery
  • adaptive thresholds   — the Phase-2 escalate/lower/remove confidence bands

Precedence (highest first):  explicit per-interview override  →  environment variable  →  default.
The per-interview override dict is whatever the DB interview_config carries under a
"campus" key, so faculty can set this per assignment.
"""
import os
import logging
from dataclasses import dataclass, asdict

logger = logging.getLogger("campus_config")

PRACTICE, ASSESSMENT = "practice", "assessment"


def _env_bool(name, default):
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


@dataclass
class CampusConfig:
    mode: str = PRACTICE
    hints_enabled: bool = True
    model_answers_enabled: bool = True
    profile: str = ""
    personalized_retake: bool = True
    # Adaptive confidence bands (mirror interview_config campus thresholds).
    escalate_conf: float = 0.70
    lower_conf: float = 0.45
    remove_conf: float = 0.10

    # ── derived behaviour ────────────────────────────────────────────────
    @property
    def is_practice(self) -> bool:
        return self.mode == PRACTICE

    @property
    def is_assessment(self) -> bool:
        return self.mode == ASSESSMENT

    def coaching_on(self) -> bool:
        """Hints/model answers only ever fire in PRACTICE mode."""
        return self.is_practice and (self.hints_enabled or self.model_answers_enabled)

    def gives_hints(self) -> bool:
        return self.is_practice and self.hints_enabled

    def gives_model_answers(self) -> bool:
        return self.is_practice and self.model_answers_enabled

    def scored_verdict(self) -> bool:
        """Assessment produces a scored pass/level; practice stays purely formative."""
        return self.is_assessment

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update({"is_practice": self.is_practice, "is_assessment": self.is_assessment,
                  "coaching_on": self.coaching_on()})
        return d

    # ── construction ─────────────────────────────────────────────────────
    @classmethod
    def resolve(cls, overrides: dict = None) -> "CampusConfig":
        """Build from defaults ← env ← explicit overrides (per-interview 'campus' dict)."""
        c = cls(
            mode=os.getenv("CAMPUS_MODE", PRACTICE).strip().lower(),
            hints_enabled=_env_bool("CAMPUS_HINTS", True),
            model_answers_enabled=_env_bool("CAMPUS_MODEL_ANSWERS", True),
            profile=os.getenv("CAMPUS_PROFILE", "").strip(),
            personalized_retake=_env_bool("CAMPUS_PERSONALIZED_RETAKE", True),
            escalate_conf=_env_float("CAMPUS_ESCALATE_CONF", 0.70),
            lower_conf=_env_float("CAMPUS_LOWER_CONF", 0.45),
            remove_conf=_env_float("CAMPUS_REMOVE_CONF", 0.10),
        )
        for k, v in (overrides or {}).items():
            if hasattr(c, k) and v is not None:
                setattr(c, k, v)
        if c.mode not in (PRACTICE, ASSESSMENT):
            logger.warning(f"Invalid campus mode '{c.mode}', defaulting to {PRACTICE}")
            c.mode = PRACTICE
        # Assessment is proctored/summative → coaching is force-disabled regardless of flags.
        if c.is_assessment:
            c.hints_enabled = False
            c.model_answers_enabled = False
        logger.info(f"🎓 CampusConfig resolved: {c.to_dict()}")
        return c
