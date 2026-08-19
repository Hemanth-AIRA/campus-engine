"""
InterviewConfig & InterviewContext — Centralized interview configuration and state.

Phase 3C+: Full mid-interview adaptive engine with:
- Strategy-driven escalation (abstraction_first / constraint_first)
- Symmetrical strategy switching (both directions, one-time lock per round)
- Escalation budget (configurable max per round)
- Explicit round transition reset
- Structured logging with AdaptationEvent enum
"""

import os
import logging
from enum import Enum
from dataclasses import dataclass, field
from collections import deque
from typing import Dict, Optional

from core.difficulty_policy import DifficultyVector

logger = logging.getLogger('InterviewConfig')

_DEFAULT_QUESTIONS = int(os.getenv("NUMBER_OF_QUESTIONS", 15))
_DEFAULT_RESUME_RATIO = float(os.getenv("RESUME_QUESTION_PERCENTAGE", "0.2"))

# ── CAMPUS adaptive-difficulty thresholds (Phase 2: adaptive-first) ──────────
# The HR engine only escalated at an "elite" rolling confidence of 0.85 and dropped
# below 0.50, leaving a very wide hold band where difficulty never tracked the student.
# For campus PREP the band is narrower (difficulty follows live performance both ways)
# and the downside is far more tolerant: struggling students get scaffolded / re-asked
# easier (Phase 4), not removed — removal is a hiring signal, not a learning one.
_CAMPUS_ESCALATE_CONF = float(os.getenv("CAMPUS_ESCALATE_CONF", "0.70"))  # step difficulty UP at/above
_CAMPUS_LOWER_CONF = float(os.getenv("CAMPUS_LOWER_CONF", "0.45"))        # step DOWN / confirm below
_CAMPUS_REMOVE_CONF = float(os.getenv("CAMPUS_REMOVE_CONF", "0.10"))      # only truly-blank answers

# ── CAMPUS adaptive-difficulty thresholds (Phase 2: adaptive-first) ──────────
# The HR engine only escalated at an "elite" rolling confidence of 0.85 and dropped
# below 0.50, leaving a very wide hold band where difficulty never tracked the student.
# For campus PREP the band is narrower (difficulty follows live performance both ways)
# and the downside is far more tolerant: struggling students get scaffolded / re-asked
# easier (Phase 4), not removed — removal is a hiring signal, not a learning one.
_CAMPUS_ESCALATE_CONF = float(os.getenv("CAMPUS_ESCALATE_CONF", "0.70"))  # step difficulty UP at/above
_CAMPUS_LOWER_CONF = float(os.getenv("CAMPUS_LOWER_CONF", "0.45"))        # step DOWN / confirm below
_CAMPUS_REMOVE_CONF = float(os.getenv("CAMPUS_REMOVE_CONF", "0.10"))      # only truly-blank answers

# ── CAMPUS adaptive-difficulty thresholds (Phase 2: adaptive-first) ──────────
# The HR engine only escalated at an "elite" rolling confidence of 0.85 and dropped
# below 0.50, leaving a very wide hold band where difficulty never tracked the student.
# For campus PREP the band is narrower (difficulty follows live performance both ways)
# and the downside is far more tolerant: struggling students get scaffolded / re-asked
# easier (Phase 4), not removed — removal is a hiring signal, not a learning one.
_CAMPUS_ESCALATE_CONF = float(os.getenv("CAMPUS_ESCALATE_CONF", "0.70"))  # step difficulty UP at/above
_CAMPUS_LOWER_CONF = float(os.getenv("CAMPUS_LOWER_CONF", "0.45"))        # step DOWN / confirm below
_CAMPUS_REMOVE_CONF = float(os.getenv("CAMPUS_REMOVE_CONF", "0.10"))      # only truly-blank answers


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3C+: Structured Logging Event Enum
# ═══════════════════════════════════════════════════════════════════════════

class AdaptationEvent(Enum):
    """Standardized event types for mid-interview adaptation logging."""
    ESCALATION = "VECTOR_ESCALATION"
    CONFIRMING = "VECTOR_CONFIRMING"
    REMOVED = "SKILL_REMOVED"
    STRATEGY_SWITCH = "STRATEGY_SWITCH"
    STRATEGY_LOCKED = "STRATEGY_LOCKED"
    ROUND_RESET = "ROUND_RESET"
    # Phase 4 Concept Events
    CONCEPT_CLASSIFIED = "CONCEPT_CLASSIFIED"
    CONCEPT_REINFORCE = "CONCEPT_REINFORCE"
    CONCEPT_DEEPEN = "CONCEPT_DEEPEN"
    CONCEPT_EXPLORE = "CONCEPT_EXPLORE"
    CONCEPT_IGNORED = "CONCEPT_IGNORED"


# ═══════════════════════════════════════════════════════════════════════════
# InterviewConfig
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class InterviewConfig:
    """Single source of truth for all interview parameters."""
    total_rounds: int = 1
    questions_per_round: int = _DEFAULT_QUESTIONS
    resume_question_ratio: float = _DEFAULT_RESUME_RATIO
    base_difficulty: str = "medium"
    escalation_mode: str = "linear"
    focus_mode: str = "mixed"
    role_experience_required: str = "mid"
    pass_threshold_pct: int = 50
    max_escalations_per_round: int = 3  # Phase 3C+: escalation budget
    max_duration_minutes: int = 60      # Configurable interview duration
    # Explicit JD/Resume question counts (optional overrides)
    # When set, these take precedence over resume_question_ratio. They are the SOURCE
    # split of the CONTENT questions: questions_per_round == jd + resume (the greeting
    # and background turns are separate and are NOT counted here).
    jd_question_count: Optional[int] = None
    resume_question_count: Optional[int] = None
    # Concept vs Scenario is a STYLE split over ALL content questions (optional).
    # conceptual + scenario should equal questions_per_round (jd + resume); resume
    # questions are conceptual-styled and count toward conceptual.
    conceptual_question_count: Optional[int] = None
    scenario_question_count: Optional[int] = None
    # Slots reserved for ADAPTIVE follow-ups on top of the quota questions, so the
    # budget arbiter can't suppress follow-ups entirely. This is an internal scheduling
    # hint only — it does NOT change questions_per_round (the content-question count).
    followup_reserve_count: int = 0

    def __post_init__(self):
        if self.total_rounds < 1:
            logger.warning(f"Invalid total_rounds={self.total_rounds}, defaulting to 1")
            self.total_rounds = 1

        # ── Explicit JD/Resume count override logic ──────────────────────
        # SOURCE counts define the CONTENT questions. questions_per_round is the
        # number of content questions = jd + resume. The greeting (Q1), background
        # (Q2) and adaptive follow-ups are SEPARATE and are NOT part of this count.
        # If BOTH source counts are given, their sum IS questions_per_round.
        # If only one is given, the configured questions_per_round is the content
        # total and the other source count is the remainder.
        if self.jd_question_count is not None and self.resume_question_count is not None:
            # Both explicit → content total is their sum (no greeting/reserve padding).
            self.questions_per_round = self.jd_question_count + self.resume_question_count
            self.resume_question_ratio = (
                self.resume_question_count / self.questions_per_round
                if self.questions_per_round > 0 else _DEFAULT_RESUME_RATIO
            )
            logger.info(
                f"📊 Explicit JD/Resume counts: jd={self.jd_question_count}, "
                f"resume={self.resume_question_count} "
                f"→ content questions_per_round={self.questions_per_round}, "
                f"resume_ratio={self.resume_question_ratio:.2f}"
            )
        elif self.jd_question_count is not None:
            # Only JD count given: questions_per_round is the content total, infer resume.
            self.resume_question_count = max(0, self.questions_per_round - self.jd_question_count)
            self.resume_question_ratio = (
                self.resume_question_count / self.questions_per_round
                if self.questions_per_round > 0 else _DEFAULT_RESUME_RATIO
            )
            logger.info(
                f"📊 Explicit JD count: jd={self.jd_question_count}, "
                f"resume={self.resume_question_count} (inferred) → resume_ratio={self.resume_question_ratio:.2f}"
            )
        elif self.resume_question_count is not None:
            # Only resume count given: questions_per_round is the content total, infer JD.
            self.jd_question_count = max(0, self.questions_per_round - self.resume_question_count)
            self.resume_question_ratio = (
                self.resume_question_count / self.questions_per_round
                if self.questions_per_round > 0 else _DEFAULT_RESUME_RATIO
            )
            logger.info(
                f"📊 Explicit resume count: resume={self.resume_question_count}, "
                f"jd={self.jd_question_count} (inferred) → resume_ratio={self.resume_question_ratio:.2f}"
            )

        if self.questions_per_round < 1:
            logger.warning(f"Invalid questions_per_round={self.questions_per_round}, defaulting to {_DEFAULT_QUESTIONS}")
            self.questions_per_round = _DEFAULT_QUESTIONS
        if not (0 <= self.resume_question_ratio <= 1):
            logger.warning(f"Invalid resume_question_ratio={self.resume_question_ratio}, defaulting to {_DEFAULT_RESUME_RATIO}")
            self.resume_question_ratio = _DEFAULT_RESUME_RATIO
        valid_difficulties = ("easy", "medium", "hard")
        if self.base_difficulty not in valid_difficulties:
            logger.warning(f"Invalid base_difficulty='{self.base_difficulty}', defaulting to 'medium'")
            self.base_difficulty = "medium"
        valid_focus = ("mixed", "deep", "broad")
        if self.focus_mode not in valid_focus:
            logger.warning(f"Invalid focus_mode='{self.focus_mode}', defaulting to 'mixed'")
            self.focus_mode = "mixed"
        valid_experience = ("junior", "mid", "senior")
        if self.role_experience_required not in valid_experience:
            logger.warning(f"Invalid role_experience_required='{self.role_experience_required}', defaulting to 'mid'")
            self.role_experience_required = "mid"
        if not (0 <= self.pass_threshold_pct <= 100):
            logger.warning(f"Invalid pass_threshold_pct={self.pass_threshold_pct}, defaulting to 50")
            self.pass_threshold_pct = 50
        if self.max_duration_minutes < 1:
            logger.warning(f"Invalid max_duration_minutes={self.max_duration_minutes}, defaulting to 60")
            self.max_duration_minutes = 60

        # ── FORMAT (conceptual vs scenario) — a STYLE split over ALL content ──
        # conceptual + scenario is the STYLE distribution across every content
        # question (= questions_per_round), independent of jd_question_count.
        # Resume questions are conceptual-styled and count toward conceptual. If the
        # style counts don't sum to the content total we NORMALIZE the style split
        # (never touching jd/resume/questions_per_round, which are the SOURCE truth).
        _N = self.questions_per_round
        if self.conceptual_question_count is not None and self.scenario_question_count is not None:
            _cs = self.conceptual_question_count + self.scenario_question_count
            if _N > 0 and _cs != _N:
                if _cs == 0:
                    self.conceptual_question_count, self.scenario_question_count = _N, 0
                else:
                    # Preserve the scenario proportion; conceptual takes the remainder.
                    self.scenario_question_count = min(_N, round(self.scenario_question_count * _N / _cs))
                    self.conceptual_question_count = _N - self.scenario_question_count
                logger.warning(
                    f"⚠️ conceptual+scenario ({_cs}) ≠ content questions ({_N}); "
                    f"normalized style split → conceptual={self.conceptual_question_count}, "
                    f"scenario={self.scenario_question_count}"
                )
            logger.info(
                f"📊 Concept/Scenario split: conceptual={self.conceptual_question_count}, "
                f"scenario={self.scenario_question_count}"
            )
        elif self.conceptual_question_count is not None:
            # Only conceptual given: scenario is the remainder over the content total.
            self.conceptual_question_count = max(0, min(self.conceptual_question_count, _N))
            self.scenario_question_count = _N - self.conceptual_question_count
            logger.info(
                f"📊 Conceptual count set: conceptual={self.conceptual_question_count}, "
                f"scenario={self.scenario_question_count} (inferred)"
            )
        elif self.scenario_question_count is not None:
            # Only scenario given: conceptual is the remainder over the content total.
            self.scenario_question_count = max(0, min(self.scenario_question_count, _N))
            self.conceptual_question_count = _N - self.scenario_question_count
            logger.info(
                f"📊 Scenario count set: scenario={self.scenario_question_count}, "
                f"conceptual={self.conceptual_question_count} (inferred)"
            )

    @property
    def total_questions(self) -> int:
        return self.total_rounds * self.questions_per_round

    def log_config(self, prefix: str = ""):
        jd_resume_info = ""
        if self.jd_question_count is not None or self.resume_question_count is not None:
            jd_resume_info = (
                f", jd_qs={self.jd_question_count}, "
                f"resume_qs={self.resume_question_count}"
            )
        concept_scenario_info = ""
        if self.conceptual_question_count is not None or self.scenario_question_count is not None:
            concept_scenario_info = (
                f", concept_qs={self.conceptual_question_count}, "
                f"scenario_qs={self.scenario_question_count}"
            )
        logger.info(
            f"📋 {prefix}InterviewConfig: "
            f"rounds={self.total_rounds}, "
            f"q_per_round={self.questions_per_round}, "
            f"resume_ratio={self.resume_question_ratio}{jd_resume_info}{concept_scenario_info}, "
            f"difficulty={self.base_difficulty}, "
            f"focus={self.focus_mode}, "
            f"experience={self.role_experience_required}, "
            f"pass_threshold={self.pass_threshold_pct}%, "
            f"max_esc={self.max_escalations_per_round}, "
            f"duration={self.max_duration_minutes}m"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Concept Adaptivity (Phase 4)
# ═══════════════════════════════════════════════════════════════════════════

class ConceptState:
    """
    Tracks the performance history of a specific topical concept within a skill.
    Used for directional steering without affecting raw difficulty vectors.
    """
    def __init__(self, name: str):
        self.name = name
        self.score_history: deque = deque(maxlen=3)
        self.status = "neutral"  # strong | weak | neutral | ignored
        self.last_probed_question = None
        self.probe_count_this_round = 0


# ═══════════════════════════════════════════════════════════════════════════
# RuntimeSkillState
# ═══════════════════════════════════════════════════════════════════════════

class RuntimeSkillState:
    """
    Phase 3C+: Runtime state for a single skill's confidence adaptation.
    
    Tracks recent evaluations and drives strategy-based vector escalation.
    Includes escalation budget to prevent early cap saturation.
    """
    def __init__(
        self,
        vector: DifficultyVector,
        skill: str = "",
        role: str = "mid",
        difficulty: str = "medium",
        round_number: int = 1,
    ):
        self.skill = skill
        self.vector = vector
        self.recent_conf: deque = deque(maxlen=3)
        self.status: str = "active"  # "active" | "confirming" | "removed"
        self.cooldown: int = 0

        # Strategy fields
        from core.difficulty_policy import get_escalation_strategy
        self.default_strategy = get_escalation_strategy(role, difficulty, round_number)
        self.current_strategy = self.default_strategy
        self.strategy_locked = False

        # Escalation budget
        self.escalation_count: int = 0

        # Concept Tracking (Phase 4)
        self.concepts: Dict[str, ConceptState] = {}
        self.current_concept_focus: Optional[str] = None
        self.current_concept_mode: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# InterviewContext
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class InterviewContext:
    """
    Runtime state wrapper for a single interview session.
    
    Phase 3C+: Full adaptive engine with strategy-driven escalation,
    symmetrical switching, escalation budget, and structured logging.
    
    Round Transition Contract (advance_round):
    ─────────────────────────────────────────────
    RESETS:     vector, current_strategy, strategy_locked, cooldown, escalation_count
    PRESERVES:  recent_conf (trimmed to last 2 for cross-round signal)
    SKIPS:      removed skills (permanent)
    """
    config: InterviewConfig
    current_round: int = 1
    current_question_index: int = 0
    skill_states: Dict[str, RuntimeSkillState] = field(default_factory=dict)

    @property
    def global_question_number(self) -> int:
        return (self.current_round - 1) * self.config.questions_per_round + self.current_question_index + 1

    def advance_question(self):
        self.current_question_index += 1

    def advance_round(self):
        """
        Phase 3C+: Round transition reset.
        
        Re-computes vectors from new round caps, resets strategy and budget.
        Preserves recent_conf (trimmed to last 2) for cross-round signal.
        """
        from core.difficulty_policy import compute_difficulty_vector, get_escalation_strategy

        self.current_round += 1
        self.current_question_index = 0

        role = self.config.role_experience_required
        difficulty = self.config.base_difficulty
        active_count = 0

        for skill, state in self.skill_states.items():
            if state.status == "removed":
                continue

            before = state.vector.to_dict()

            # Re-compute vector from new round caps
            state.vector = compute_difficulty_vector(
                difficulty, role, self.current_round, total_rounds=self.config.total_rounds
            )
            # Reset strategy to default for new round
            state.default_strategy = get_escalation_strategy(role, difficulty, self.current_round)
            state.current_strategy = state.default_strategy
            state.strategy_locked = False
            state.cooldown = 0
            state.escalation_count = 0

            # Reset concept probes for budget control (Phase 4)
            for c_state in state.concepts.values():
                c_state.probe_count_this_round = 0

            # Trim recent_conf to last 2 for cross-round signal
            recent = list(state.recent_conf)[-2:] if state.recent_conf else []
            state.recent_conf = deque(recent, maxlen=3)

            active_count += 1

        self._log_event(
            AdaptationEvent.ROUND_RESET, "ALL_SKILLS", 0.0,
            {"round": self.current_round - 1},
            {"round": self.current_round, "active_skills": active_count},
        )

    # ═══════════════════════════════════════════════════════════════════════
    # Phase 3C+: Mid-Interview Skill Confidence Adaptation
    # ═══════════════════════════════════════════════════════════════════════

    def prepare_skill_for_next_question(self, skill: str) -> Optional[DifficultyVector]:
        """Trigger point before generating the next question.
        Handles both Vector Adaptation (Phase 3C) and Concept Steering (Phase 4)."""
        state = self.skill_states.get(skill)
        if not state or state.status == "removed":
            return None

        # 1. Vector Adaptation (Phase 3C+)
        if state.cooldown > 0:
            state.cooldown -= 1
        else:
            self.maybe_adapt(skill)
            
        if state.status == "removed":
            return None

        # 2. Concept Scoring and Steering (Phase 4)
        MAX_CONCEPT_PROBES_PER_ROUND = 3
        weak_concepts = []
        strong_concepts = []

        # PART 3: Concept Scoring (Main Thread)
        for c_name, c_state in state.concepts.items():
            if len(c_state.score_history) == 0 or c_state.status == "ignored":
                continue

            scores = list(c_state.score_history)
            avg = sum(scores) / len(scores)

            old_status = c_state.status
            # Reclassify (avoid jitter by requiring catastrophic failure or strong signal)
            if avg >= 0.85:
                c_state.status = "strong"
            elif avg >= 0.50:
                c_state.status = "neutral"
            elif avg >= 0.20:
                c_state.status = "weak"
            elif avg < 0.20 and len(scores) >= 2:
                c_state.status = "ignored"

            if old_status != c_state.status:
                self._log_event(
                    AdaptationEvent.CONCEPT_CLASSIFIED, skill, avg,
                    {"concept": c_name, "old_status": old_status},
                    {"concept": c_name, "new_status": c_state.status},
                )

            # Safeguard: skip if we just probed it last question (unless confirming)
            if c_state.last_probed_question == self.global_question_number - 1:
                continue

            if c_state.status == "weak":
                weak_concepts.append((c_name, c_state, avg))
            elif c_state.status == "strong":
                strong_concepts.append((c_name, c_state, avg))

        # PART 4: Steering Policy
        weak_concepts.sort(key=lambda x: x[2])  # Ascending (weakest first)
        strong_concepts.sort(key=lambda x: x[2], reverse=True)  # Descending (strongest first)

        state.current_concept_focus = None
        state.current_concept_mode = "explore"

        for c_name, c_state, avg in weak_concepts:
            if c_state.probe_count_this_round < MAX_CONCEPT_PROBES_PER_ROUND:
                state.current_concept_focus = c_name
                state.current_concept_mode = "reinforce"
                c_state.probe_count_this_round += 1
                c_state.last_probed_question = self.global_question_number
                break
        
        if not state.current_concept_focus:
            for c_name, c_state, avg in strong_concepts:
                if c_state.probe_count_this_round < MAX_CONCEPT_PROBES_PER_ROUND:
                    state.current_concept_focus = c_name
                    state.current_concept_mode = "deepen"
                    c_state.probe_count_this_round += 1
                    c_state.last_probed_question = self.global_question_number
                    break

        if state.current_concept_focus:
            event_type = getattr(AdaptationEvent, f"CONCEPT_{state.current_concept_mode.upper()}")
            self._log_event(
                event_type, skill, 0.0,
                {"previous_focus": None},
                {"focus": state.current_concept_focus, "mode": state.current_concept_mode}
            )
        else:
            self._log_event(
                AdaptationEvent.CONCEPT_EXPLORE, skill, 0.0,
                {}, {"mode": "explore"}
            )

        return state.vector

    def maybe_adapt(self, skill: str):
        from core.difficulty_policy import get_round_caps
        state = self.skill_states.get(skill)
        if not state or state.status == "removed":
            return

        scores = list(state.recent_conf)
        n = len(scores)
        if n == 0:
            return

        # 1. Confirming mode exit logic
        if state.status == "confirming":
            recent_avg = sum(scores) / len(scores)
            if recent_avg >= _CAMPUS_LOWER_CONF:
                logger.info(f"🔄 [Campus] Skill '{skill}' recovered from confirming. Avg: {recent_avg:.2f}")
                state.status = "active"
                state.cooldown = 1
            elif recent_avg < _CAMPUS_REMOVE_CONF:
                self._remove_skill(skill)
            return

        # 2. Catastrophic first-answer failure (campus: only a truly blank answer)
        if n == 1:
            if scores[0] < _CAMPUS_REMOVE_CONF:
                self._remove_skill(skill)
            return

        # 3. Standard Rolling Adaptation — adaptive-first: narrower hold band so the
        #    next question's difficulty follows the student's live performance.
        recent_avg = sum(scores[-2:]) / min(len(scores[-2:]), 2)
        caps = get_round_caps(self.config.role_experience_required, self.config.base_difficulty, self.current_round, self.config.total_rounds)

        if recent_avg < _CAMPUS_REMOVE_CONF:
            self._remove_skill(skill)
            return

        if recent_avg < _CAMPUS_LOWER_CONF:
            self._enter_confirming_mode(skill, caps)
            return

        if recent_avg >= _CAMPUS_ESCALATE_CONF:
            self._escalate(skill, caps, recent_avg)
            return

    # ═══════════════════════════════════════════════════════════════════════
    # Phase 3C+: Strategy-Driven Escalation with Budget
    # ═══════════════════════════════════════════════════════════════════════

    def _escalate(self, skill: str, caps: dict, recent_avg: float):
        """
        Strategy-driven escalation. Only increases ONE dimension per cycle.
        Respects escalation budget. Only counts toward budget if mutation occurred.
        
        abstraction_first: A → C → Amb → S
        constraint_first:  C → Amb → S → A
        """
        state = self.skill_states.get(skill)
        if not state:
            return

        # Check escalation budget
        if state.escalation_count >= self.config.max_escalations_per_round:
            logger.info(
                f"⏸️ [Phase 3C+] Skill '{skill}' escalation budget exhausted "
                f"({state.escalation_count}/{self.config.max_escalations_per_round}). Skipping."
            )
            return

        v = state.vector
        before = v.to_dict()

        if state.current_strategy == "abstraction_first":
            order = [
                ("A", "abstraction_depth", caps["A"]),
                ("C", "constraint_pressure", caps["C"]),
                ("Amb", "ambiguity", caps["Amb"]),
                ("S", "scale_complexity", caps["S"]),
            ]
        else:  # constraint_first
            order = [
                ("C", "constraint_pressure", caps["C"]),
                ("Amb", "ambiguity", caps["Amb"]),
                ("S", "scale_complexity", caps["S"]),
                ("A", "abstraction_depth", caps["A"]),
            ]

        # Try to increase ONE dimension in priority order
        escalated_dim = None
        for dim_name, attr_name, (cap_min, cap_max) in order:
            current_val = getattr(v, attr_name)
            if current_val < cap_max:
                setattr(v, attr_name, current_val + 1)
                escalated_dim = dim_name
                break

        # Enforce caps (safety)
        v.abstraction_depth = min(max(v.abstraction_depth, caps["A"][0]), caps["A"][1])
        v.constraint_pressure = min(max(v.constraint_pressure, caps["C"][0]), caps["C"][1])
        v.ambiguity = min(max(v.ambiguity, caps["Amb"][0]), caps["Amb"][1])
        v.scale_complexity = min(max(v.scale_complexity, caps["S"][0]), caps["S"][1])

        # Only count toward budget if vector actually changed
        vector_changed = v.to_dict() != before
        if vector_changed:
            state.escalation_count += 1

        state.cooldown = 1

        # Strategy switching (symmetrical)
        self._check_strategy_switch(skill, caps, recent_avg)

        self._log_event(
            AdaptationEvent.ESCALATION, skill, recent_avg, before, v.to_dict(),
            extra=f"dim={escalated_dim} strategy={state.current_strategy} "
                  f"budget={state.escalation_count}/{self.config.max_escalations_per_round} "
                  f"mutated={vector_changed}"
        )

    def _check_strategy_switch(self, skill: str, caps: dict, recent_avg: float):
        """
        Symmetrical strategy switching. Both directions, one-time lock per round.
        
        abstraction_first → constraint_first: when A saturated, avg >= 0.90
        constraint_first → abstraction_first: when C+Amb+S ALL saturated, A NOT saturated, avg >= 0.90
        """
        state = self.skill_states.get(skill)
        if not state:
            return

        if state.strategy_locked:
            self._log_event(
                AdaptationEvent.STRATEGY_LOCKED, skill, recent_avg,
                {"strategy": state.current_strategy}, {"locked": True},
                extra="Switch blocked: already locked this round"
            )
            return

        if recent_avg < 0.90:
            return

        v = state.vector
        previous = state.current_strategy

        if state.current_strategy == "abstraction_first":
            # Switch when A is saturated
            if v.abstraction_depth >= caps["A"][1]:
                state.current_strategy = "constraint_first"
                state.strategy_locked = True
                self._log_event(
                    AdaptationEvent.STRATEGY_SWITCH, skill, recent_avg,
                    {"strategy": previous}, {"strategy": state.current_strategy},
                    extra=f"A_saturated={v.abstraction_depth}/{caps['A'][1]}"
                )

        elif state.current_strategy == "constraint_first":
            # Switch when C, Amb, S ALL saturated AND A NOT saturated
            c_sat = v.constraint_pressure >= caps["C"][1]
            amb_sat = v.ambiguity >= caps["Amb"][1]
            s_sat = v.scale_complexity >= caps["S"][1]
            a_not_sat = v.abstraction_depth < caps["A"][1]

            if c_sat and amb_sat and s_sat and a_not_sat:
                state.current_strategy = "abstraction_first"
                state.strategy_locked = True
                self._log_event(
                    AdaptationEvent.STRATEGY_SWITCH, skill, recent_avg,
                    {"strategy": previous}, {"strategy": state.current_strategy},
                    extra=f"C_sat={c_sat} Amb_sat={amb_sat} S_sat={s_sat} A_avail={a_not_sat}"
                )

    def _enter_confirming_mode(self, skill: str, caps: dict):
        """Drop to cap minimums. Does NOT consume escalation budget."""
        state = self.skill_states.get(skill)
        if not state:
            return

        before = state.vector.to_dict()
        recent_avg = sum(state.recent_conf) / len(state.recent_conf) if state.recent_conf else 0

        state.status = "confirming"
        state.vector.abstraction_depth = caps["A"][0]
        state.vector.constraint_pressure = caps["C"][0]
        state.vector.ambiguity = caps["Amb"][0]
        state.vector.scale_complexity = caps["S"][0]
        state.cooldown = 1
        # Confirming does NOT burn escalation budget

        self._log_event(
            AdaptationEvent.CONFIRMING, skill, recent_avg,
            before, state.vector.to_dict()
        )

    def _remove_skill(self, skill: str):
        state = self.skill_states.get(skill)
        if not state:
            return

        recent_avg = sum(state.recent_conf) / len(state.recent_conf) if state.recent_conf else 0
        state.status = "removed"

        self._log_event(
            AdaptationEvent.REMOVED, skill, recent_avg,
            state.vector.to_dict(), {"status": "removed"}
        )

    def soften_skill_on_skip(self, skill: str) -> str:
        """Candidate said 'I don't know'. Instead of dropping the skill, lower its
        difficulty by ONE notch and re-ask an easier question on the SAME skill.
        Only give up once it's already at the easiest level and still skipped.

        Returns:
          'retry' → difficulty lowered; re-ask the same skill (easier)
          'floor' → already easiest and still skipped → skill removed, move on
          'skip'  → no runtime state for this skill (nothing to do)
        """
        from core.difficulty_policy import get_round_caps
        state = self.skill_states.get(skill)
        if not state or state.status == "removed":
            return "skip"

        caps = get_round_caps(
            self.config.role_experience_required,
            self.config.base_difficulty,
            self.current_round,
            self.config.total_rounds,
        )
        floor_a = caps["A"][0]
        v = state.vector

        if v.abstraction_depth > floor_a:
            # Lower one notch and strip added pressure — make the re-ask genuinely easier.
            v.abstraction_depth -= 1
            v.constraint_pressure = 0
            v.ambiguity = 0
            v.scale_complexity = 0
            state.status = "active"
            # Protect this manual setting from being overwritten by maybe_adapt next turn.
            state.cooldown = 1
            self._log_event(
                AdaptationEvent.CONFIRMING, skill, 0.0,
                {}, {"reason": "skip_soften", "A": v.abstraction_depth},
            )
            return "retry"

        # Already at the easiest level and still 'I don't know' → move on.
        self._remove_skill(skill)
        return "floor"

    def soften_skill_on_skip(self, skill: str) -> str:
        """Candidate said 'I don't know'. Instead of dropping the skill, lower its
        difficulty by ONE notch and re-ask an easier question on the SAME skill.
        Only give up once it's already at the easiest level and still skipped.

        Returns:
          'retry' → difficulty lowered; re-ask the same skill (easier)
          'floor' → already easiest and still skipped → skill removed, move on
          'skip'  → no runtime state for this skill (nothing to do)
        """
        from core.difficulty_policy import get_round_caps
        state = self.skill_states.get(skill)
        if not state or state.status == "removed":
            return "skip"

        caps = get_round_caps(
            self.config.role_experience_required,
            self.config.base_difficulty,
            self.current_round,
            self.config.total_rounds,
        )
        floor_a = caps["A"][0]
        v = state.vector

        if v.abstraction_depth > floor_a:
            # Lower one notch and strip added pressure — make the re-ask genuinely easier.
            v.abstraction_depth -= 1
            v.constraint_pressure = 0
            v.ambiguity = 0
            v.scale_complexity = 0
            state.status = "active"
            # Protect this manual setting from being overwritten by maybe_adapt next turn.
            state.cooldown = 1
            self._log_event(
                AdaptationEvent.CONFIRMING, skill, 0.0,
                {}, {"reason": "skip_soften", "A": v.abstraction_depth},
            )
            return "retry"

        # Already at the easiest level and still 'I don't know' → move on.
        self._remove_skill(skill)
        return "floor"

    def soften_skill_on_skip(self, skill: str) -> str:
        """Candidate said 'I don't know'. Instead of dropping the skill, lower its
        difficulty by ONE notch and re-ask an easier question on the SAME skill.
        Only give up once it's already at the easiest level and still skipped.

        Returns:
          'retry' → difficulty lowered; re-ask the same skill (easier)
          'floor' → already easiest and still skipped → skill removed, move on
          'skip'  → no runtime state for this skill (nothing to do)
        """
        from core.difficulty_policy import get_round_caps
        state = self.skill_states.get(skill)
        if not state or state.status == "removed":
            return "skip"

        caps = get_round_caps(
            self.config.role_experience_required,
            self.config.base_difficulty,
            self.current_round,
            self.config.total_rounds,
        )
        floor_a = caps["A"][0]
        v = state.vector

        if v.abstraction_depth > floor_a:
            # Lower one notch and strip added pressure — make the re-ask genuinely easier.
            v.abstraction_depth -= 1
            v.constraint_pressure = 0
            v.ambiguity = 0
            v.scale_complexity = 0
            state.status = "active"
            # Protect this manual setting from being overwritten by maybe_adapt next turn.
            state.cooldown = 1
            self._log_event(
                AdaptationEvent.CONFIRMING, skill, 0.0,
                {}, {"reason": "skip_soften", "A": v.abstraction_depth},
            )
            return "retry"

        # Already at the easiest level and still 'I don't know' → move on.
        self._remove_skill(skill)
        return "floor"

    def soften_skill_on_skip(self, skill: str) -> str:
        """Candidate said 'I don't know'. Instead of dropping the skill, lower its
        difficulty by ONE notch and re-ask an easier question on the SAME skill.
        Only give up once it's already at the easiest level and still skipped.

        Returns:
          'retry' → difficulty lowered; re-ask the same skill (easier)
          'floor' → already easiest and still skipped → skill removed, move on
          'skip'  → no runtime state for this skill (nothing to do)
        """
        from core.difficulty_policy import get_round_caps
        state = self.skill_states.get(skill)
        if not state or state.status == "removed":
            return "skip"

        caps = get_round_caps(
            self.config.role_experience_required,
            self.config.base_difficulty,
            self.current_round,
            self.config.total_rounds,
        )
        floor_a = caps["A"][0]
        v = state.vector

        if v.abstraction_depth > floor_a:
            # Lower one notch and strip added pressure — make the re-ask genuinely easier.
            v.abstraction_depth -= 1
            v.constraint_pressure = 0
            v.ambiguity = 0
            v.scale_complexity = 0
            state.status = "active"
            # Protect this manual setting from being overwritten by maybe_adapt next turn.
            state.cooldown = 1
            self._log_event(
                AdaptationEvent.CONFIRMING, skill, 0.0,
                {}, {"reason": "skip_soften", "A": v.abstraction_depth},
            )
            return "retry"

        # Already at the easiest level and still 'I don't know' → move on.
        self._remove_skill(skill)
        return "floor"

    # Backward compatibility wrappers
    def remove_skill(self, skill: str):
        self._remove_skill(skill)

    def enter_confirming_mode(self, skill: str, caps: dict):
        self._enter_confirming_mode(skill, caps)

    def increase_pressure(self, skill: str, caps: dict):
        """Backward-compatible wrapper — delegates to strategy-driven escalation."""
        recent_avg = 0.85
        state = self.skill_states.get(skill)
        if state and state.recent_conf:
            recent_avg = sum(list(state.recent_conf)[-2:]) / min(len(state.recent_conf), 2)
        self._escalate(skill, caps, recent_avg)

    # ═══════════════════════════════════════════════════════════════════════
    # Phase 3C+: Structured Logging
    # ═══════════════════════════════════════════════════════════════════════

    def _log_event(self, event: AdaptationEvent, skill: str, confidence_avg: float,
                   before: dict, after: dict, extra: str = ""):
        """Structured log with standardized event enum and consistent fields."""
        state = self.skill_states.get(skill)
        strategy = state.current_strategy if state else "n/a"

        logger.info(
            f"📊 [{event.value}] "
            f"skill='{skill}' | "
            f"R{self.current_round} | "
            f"conf_avg={confidence_avg:.2f} | "
            f"strategy={strategy} | "
            f"before={before} | "
            f"after={after}"
            + (f" | {extra}" if extra else "")
        )

    # Backward compatibility alias
    def _log_vector_change(self, event_type: str, skill: str, confidence_avg: float,
                           before: dict, after: dict, extra: str = ""):
        # Map string to enum
        event_map = {v.value: v for v in AdaptationEvent}
        event = event_map.get(event_type, AdaptationEvent.ESCALATION)
        self._log_event(event, skill, confidence_avg, before, after, extra)

    @property
    def is_interview_complete(self) -> bool:
        return self.current_round > self.config.total_rounds
