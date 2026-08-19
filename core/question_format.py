"""Question format planning — the QuestionFormat enum, QuestionFormatTracker, difficulty
strategies, and the deterministic slot helpers. Split out of responses_client for modularity.
Lowest layer: skill_tracker and responses_client import from here."""
import os
import math
import logging
from typing import List, Dict, Tuple, Set, Optional
from dataclasses import dataclass, field
from enum import Enum
from config_loader import NUMBER_OF_QUESTIONS

logger = logging.getLogger("question_format")
RESUME_QUESTION_PERCENTAGE = float(os.getenv("RESUME_QUESTION_PERCENTAGE", "0.4"))
_GOLDEN = 0.6180339887498949  # golden-ratio conjugate → well-spread fractional sequence


def get_resume_slots(total_questions: int = NUMBER_OF_QUESTIONS, 
                     percentage: float = RESUME_QUESTION_PERCENTAGE) -> list:
    """
    Calculate deterministic question numbers for resume-based questions.
    
    Resume slots are evenly distributed in the middle of the Q3-QN range.
    Never exceeds floor(technical_questions * percentage).
    
    Example: 15 total questions, 20% → floor(13 * 0.2) = 2 resume Qs → [Q6, Q10]
    Example: 10 total questions, 20% → floor(8 * 0.2) = 1 resume Q  → [Q6]
    """
    technical_questions = total_questions - 2  # Exclude Q1 (greeting) and Q2 (background)

    # Very short interviews (questions_per_round <= 2) leave no technical slots.
    # Guard against this: a negative technical_questions makes resume_count negative,
    # which slips past an "== 0" check and blows up at `/ (resume_count + 1)`.
    if technical_questions <= 0:
        return []

    resume_count = math.floor(technical_questions * percentage)

    if resume_count <= 0:
        return []
    
    # Distribute evenly in the Q3-QN range
    start = 3  # First technical question
    end = total_questions  # Last question
    
    # Calculate even spacing
    step = technical_questions / (resume_count + 1)
    slots = []
    for i in range(1, resume_count + 1):
        slot = start + round(step * i) - 1
        slot = max(start + 1, min(slot, end - 1))  # Keep away from Q3 and last Q
        if slot not in slots:
            slots.append(slot)
    
    return sorted(slots)


def _even_count_slots(count: int, lo: int, hi: int, exclude=None) -> list:
    """Return `count` question indices evenly spaced across [lo, hi], skipping any in
    `exclude`. Used by the deterministic (source × style) mode to place an EXACT number
    of resume/scenario slots — unlike get_resume_slots which works off a percentage."""
    exclude = exclude or set()
    avail = [q for q in range(lo, hi + 1) if q not in exclude]
    if count <= 0 or not avail:
        return []
    count = min(int(count), len(avail))
    picks = []
    for i in range(count):
        idx = min(len(avail) - 1, int((i + 0.5) * len(avail) / count))
        picks.append(avail[idx])
    return sorted(set(picks))


def _det_frac(n: int) -> float:
    """Deterministic pseudo-uniform fraction in [0,1) for question index n."""
    return (int(n) * _GOLDEN) % 1.0


def _det_pick(seq, n: int):
    """Deterministically pick one item from seq by rotating on n. Sets are sorted by
    a stable key first so iteration order can't vary run to run."""
    items = list(seq)
    if not items:
        return None
    try:
        items = sorted(items, key=lambda x: getattr(x, "value", x))
    except TypeError:
        items = list(seq)
    return items[int(n) % len(items)]


class QuestionFormat(Enum):
    """Question format types for audio-based interviews (no visual/writing tasks)."""
    DIRECT_EXPERIENCE = "direct_experience"
    SCENARIO_BASED = "scenario_based"
    VERBAL_ARCHITECTURE = "verbal_architecture"
    EXPERIMENTAL = "experimental"
    RESUME_PROJECT = "resume_project"
    TROUBLESHOOTING = "troubleshooting"
    COMPARISON = "comparison"
    IMPLEMENTATION_WALKTHROUGH = "implementation_walkthrough"
    CONCEPTUAL = "conceptual"


_SCENARIO_FAMILY = (
    QuestionFormat.SCENARIO_BASED,
    QuestionFormat.TROUBLESHOOTING,
    QuestionFormat.EXPERIMENTAL,
    QuestionFormat.IMPLEMENTATION_WALKTHROUGH,
    QuestionFormat.VERBAL_ARCHITECTURE,
    QuestionFormat.COMPARISON,
)


DIFFICULTY_STRATEGIES = {
    "junior": {
        "allowed_formats": [
            QuestionFormat.SCENARIO_BASED,
            QuestionFormat.IMPLEMENTATION_WALKTHROUGH,
            QuestionFormat.COMPARISON,
            QuestionFormat.DIRECT_EXPERIENCE,
            QuestionFormat.RESUME_PROJECT,
            QuestionFormat.CONCEPTUAL
        ],
        "skill_count_weights": [80, 20, 0]  # Mostly 1 skill, rarely 2
    },
    "mid": {
        "allowed_formats": [
            QuestionFormat.SCENARIO_BASED,
            QuestionFormat.TROUBLESHOOTING,
            QuestionFormat.IMPLEMENTATION_WALKTHROUGH,
            QuestionFormat.COMPARISON,
            QuestionFormat.EXPERIMENTAL,
            QuestionFormat.DIRECT_EXPERIENCE,
            QuestionFormat.RESUME_PROJECT,
            QuestionFormat.CONCEPTUAL
        ],
        "skill_count_weights": [40, 50, 10]  # Mix of 1 and 2 skills
    },
    "senior": {
        "allowed_formats": [
            # All formats including Architecture
            QuestionFormat.SCENARIO_BASED,
            QuestionFormat.TROUBLESHOOTING,
            QuestionFormat.IMPLEMENTATION_WALKTHROUGH,
            QuestionFormat.COMPARISON,
            QuestionFormat.EXPERIMENTAL,
            QuestionFormat.VERBAL_ARCHITECTURE,
            QuestionFormat.DIRECT_EXPERIENCE,
            QuestionFormat.RESUME_PROJECT,
            QuestionFormat.CONCEPTUAL
        ],
        "skill_count_weights": [20, 40, 40]  # Heavily multi-skill
    }
}


@dataclass
class QuestionFormatTracker:
    """
    Tracks and determines question formats throughout the interview.
    Ensures diversity and enforces quotas:
    - Resume vs JD question split driven by resume_ratio from InterviewConfig
    - Deep-dive mode (resume_ratio >= 0.8): ~80% project digs, ~20% skill checks
    - Max 2 direct experience questions
    - 2-3 experimental/hypothetical questions
    - Concept vs Scenario quotas (when configured)
    - Anti-repetition: avoids using same format consecutively
    """
    total_questions: int = NUMBER_OF_QUESTIONS - 2  # Technical questions only
    _total_q_with_greeting: int = NUMBER_OF_QUESTIONS  # Total including Q1+Q2
    resume_ratio: float = RESUME_QUESTION_PERCENTAGE  # From InterviewConfig or env
    
    # Format usage tracking
    _format_count: Dict[QuestionFormat, int] = field(default_factory=dict)
    _question_formats: Dict[int, QuestionFormat] = field(default_factory=dict)
    
    # Constraints
    max_direct_experience: int = 2
    target_experimental: tuple = (2, 3)
    max_resume_questions: int = 0  # Set in __post_init__ from resume_ratio
    
    # Concept vs Scenario quotas (optional — from InterviewConfig)
    target_conceptual: Optional[int] = None
    target_scenario: Optional[int] = None
    target_resume: Optional[int] = None       # explicit resume count (deterministic mode)

    # Instance-level resume slots (computed from ratio, not module-level)
    _resume_slots: list = field(default_factory=list)
    _scenario_slots: list = field(default_factory=list)   # deterministic style slots
    _explicit_mode: bool = False                          # honor exact source×style counts
    
    # Deep-dive mode flag
    project_deep_dive_mode: bool = False
    
    def __post_init__(self):
        """Initialize format counts and compute resume slots from ratio."""
        for fmt in QuestionFormat:
            self._format_count[fmt] = 0
        # Sync total_questions if _total_q_with_greeting was set from config
        if self._total_q_with_greeting != NUMBER_OF_QUESTIONS:
            self.total_questions = self._total_q_with_greeting - 2
        
        # Compute resume slots from ratio (per-instance, not module-level)
        self._resume_slots = get_resume_slots(
            total_questions=self._total_q_with_greeting,
            percentage=self.resume_ratio
        )
        self.max_resume_questions = len(self._resume_slots)

        # ── Deterministic (source × style) mode ──────────────────────────────
        # When the config gives explicit conceptual/scenario counts, honor ALL four
        # counts EXACTLY via deterministic slots: source = resume slots, style =
        # scenario slots drawn from the NON-resume questions, everything else
        # conceptual. Resume questions are conceptual-styled and count toward the
        # conceptual total — so e.g. jd=6,resume=4,conceptual=8,scenario=2 →
        # 4 resume + 4 skill-conceptual + 2 skill-scenario = 8 conceptual + 2 scenario.
        # Bypasses the ratio/deep-dive/weighted-pool path (kept for legacy configs).
        self._explicit_mode = (self.target_conceptual is not None or self.target_scenario is not None)
        if self._explicit_mode:
            # The loop asks the qpr content questions as cq=2..qpr+1, and get_next_format
            # receives those same numbers. With _total_q_with_greeting = qpr+2, the asked
            # content range is [2, _total_q_with_greeting-1] = exactly qpr questions.
            lo, hi = 2, self._total_q_with_greeting - 1
            self._resume_slots = _even_count_slots(int(self.target_resume or 0), lo, hi)
            self._scenario_slots = _even_count_slots(
                int(self.target_scenario or 0), lo, hi, exclude=set(self._resume_slots))
            self.max_resume_questions = len(self._resume_slots)
            self.project_deep_dive_mode = False
            logger.info(
                f"📋 Deterministic format mode (Q{lo}-Q{hi}): "
                f"resume_slots={self._resume_slots}→RESUME_PROJECT, "
                f"scenario_slots={self._scenario_slots}→SCENARIO_BASED, rest→CONCEPTUAL "
                f"(conceptual total={(hi - lo + 1) - len(self._scenario_slots)})")
            return

        # Deep-dive mode activates at >= 80% resume ratio
        self.project_deep_dive_mode = self.resume_ratio >= 0.8
        
        if self.project_deep_dive_mode:
            logger.info(
                f"🎯 PROJECT DEEP-DIVE MODE activated (resume_ratio={self.resume_ratio:.0%}). "
                f"Resume slots: {self._resume_slots} ({self.max_resume_questions} of {self.total_questions} technical Qs)"
            )
        else:
            logger.info(
                f"📋 Standard mode (resume_ratio={self.resume_ratio:.0%}). "
                f"Resume slots: {self._resume_slots} ({self.max_resume_questions} of {self.total_questions} technical Qs)"
            )
    
    def get_next_format(self, 
                       question_number: int,
                       has_resume_relevance: bool = True,
                       is_blend_question: bool = False,
                       role: str = "mid") -> QuestionFormat:
        """
        Determine the next question format based on role strategy, quotas,
        and resume_ratio. In deep-dive mode (>=80%), heavily favors RESUME_PROJECT.
        """
        # ── Deterministic (source × style) mode ──
        # Exact source×format honoring for explicit-count configs (see __post_init__).
        # source = resume slot → RESUME_PROJECT; style = scenario slot → SCENARIO_BASED;
        # everything else → CONCEPTUAL. This is a hard, exact schedule.
        if self._explicit_mode:
            if question_number in self._resume_slots:
                return QuestionFormat.RESUME_PROJECT
            if question_number in self._scenario_slots:
                return QuestionFormat.SCENARIO_BASED
            return QuestionFormat.CONCEPTUAL

        role_key = role.lower() if role else "mid"
        strategy = DIFFICULTY_STRATEGIES.get(role_key, DIFFICULTY_STRATEGIES["mid"])
        allowed_formats = set(strategy["allowed_formats"])
        
        remaining_questions = self._total_q_with_greeting - question_number + 1
        resume_count = self._format_count.get(QuestionFormat.RESUME_PROJECT, 0)
        
        # ═══════════════════════════════════════════════════════════════
        # PROJECT DEEP-DIVE MODE (resume_ratio >= 80%)
        # ═══════════════════════════════════════════════════════════════
        if self.project_deep_dive_mode and question_number >= 3:
            # In deep-dive mode, use percentage-based allocation:
            # - resume_ratio % of questions → RESUME_PROJECT
            # - remaining % → other formats (skill-based variety)
            total_technical = self.total_questions  # e.g., 13 for 15 total
            target_resume_qs = math.floor(total_technical * self.resume_ratio)
            
            if (has_resume_relevance and 
                resume_count < target_resume_qs and
                QuestionFormat.RESUME_PROJECT in allowed_formats):
                # Don't do resume back-to-back more than 2 times in a row
                recent_resume_streak = 0
                for q in range(question_number - 1, max(2, question_number - 3), -1):
                    if self._question_formats.get(q) == QuestionFormat.RESUME_PROJECT:
                        recent_resume_streak += 1
                    else:
                        break
                
                if recent_resume_streak < 2:
                    return QuestionFormat.RESUME_PROJECT
                else:
                    # Break the streak with a variety question, then resume
                    logger.info(
                        f"🔄 Deep-dive streak break at Q{question_number} "
                        f"(resume={resume_count}/{target_resume_qs}, streak={recent_resume_streak})"
                    )
                    # Fall through to weighted pool below
            elif resume_count >= target_resume_qs:
                logger.info(
                    f"✅ Resume quota met ({resume_count}/{target_resume_qs}). "
                    f"Q{question_number} will be skill-based."
                )
                # Fall through to weighted pool for skill-based questions
        
        # ═══════════════════════════════════════════════════════════════
        # STANDARD MODE (or deep-dive overflow to skill-based)
        # ═══════════════════════════════════════════════════════════════
        
        # Last question logic - Role Aware
        if question_number == self._total_q_with_greeting:
            # Honor the conceptual quota even on the final question — otherwise the
            # capstone scenario/verbal-architecture default silently drops the last
            # conceptual slot (breaking e.g. a 100%-conceptual config).
            conceptual_count = self._format_count.get(QuestionFormat.CONCEPTUAL, 0)
            if (self.target_conceptual is not None and
                conceptual_count < self.target_conceptual and
                QuestionFormat.CONCEPTUAL in allowed_formats):
                return QuestionFormat.CONCEPTUAL
            if role_key == "junior":
                potentials = [f for f in allowed_formats if f in (QuestionFormat.TROUBLESHOOTING, QuestionFormat.SCENARIO_BASED, QuestionFormat.IMPLEMENTATION_WALKTHROUGH)]
                return _det_pick(potentials, question_number) if potentials else _det_pick(allowed_formats, question_number)
            if QuestionFormat.VERBAL_ARCHITECTURE in allowed_formats:
                return QuestionFormat.VERBAL_ARCHITECTURE
            return QuestionFormat.SCENARIO_BASED
        
        # ─── DETERMINISTIC RESUME SLOTS (standard mode) ───
        if (not self.project_deep_dive_mode and
            question_number in self._resume_slots and 
            has_resume_relevance and 
            resume_count < self.max_resume_questions and
            QuestionFormat.RESUME_PROJECT in allowed_formats):
            return QuestionFormat.RESUME_PROJECT
        
        # ─── CONCEPT/SCENARIO QUOTA ENFORCEMENT ───
        # Runs BEFORE the experimental/direct-experience defaults so the explicit
        # conceptual/scenario config from interview_config/.env is actually honored
        # (previously those defaults returned first and starved CONCEPTUAL entirely).
        conceptual_count = self._format_count.get(QuestionFormat.CONCEPTUAL, 0)
        scenario_formats = sum(self._format_count.get(f, 0) for f in _SCENARIO_FAMILY)

        conceptual_deficit = (
            max(0, self.target_conceptual - conceptual_count)
            if self.target_conceptual is not None else 0
        )
        scenario_deficit = (
            max(0, self.target_scenario - scenario_formats)
            if self.target_scenario is not None else 0
        )
        scenario_pool = [f for f in _SCENARIO_FAMILY if f in allowed_formats]
        can_conceptual = conceptual_deficit > 0 and QuestionFormat.CONCEPTUAL in allowed_formats
        can_scenario = scenario_deficit > 0 and bool(scenario_pool)

        # ─── BUDGET-ARBITER SCHEDULER ───
        # Reconcile the conceptual + scenario deficits against the slots left in
        # ONE decision (resume is already placed by its deterministic slots above).
        # Using the COMBINED deficit for the "must place now" test is the key fix:
        # previously each quota checked only its OWN deficit vs remaining, so when
        # both were behind, neither individually filled the slot and a free/variety
        # question slipped in — silently starving one of them.
        if can_conceptual or can_scenario:
            remaining = max(1, self._total_q_with_greeting - question_number)
            must_place = (conceptual_deficit + scenario_deficit) >= remaining
            # When forced, spend the slot on whichever quota is more urgent so both
            # converge to target; ties go to conceptual (it's the scarcer shape).
            prefer_conceptual = can_conceptual and (
                not can_scenario or conceptual_deficit >= scenario_deficit
            )
            if must_place:
                if prefer_conceptual:
                    return QuestionFormat.CONCEPTUAL
                if can_scenario:
                    return _det_pick(scenario_pool, question_number)
                return QuestionFormat.CONCEPTUAL
            # Slack remains → place deterministically (low-discrepancy) so quotas still
            # spread across the interview, but reproducibly (was random.random()).
            if can_conceptual:
                p = min(1.0, max(0.3, conceptual_deficit / remaining))
                if _det_frac(question_number) < p:
                    return QuestionFormat.CONCEPTUAL
            if can_scenario:
                p = min(1.0, max(0.3, scenario_deficit / remaining))
                if _det_frac(question_number * 2 + 1) < p:
                    return _det_pick(scenario_pool, question_number)

        # Check mandatory quotas (EXPERIMENTAL)
        experimental_count = self._format_count.get(QuestionFormat.EXPERIMENTAL, 0)
        direct_count = self._format_count.get(QuestionFormat.DIRECT_EXPERIENCE, 0)

        # Force experimental quota if low and allowed (only in standard mode).
        # Skip once the scenario budget is met (experimental is a scenario type) so
        # it can't blow past target_scenario.
        if (not self.project_deep_dive_mode and
            QuestionFormat.EXPERIMENTAL in allowed_formats and
            experimental_count < self.target_experimental[0] and
            remaining_questions <= 5 and
            (self.target_scenario is None or scenario_formats < self.target_scenario)):
            return QuestionFormat.EXPERIMENTAL

        # Early questions (Q3-Q5): Allow Direct Experience (standard mode only)
        if (not self.project_deep_dive_mode and
            question_number <= 5 and
            direct_count < self.max_direct_experience and
            QuestionFormat.DIRECT_EXPERIENCE in allowed_formats):
            if _det_frac(question_number) < 0.3:
                return QuestionFormat.DIRECT_EXPERIENCE

        # Build weighted pool from ALLOWED formats
        weighted_pool = []

        recent_formats = []
        for q in range(max(3, question_number - 2), question_number):
            if q in self._question_formats:
                recent_formats.append(self._question_formats[q])
        
        # Leftover slots (the follow-up reserve when a follow-up didn't fire) should
        # fall back to the configured shape, not random variety. So when a shape is
        # the SOLE configured quota, don't strip it once met — let it fill the
        # remainder (keeps a 100%-conceptual interview conceptual end-to-end).
        conceptual_is_sole = self.target_conceptual is not None and not self.target_scenario
        scenario_is_sole = self.target_scenario is not None and not self.target_conceptual

        # Suppress CONCEPTUAL if quota met (unless it's the sole configured shape)
        if (self.target_conceptual is not None and
                conceptual_count >= self.target_conceptual and not conceptual_is_sole):
            allowed_formats = allowed_formats - {QuestionFormat.CONCEPTUAL}

        # Suppress SCENARIO types if scenario quota met (unless it's the sole shape)
        if (self.target_scenario is not None and
                scenario_formats >= self.target_scenario and not scenario_is_sole):
            allowed_formats = allowed_formats - set(_SCENARIO_FAMILY)
        
        # Define base weights for all major types.
        # Rebalanced (2026-06): the old weights summed scenario-family formats to
        # 17 vs conceptual 3, so ~85% of questions came out as "design a system
        # that handles N" — the repetitive template seen in production. Conceptual,
        # comparison and troubleshooting are now first-class so the question SHAPE
        # varies, not just the skill/number.
        all_weighted_formats = [
            (QuestionFormat.SCENARIO_BASED, 3),
            (QuestionFormat.VERBAL_ARCHITECTURE, 2),
            (QuestionFormat.TROUBLESHOOTING, 3),
            (QuestionFormat.COMPARISON, 3),
            (QuestionFormat.EXPERIMENTAL, 2),
            (QuestionFormat.IMPLEMENTATION_WALKTHROUGH, 2),
            (QuestionFormat.CONCEPTUAL, 4),
        ]

        # "Design a system that handles N" family — these share one surface form.
        # If a recent question was any of them, heavily downweight the WHOLE family
        # so the next question takes a different shape (conceptual / comparison /
        # troubleshooting) instead of just swapping SCENARIO → ARCHITECTURE.
        design_family = {
            QuestionFormat.SCENARIO_BASED,
            QuestionFormat.VERBAL_ARCHITECTURE,
            QuestionFormat.IMPLEMENTATION_WALKTHROUGH,
        }
        recent_was_design = any(f in design_family for f in recent_formats)

        # Filter by allowed_formats
        available_formats = [
            (fmt, w) for fmt, w in all_weighted_formats if fmt in allowed_formats
        ]

        # Boost Experimental if under target
        if QuestionFormat.EXPERIMENTAL in allowed_formats and experimental_count < self.target_experimental[1]:
             available_formats = [(fmt, w if fmt != QuestionFormat.EXPERIMENTAL else 5) for fmt, w in available_formats]

        for fmt, base_weight in available_formats:
            weight = base_weight
            # Penalize the exact format if used in the last 2 questions…
            if fmt in recent_formats:
                weight = max(1, weight // 2)
            # …and penalize the whole design-a-system family right after one, so
            # we don't just trade one "build a system" question for another.
            if recent_was_design and fmt in design_family:
                weight = max(1, weight // 3)
            weighted_pool.extend([fmt] * weight)
        
        if not weighted_pool:
            return _det_pick(allowed_formats, question_number)

        # Deterministic rotation over the weighted pool preserves the weighting
        # (more copies → more indices) without run-to-run RNG.
        return weighted_pool[question_number % len(weighted_pool)]
    
    def register_format(self, question_number: int, format_type: QuestionFormat):
        """Register the format used for a question."""
        self._format_count[format_type] = self._format_count.get(format_type, 0) + 1
        self._question_formats[question_number] = format_type

    # ═══════════════════════════════════════════════════════════════════════
    # QUESTION BUDGET ARBITER
    # Reconciles ALL configured quotas (conceptual + scenario + resume) against
    # the fixed number of question slots, so no single quota is silently starved
    # by another quota or by an experience follow-up. Only active when a "shape"
    # quota (conceptual/scenario) is configured — default/resume-only interviews
    # are completely unaffected, so this is safe for technical, non-technical and
    # any experience level.
    # ═══════════════════════════════════════════════════════════════════════

    def _has_shape_quota(self) -> bool:
        """True when the interview_config pinned a conceptual/scenario mix."""
        return self.target_conceptual is not None or self.target_scenario is not None

    def remaining_quota_deficit(self, upcoming_qnum: int, pending_format: 'QuestionFormat' = None):
        """(total_deficit, slots_remaining) at `upcoming_qnum`.

        total_deficit = how many quota questions (conceptual + scenario + resume)
        are still owed. Once a shape quota is configured, resume is counted too so
        its deterministic slots aren't eaten by follow-ups. slots_remaining counts
        the upcoming question itself.

        `pending_format` folds in the last-DECIDED question whose format the
        tracker hasn't registered yet (registration is deferred to the next
        planner call). Without it the deficit is overcounted by one and follow-ups
        are over-suppressed.
        """
        conceptual_count = self._format_count.get(QuestionFormat.CONCEPTUAL, 0)
        scenario_count = sum(self._format_count.get(f, 0) for f in _SCENARIO_FAMILY)
        resume_count = self._format_count.get(QuestionFormat.RESUME_PROJECT, 0)
        if pending_format == QuestionFormat.CONCEPTUAL:
            conceptual_count += 1
        elif pending_format in _SCENARIO_FAMILY:
            scenario_count += 1
        elif pending_format == QuestionFormat.RESUME_PROJECT:
            resume_count += 1

        deficit = 0
        if self.target_conceptual is not None:
            deficit += max(0, self.target_conceptual - conceptual_count)
        if self.target_scenario is not None:
            deficit += max(0, self.target_scenario - scenario_count)
        deficit += max(0, self.max_resume_questions - resume_count)

        remaining = max(1, self._total_q_with_greeting - upcoming_qnum + 1)
        return deficit, remaining

    def quota_needs_slot(self, upcoming_qnum: int, pending_format: 'QuestionFormat' = None) -> bool:
        """True when the remaining quotas need EVERY remaining slot — i.e. a
        non-quota question (an experience follow-up, or free variety) can no
        longer be afforded without missing a target.

        Drives (a) follow-up suppression and (b) starting quota enforcement from
        the first technical question. Returns False whenever no shape quota is
        configured, leaving default interviews' follow-up behavior untouched.
        """
        if not self._has_shape_quota():
            return False
        deficit, remaining = self.remaining_quota_deficit(upcoming_qnum, pending_format)
        return deficit >= remaining

    def adherence_report(self, pending_format: 'QuestionFormat' = None) -> str:
        """One-line 'planned vs delivered' summary per quota — observability so
        drift between the configured mix and what was actually asked is visible
        instead of silent. Returns '' when no shape quota was configured.

        `pending_format` is the format of the FINAL question, which the tracker
        registers on the (never-arriving) next call — pass it so the last
        question is counted instead of silently dropped from the totals.
        """
        if not self._has_shape_quota():
            return ""
        conceptual_count = self._format_count.get(QuestionFormat.CONCEPTUAL, 0)
        scenario_count = sum(self._format_count.get(f, 0) for f in _SCENARIO_FAMILY)
        resume_count = self._format_count.get(QuestionFormat.RESUME_PROJECT, 0)
        # Fold in the last question's pending (unregistered) format.
        if pending_format == QuestionFormat.CONCEPTUAL:
            conceptual_count += 1
        elif pending_format in _SCENARIO_FAMILY:
            scenario_count += 1
        elif pending_format == QuestionFormat.RESUME_PROJECT:
            resume_count += 1
        parts = []
        if self.target_conceptual is not None:
            parts.append(f"conceptual {conceptual_count}/{self.target_conceptual}")
        if self.target_scenario is not None:
            parts.append(f"scenario {scenario_count}/{self.target_scenario}")
        parts.append(f"resume {resume_count}/{self.max_resume_questions}")
        return "📊 Quota adherence (delivered/planned): " + ", ".join(parts)

    # ═══════════════════════════════════════════════════════════════════════
    # QUESTION BUDGET ARBITER
    # Reconciles ALL configured quotas (conceptual + scenario + resume) against
    # the fixed number of question slots, so no single quota is silently starved
    # by another quota or by an experience follow-up. Only active when a "shape"
    # quota (conceptual/scenario) is configured — default/resume-only interviews
    # are completely unaffected, so this is safe for technical, non-technical and
    # any experience level.
    # ═══════════════════════════════════════════════════════════════════════

    def _has_shape_quota(self) -> bool:
        """True when the interview_config pinned a conceptual/scenario mix."""
        return self.target_conceptual is not None or self.target_scenario is not None

    def remaining_quota_deficit(self, upcoming_qnum: int, pending_format: 'QuestionFormat' = None):
        """(total_deficit, slots_remaining) at `upcoming_qnum`.

        total_deficit = how many quota questions (conceptual + scenario + resume)
        are still owed. Once a shape quota is configured, resume is counted too so
        its deterministic slots aren't eaten by follow-ups. slots_remaining counts
        the upcoming question itself.

        `pending_format` folds in the last-DECIDED question whose format the
        tracker hasn't registered yet (registration is deferred to the next
        planner call). Without it the deficit is overcounted by one and follow-ups
        are over-suppressed.
        """
        conceptual_count = self._format_count.get(QuestionFormat.CONCEPTUAL, 0)
        scenario_count = sum(self._format_count.get(f, 0) for f in _SCENARIO_FAMILY)
        resume_count = self._format_count.get(QuestionFormat.RESUME_PROJECT, 0)
        if pending_format == QuestionFormat.CONCEPTUAL:
            conceptual_count += 1
        elif pending_format in _SCENARIO_FAMILY:
            scenario_count += 1
        elif pending_format == QuestionFormat.RESUME_PROJECT:
            resume_count += 1

        deficit = 0
        if self.target_conceptual is not None:
            deficit += max(0, self.target_conceptual - conceptual_count)
        if self.target_scenario is not None:
            deficit += max(0, self.target_scenario - scenario_count)
        deficit += max(0, self.max_resume_questions - resume_count)

        remaining = max(1, self._total_q_with_greeting - upcoming_qnum + 1)
        return deficit, remaining

    def quota_needs_slot(self, upcoming_qnum: int, pending_format: 'QuestionFormat' = None) -> bool:
        """True when the remaining quotas need EVERY remaining slot — i.e. a
        non-quota question (an experience follow-up, or free variety) can no
        longer be afforded without missing a target.

        Drives (a) follow-up suppression and (b) starting quota enforcement from
        the first technical question. Returns False whenever no shape quota is
        configured, leaving default interviews' follow-up behavior untouched.
        """
        if not self._has_shape_quota():
            return False
        deficit, remaining = self.remaining_quota_deficit(upcoming_qnum, pending_format)
        return deficit >= remaining

    def adherence_report(self, pending_format: 'QuestionFormat' = None) -> str:
        """One-line 'planned vs delivered' summary per quota — observability so
        drift between the configured mix and what was actually asked is visible
        instead of silent. Returns '' when no shape quota was configured.

        `pending_format` is the format of the FINAL question, which the tracker
        registers on the (never-arriving) next call — pass it so the last
        question is counted instead of silently dropped from the totals.
        """
        if not self._has_shape_quota():
            return ""
        conceptual_count = self._format_count.get(QuestionFormat.CONCEPTUAL, 0)
        scenario_count = sum(self._format_count.get(f, 0) for f in _SCENARIO_FAMILY)
        resume_count = self._format_count.get(QuestionFormat.RESUME_PROJECT, 0)
        # Fold in the last question's pending (unregistered) format.
        if pending_format == QuestionFormat.CONCEPTUAL:
            conceptual_count += 1
        elif pending_format in _SCENARIO_FAMILY:
            scenario_count += 1
        elif pending_format == QuestionFormat.RESUME_PROJECT:
            resume_count += 1
        parts = []
        if self.target_conceptual is not None:
            parts.append(f"conceptual {conceptual_count}/{self.target_conceptual}")
        if self.target_scenario is not None:
            parts.append(f"scenario {scenario_count}/{self.target_scenario}")
        parts.append(f"resume {resume_count}/{self.max_resume_questions}")
        return "📊 Quota adherence (delivered/planned): " + ", ".join(parts)

    # ═══════════════════════════════════════════════════════════════════════
    # QUESTION BUDGET ARBITER
    # Reconciles ALL configured quotas (conceptual + scenario + resume) against
    # the fixed number of question slots, so no single quota is silently starved
    # by another quota or by an experience follow-up. Only active when a "shape"
    # quota (conceptual/scenario) is configured — default/resume-only interviews
    # are completely unaffected, so this is safe for technical, non-technical and
    # any experience level.
    # ═══════════════════════════════════════════════════════════════════════

    def _has_shape_quota(self) -> bool:
        """True when the interview_config pinned a conceptual/scenario mix."""
        return self.target_conceptual is not None or self.target_scenario is not None

    def remaining_quota_deficit(self, upcoming_qnum: int, pending_format: 'QuestionFormat' = None):
        """(total_deficit, slots_remaining) at `upcoming_qnum`.

        total_deficit = how many quota questions (conceptual + scenario + resume)
        are still owed. Once a shape quota is configured, resume is counted too so
        its deterministic slots aren't eaten by follow-ups. slots_remaining counts
        the upcoming question itself.

        `pending_format` folds in the last-DECIDED question whose format the
        tracker hasn't registered yet (registration is deferred to the next
        planner call). Without it the deficit is overcounted by one and follow-ups
        are over-suppressed.
        """
        conceptual_count = self._format_count.get(QuestionFormat.CONCEPTUAL, 0)
        scenario_count = sum(self._format_count.get(f, 0) for f in _SCENARIO_FAMILY)
        resume_count = self._format_count.get(QuestionFormat.RESUME_PROJECT, 0)
        if pending_format == QuestionFormat.CONCEPTUAL:
            conceptual_count += 1
        elif pending_format in _SCENARIO_FAMILY:
            scenario_count += 1
        elif pending_format == QuestionFormat.RESUME_PROJECT:
            resume_count += 1

        deficit = 0
        if self.target_conceptual is not None:
            deficit += max(0, self.target_conceptual - conceptual_count)
        if self.target_scenario is not None:
            deficit += max(0, self.target_scenario - scenario_count)
        deficit += max(0, self.max_resume_questions - resume_count)

        remaining = max(1, self._total_q_with_greeting - upcoming_qnum + 1)
        return deficit, remaining

    def quota_needs_slot(self, upcoming_qnum: int, pending_format: 'QuestionFormat' = None) -> bool:
        """True when the remaining quotas need EVERY remaining slot — i.e. a
        non-quota question (an experience follow-up, or free variety) can no
        longer be afforded without missing a target.

        Drives (a) follow-up suppression and (b) starting quota enforcement from
        the first technical question. Returns False whenever no shape quota is
        configured, leaving default interviews' follow-up behavior untouched.
        """
        if not self._has_shape_quota():
            return False
        deficit, remaining = self.remaining_quota_deficit(upcoming_qnum, pending_format)
        return deficit >= remaining

    def adherence_report(self, pending_format: 'QuestionFormat' = None) -> str:
        """One-line 'planned vs delivered' summary per quota — observability so
        drift between the configured mix and what was actually asked is visible
        instead of silent. Returns '' when no shape quota was configured.

        `pending_format` is the format of the FINAL question, which the tracker
        registers on the (never-arriving) next call — pass it so the last
        question is counted instead of silently dropped from the totals.
        """
        if not self._has_shape_quota():
            return ""
        conceptual_count = self._format_count.get(QuestionFormat.CONCEPTUAL, 0)
        scenario_count = sum(self._format_count.get(f, 0) for f in _SCENARIO_FAMILY)
        resume_count = self._format_count.get(QuestionFormat.RESUME_PROJECT, 0)
        # Fold in the last question's pending (unregistered) format.
        if pending_format == QuestionFormat.CONCEPTUAL:
            conceptual_count += 1
        elif pending_format in _SCENARIO_FAMILY:
            scenario_count += 1
        elif pending_format == QuestionFormat.RESUME_PROJECT:
            resume_count += 1
        parts = []
        if self.target_conceptual is not None:
            parts.append(f"conceptual {conceptual_count}/{self.target_conceptual}")
        if self.target_scenario is not None:
            parts.append(f"scenario {scenario_count}/{self.target_scenario}")
        parts.append(f"resume {resume_count}/{self.max_resume_questions}")
        return "📊 Quota adherence (delivered/planned): " + ", ".join(parts)

    # ═══════════════════════════════════════════════════════════════════════
    # QUESTION BUDGET ARBITER
    # Reconciles ALL configured quotas (conceptual + scenario + resume) against
    # the fixed number of question slots, so no single quota is silently starved
    # by another quota or by an experience follow-up. Only active when a "shape"
    # quota (conceptual/scenario) is configured — default/resume-only interviews
    # are completely unaffected, so this is safe for technical, non-technical and
    # any experience level.
    # ═══════════════════════════════════════════════════════════════════════

    def _has_shape_quota(self) -> bool:
        """True when the interview_config pinned a conceptual/scenario mix."""
        return self.target_conceptual is not None or self.target_scenario is not None

    def remaining_quota_deficit(self, upcoming_qnum: int, pending_format: 'QuestionFormat' = None):
        """(total_deficit, slots_remaining) at `upcoming_qnum`.

        total_deficit = how many quota questions (conceptual + scenario + resume)
        are still owed. Once a shape quota is configured, resume is counted too so
        its deterministic slots aren't eaten by follow-ups. slots_remaining counts
        the upcoming question itself.

        `pending_format` folds in the last-DECIDED question whose format the
        tracker hasn't registered yet (registration is deferred to the next
        planner call). Without it the deficit is overcounted by one and follow-ups
        are over-suppressed.
        """
        conceptual_count = self._format_count.get(QuestionFormat.CONCEPTUAL, 0)
        scenario_count = sum(self._format_count.get(f, 0) for f in _SCENARIO_FAMILY)
        resume_count = self._format_count.get(QuestionFormat.RESUME_PROJECT, 0)
        if pending_format == QuestionFormat.CONCEPTUAL:
            conceptual_count += 1
        elif pending_format in _SCENARIO_FAMILY:
            scenario_count += 1
        elif pending_format == QuestionFormat.RESUME_PROJECT:
            resume_count += 1

        deficit = 0
        if self.target_conceptual is not None:
            deficit += max(0, self.target_conceptual - conceptual_count)
        if self.target_scenario is not None:
            deficit += max(0, self.target_scenario - scenario_count)
        deficit += max(0, self.max_resume_questions - resume_count)

        remaining = max(1, self._total_q_with_greeting - upcoming_qnum + 1)
        return deficit, remaining

    def quota_needs_slot(self, upcoming_qnum: int, pending_format: 'QuestionFormat' = None) -> bool:
        """True when the remaining quotas need EVERY remaining slot — i.e. a
        non-quota question (an experience follow-up, or free variety) can no
        longer be afforded without missing a target.

        Drives (a) follow-up suppression and (b) starting quota enforcement from
        the first technical question. Returns False whenever no shape quota is
        configured, leaving default interviews' follow-up behavior untouched.
        """
        if not self._has_shape_quota():
            return False
        deficit, remaining = self.remaining_quota_deficit(upcoming_qnum, pending_format)
        return deficit >= remaining

    def adherence_report(self, pending_format: 'QuestionFormat' = None) -> str:
        """One-line 'planned vs delivered' summary per quota — observability so
        drift between the configured mix and what was actually asked is visible
        instead of silent. Returns '' when no shape quota was configured.

        `pending_format` is the FINAL question's format, which the tracker would
        register on the (never-arriving) next call — pass it so the last question
        is counted instead of silently dropped from the totals.
        """
        if not self._has_shape_quota():
            return ""
        conceptual_count = self._format_count.get(QuestionFormat.CONCEPTUAL, 0)
        scenario_count = sum(self._format_count.get(f, 0) for f in _SCENARIO_FAMILY)
        resume_count = self._format_count.get(QuestionFormat.RESUME_PROJECT, 0)
        # Fold in the last question's pending (unregistered) format.
        if pending_format == QuestionFormat.CONCEPTUAL:
            conceptual_count += 1
        elif pending_format in _SCENARIO_FAMILY:
            scenario_count += 1
        elif pending_format == QuestionFormat.RESUME_PROJECT:
            resume_count += 1
        parts = []
        if self.target_conceptual is not None:
            parts.append(f"conceptual {conceptual_count}/{self.target_conceptual}")
        if self.target_scenario is not None:
            parts.append(f"scenario {scenario_count}/{self.target_scenario}")
        parts.append(f"resume {resume_count}/{self.max_resume_questions}")
        return "📊 Quota adherence (delivered/planned): " + ", ".join(parts)
    
    def get_format_prompt(self, 
                         format_type: QuestionFormat,
                         skills: List[str],
                         has_resume: bool = True,
                         resume_text: str = "",
                         role: str = "mid",
                         parsed_resume_projects: str = None) -> str:
        """Generate format-specific instructions for the LLM - AUDIO INTERVIEW COMPATIBLE."""
        skill_str = ', '.join(skills) if skills else 'relevant skills'
        first_skill = skills[0] if skills else 'the skill'
        
        # For resume project format, include resume context
        resume_context = ""
        if format_type == QuestionFormat.RESUME_PROJECT and resume_text:
            # Use GPT-parsed project data if available (parsed async during intro)
            if parsed_resume_projects:
                resume_context = f"""
📋 CANDIDATE'S PARSED RESUME (structured by AI):
{parsed_resume_projects}

⚠️ You MUST reference one of the above projects BY ITS EXACT NAME in your question.
⚠️ Use the project name naturally — "So I saw your [Project Name]..." or "Tell me about your [Project Name]..."
"""
            else:
                # Fallback: raw resume (GPT parsing may not have completed)
                resume_snippet = resume_text[:2000] if len(resume_text) > 2000 else resume_text
                resume_context = f"""
📋 CANDIDATE'S RESUME:
{resume_snippet}

⚠️ Find project names in this resume and reference them BY NAME in your question.
"""
        
        # ─── 3-LEVEL EXAMPLE BANKS ───
        
        # JUNIOR: Foundational, Syntax, Local Environment, Single Function
        junior_ex = {
            QuestionFormat.SCENARIO_BASED: (
                "- You need to implement a simple REST endpoint using Spring Boot. Walk me through it.\\n"
                "- Let's say you need to debug a NullPointerException in a critical service. What do you do?\\n"
                "- Suppose you need to optimize a loop that processes a list of users. How do you approach it?"
            ),
            QuestionFormat.EXPERIMENTAL: (
                "- What would you do if you pushed code that broke the build?\\n"
                "- Suppose your application throws an OutOfMemoryError locally. How do you debug?\\n"
                "- Imagine a user reports a 500 error on a specific page. What's your first step?"
            ),
            QuestionFormat.TROUBLESHOOTING: (
                "- Your application is throwing a NullPointerException on startup. How do you debug?\\n"
                "- You notice a simple API endpoint is returning 400 Bad Request intermittently. What do you check?\\n"
                "- Users report they can't login, but your local setup works. How do you investigate?"
            ),
            QuestionFormat.COMPARISON: (
                "- When would you choose an ArrayList over a LinkedList? What are the trade-offs?\\n"
                "- Compare String and StringBuilder in Java. When would you pick each?\\n"
                "- What factors would guide your choice between SQL and a simple CSV file storage?"
            ),
            QuestionFormat.IMPLEMENTATION_WALKTHROUGH: (
                "- Walk me through how you would implement a user registration feature.\\n"
                "- How would you go about creating a basic API endpoint that returns JSON?\\n"
                "- Describe your approach to writing a unit test for a simple calculator class."
            )
        }

        # MID: Integration, Best Practices, Optimization, Standard Patterns
        mid_ex = {
            QuestionFormat.SCENARIO_BASED: (
                "- You need to add a feature to a legacy codebase with no tests. How do you approach it?\\n"
                "- Let's say your service is seeing slow response times under load. How do you optimize?\\n"
                "- Suppose you need to design a database schema for an e-commerce order system. Walk me through it."
            ),
            QuestionFormat.EXPERIMENTAL: (
                "- What would you do if a production deployment failed and caused downtime?\\n"
                "- Suppose a critical third-party API goes down. How should your application handle it?\\n"
                "- Imagine you found a security vulnerability in a library used by multiple services. What's your plan?"
            ),
            QuestionFormat.TROUBLESHOOTING: (
                "- Your service is running out of database connections. How do you investigate and fix?\\n"
                "- You notice a memory leak in production after a new release. What steps do you take?\\n"
                "- API latency spikes every hour. How do you reproduce and debug this?"
            ),
            QuestionFormat.COMPARISON: (
                "- When would you choose SQL (Postgres) over NoSQL (Mongo)? What are the trade-offs?\\n"
                "- Compare Monolithic architecture vs Microservices. When is Monolith better?\\n"
                "- What factors would guide your choice between REST and GraphQL?"
            ),
            QuestionFormat.IMPLEMENTATION_WALKTHROUGH: (
                "- Walk me through implementing a rate limiter for an API.\\n"
                "- How would you go about adding caching to a read-heavy endpoint?\\n"
                "- Describe your approach to handling asynchronous background jobs."
            )
        }

        # SENIOR: System Design, Scale, Failure Modes, Architecture, Trade-offs
        senior_ex = {
            QuestionFormat.SCENARIO_BASED: (
                "- You need to build a real-time notification system using Kafka and Redis. Walk me through your approach.\\n"
                "- Let's say your team is migrating a monolithic app to microservices with Docker and Kubernetes. How would you plan this?\\n"
                "- Suppose you're tasked with optimizing a slow database query affecting production. What's your approach?"
            ),
            QuestionFormat.EXPERIMENTAL: (
                "- What would you do if your production Kubernetes cluster suddenly lost 50% of its nodes during peak traffic?\\n"
                "- How would you handle a situation where your caching layer becomes the bottleneck instead of helping?\\n"
                "- Suppose you discovered a critical security vulnerability in a core dependency. What's your response?"
            ),
            QuestionFormat.TROUBLESHOOTING: (
                "- Your Docker containers are consuming excessive memory and crashing randomly. How would you troubleshoot this?\\n"
                "- You notice a Kafka consumer lagging by millions of messages. What's your approach?\\n"
                "- Production system is experiencing a split-brain scenario. How do you recover?"
            ),
            QuestionFormat.COMPARISON: (
                "- When would you choose Strong Consistency over Eventual Consistency? Trade-offs?\\n"
                "- Compare Event Sourcing vs CRUD for a financial ledger. When would you pick each?\\n"
                "- What factors would guide your choice between Kubernetes and Serverless (Lambda) for high scale?"
            ),
            QuestionFormat.IMPLEMENTATION_WALKTHROUGH: (
                "- Walk me through setting up a CI/CD pipeline for a multi-region deployment.\\n"
                "- How would you go about implementing a distributed lock system?\\n"
                "- Describe your approach to sharding a massive database without downtime."
            )
        }
        
        # Select Bank
        role_key = role.lower() if role else "mid"
        if role_key == "junior":
            current_bank = junior_ex
            note_text = "NOTE: For JUNIOR role, keep questions simple, concrete, and focused on basics/syntax."
        elif role_key == "senior":
            current_bank = senior_ex
            note_text = "NOTE: For SENIOR role, focus on depth, trade-offs, ambiguity, and strategic decision-making."
        else:
            # Default to Mid
            current_bank = mid_ex
            note_text = "NOTE: For MID role, focus on integration, best practices, optimization, and standard patterns."

        # Helper to safely get examples
        def get_ex(fmt):
            return current_bank.get(fmt, f"- Test knowledge of {skill_str}")

        formats = {
            QuestionFormat.DIRECT_EXPERIENCE: f"""
FORMAT: Direct Experience Question (Use sparingly - max 2 total!)

Ask about their hands-on experience with {first_skill} naturally — like a colleague who's genuinely curious.
DON'T use robotic openers like "Can you describe your experience with {first_skill}?"

INSTEAD, try something like:
- "So how long have you been working with {first_skill}, and what's been your biggest challenge with it so far?"
- "What's the most complex thing you've built using {first_skill}?"
- "If I looked at your last three months of work, how much of it involved {first_skill}?"

Keep it open-ended. Let them talk. This is a warmup-style question.
""",
            
            QuestionFormat.SCENARIO_BASED: f"""
FORMAT: Scenario-Based Question (AUDIO FRIENDLY)

{note_text}

Paint a VIVID, SPECIFIC scenario — not a vague "imagine you need to do X" template.
Good scenarios have: a team, a deadline, a constraint, and a real problem to solve.

GOOD EXAMPLES ({role.upper()}):
{get_ex(QuestionFormat.SCENARIO_BASED)}

HOW TO THINK ABOUT THIS:
- Picture a real Monday morning standup where someone raises a problem
- Describe that problem concretely — with numbers, team size, or system state
- Then ask: "What do you do?" or "How do you handle this?"
- The scenario should REQUIRE knowledge of {skill_str} to answer properly
- DON'T list all the skills in the question — weave them into the scenario naturally

BAD: "Imagine you need to implement a system using {skill_str}. How would you approach this?"
GOOD: "Your team just discovered the search results are returning stale data after every deploy. The index hasn't been rebuilding properly. What's your game plan?"
""",
            
            QuestionFormat.EXPERIMENTAL: f"""
FORMAT: ⚡ What-If / Crisis Question ⚡ (AUDIO FRIENDLY)

Drop the candidate into an UNEXPECTED situation and see how they think on their feet.
This isn't a planning question — it's a "the house is on fire" question.

GOOD EXAMPLES ({role.upper()}):
{get_ex(QuestionFormat.EXPERIMENTAL)}

HOW TO THINK ABOUT THIS:
- Start mid-crisis: "It's 2 AM, your pager goes off..."
- Or drop a curveball: "Your team lead just told you the deadline moved up by 3 weeks..."
- Make it specific enough that generic answers won't cut it
- The answer should reveal how they handle pressure, not just what they know
- MUST involve {skill_str} but don't list the skills — embed them in the crisis

BAD: "What would you do if there was a problem with {skill_str}?"
GOOD: "Your model just started hallucinating in production — confidence scores look fine but the outputs are garbage. Your PM is in a meeting with the client in 30 minutes. Go."
""",
            
            QuestionFormat.RESUME_PROJECT: f"""
FORMAT: 🎯 Project Deep-Dive — NATURAL CONVERSATIONAL STYLE 🎯 (AUDIO FRIENDLY)

{resume_context}

You are an interviewer who has ACTUALLY READ this person's resume. You noticed their
projects and you're genuinely curious about them. Ask like a real human — casual,
direct, and specific.

🚨 CRITICAL RULES:
1. You MUST mention the PROJECT BY ITS ACTUAL NAME from the resume
2. You MUST sound like a real interviewer — conversational, not robotic
3. You MUST ask about SPECIFIC tech choices, challenges, or architecture decisions
4. DO NOT use generic phrasing like "In your role as..." or "You mentioned working on..."
5. If asking about {skill_str}, tie it to a NAMED project from the resume

🚫 FORBIDDEN PHRASES (never use these):
- "In your role as an AI Researcher, you mentioned..."
- "Can you walk me through a specific decision..."
- "In your recent work on scalable RAG pipelines..."
- "You mentioned working on..." (too vague — NAME the project!)
- Any generic opener that doesn't include a project name

✅ QUESTION STYLES (pick one):

📌 STYLE A — "Tell me about this project" (use for first 1-2 resume questions):
Open-ended, let them explain the project. Great as an opener.
- "So I saw you built this Enterprise Medical Intelligence System — tell me about that. How did you end up doing that project, and what were the main challenges you ran into?"
- "I noticed your Anomaly Transformer project — that sounds interesting. Walk me through how you actually built that."
- "Tell me about your Legal LLM project — how did you approach fine-tuning for the legal domain, and what surprised you along the way?"

📌 STYLE B — "Why did you use X in project Y?" (probe specific tech choices):
- "So in your Legal LLM project, I see you used a Falcon model with QLoRA — why Falcon specifically? Did you try any other base models before settling on that?"
- "In your Anomaly Transformer project, you mentioned using discrepancy-based attention — can you explain why you went with that approach instead of a more standard anomaly detection method?"
- "Your Medical Intelligence System uses multi-step reasoning — tell me about the architecture. How many agents are involved and how do they coordinate?"

📌 STYLE C — "What went wrong / what was hard?" (dig into challenges):
- "So your Legal LLM project got hallucination rate down by 23% — what was the hardest part of that? What did you try that DIDN'T work before you found what did?"
- "In your Anomaly Transformer project, training on time-series data can be tricky — what was the most frustrating bug or issue you hit during training?"
- "With your Medical Intelligence System, real-time diagnostic interaction is a tough latency requirement — how did you actually get the inference fast enough?"

📌 STYLE D — "Architecture deep-dive" (for follow-up questions):
- "Give me a brief architecture overview of your Legal LLM project — from the user query to the final response, what are the key components?"
- "In your Anomaly Transformer, you said you trained a model — tell me about the parameters, the transformer architecture you used, and how you handled the training data."
- "How does your Medical Intelligence System handle cases where the reasoning chain leads to a wrong conclusion? Is there a fallback?"

🚨 ANTI-HALLUCINATION GUARDRAIL 🚨
If the resume does NOT mention a project relevant to {skill_str}:
FALL BACK to asking about their work experience at a specific company instead.
Still use the company name: "So at OpenAI, you worked on transformer optimization — tell me about that."

BAD: "In your role as an AI Researcher, you mentioned working on scalable RAG pipelines. Can you walk me through a specific decision you made?" ← GENERIC, ROBOTIC, NO PROJECT NAME
GOOD: "So I saw your Anomaly Transformer project — what challenges did you face, and you mentioned training a model, so tell me about the parameters and the architecture you used." ← SPECIFIC, NATURAL, NAMES THE PROJECT
""",
            
            QuestionFormat.VERBAL_ARCHITECTURE: f"""
FORMAT: Architecture / System Design Discussion (AUDIO FRIENDLY — NO DRAWING)

Give them a concrete system to design with real constraints. This is a CONVERSATION
about architecture, not a whiteboard exercise.

HOW TO THINK ABOUT THIS:
- Name a real system (notification system, payment gateway, search engine)
- Add constraints: "must handle X requests/sec", "must be GDPR compliant", "budget of $500/month"
- Ask them to talk through their approach — what they'd pick and why
- Probe on specific decisions: "Why that database? What's your caching strategy?"

GOOD EXAMPLES:
- "Let's say you need to build a real-time fraud detection system that processes 10K transactions per second. Talk me through your architecture — what are the key components and where are the bottlenecks?"
- "Design a document search system that needs sub-100ms latency for a corpus of 10 million documents. What's your stack?"
- "How would you architect a multi-tenant LLM service where each customer has their own fine-tuned model?"

Keep the discussion focused on {skill_str} — that's where you should probe deepest.
""",
            
            QuestionFormat.TROUBLESHOOTING: f"""
FORMAT: Debugging / Troubleshooting Question (AUDIO FRIENDLY)

Give them a BROKEN system and make them fix it. Be specific about the symptoms.

GOOD EXAMPLES ({role.upper()}):
{get_ex(QuestionFormat.TROUBLESHOOTING)}

HOW TO THINK ABOUT THIS:
- Describe symptoms, not the root cause (they need to figure that out)
- Include misleading clues — real debugging has red herrings
- Ask about their PROCESS: "What's the first thing you check? Second?"
- The system should involve {first_skill} but frame it as "your system is doing X" not "your {first_skill} is broken"

BAD: "How would you troubleshoot issues with {first_skill}?"
GOOD: "Your API response times just jumped from 200ms to 3 seconds, but only for about 20% of requests. CPU and memory look normal. What's your first move?"
""",
            
            QuestionFormat.COMPARISON: f"""
FORMAT: Comparison / Trade-off Discussion (AUDIO FRIENDLY)

Make them CHOOSE between two legitimate options and defend their choice.
Both options must be reasonable — no softballs.

GOOD EXAMPLES ({role.upper()}):
{get_ex(QuestionFormat.COMPARISON)}

HOW TO THINK ABOUT THIS:
- Name TWO specific, real technologies or approaches
- Give enough context that both choices are defensible
- Ask: "Which one and why?" — force them to commit
- Follow up with: "What do you give up with that choice?"
- The comparison should involve {first_skill} naturally

BAD: "Compare {first_skill} and [alternative]. What are the trade-offs?"
GOOD: "You've got a team of 3 and 6 weeks to ship. Are you building the search layer with Elasticsearch or rolling your own with FAISS? What breaks if you pick wrong?"
""",
            
            QuestionFormat.IMPLEMENTATION_WALKTHROUGH: f"""
FORMAT: "How Would You Build It" Walkthrough (AUDIO FRIENDLY — NO CODE)

Ask them to talk through HOW they'd implement something, step by step.
This tests whether they actually know the tools or just know the buzzwords.

GOOD EXAMPLES ({role.upper()}):
{get_ex(QuestionFormat.IMPLEMENTATION_WALKTHROUGH)}

HOW TO THINK ABOUT THIS:
- Pick a SPECIFIC feature, not a whole system
- Ask "walk me through it" — they should explain their approach conversationally
- Probe on details: "What library would you use? What's the data format? How do you test it?"
- This is about practical implementation knowledge of {skill_str}
- If they stay high-level, push for specifics: "Okay, but how exactly would you do the [X] part?"

BAD: "Walk me through how you would implement [feature] using {skill_str}."
GOOD: "You need to add semantic search to an existing app that's currently using keyword search. How do you actually do it — what's step one, what libraries are you pulling in, and how do you handle the transition?"
"""
        }
        
        # ─── CONCEPTUAL FORMAT PROMPT (pure theory/definition questions) ───
        conceptual_prompts = {
            "junior": f"""\
FORMAT: 💡 DIRECT CONCEPT QUESTION (AUDIO FRIENDLY)

⚠️ THIS IS A PURE THEORY / KNOWLEDGE QUESTION — NOT A SCENARIO!

Ask a DIRECT question about {first_skill} — testing definitions, mechanisms, 
differences, or "when to use what". NO scenario wrapping.

🚫 ABSOLUTELY FORBIDDEN:
- DO NOT start with "Imagine you're..."
- DO NOT start with "You're tasked with..."
- DO NOT start with "Your team is..."
- DO NOT create any situational context
- DO NOT ask "how would you build X"
- DO NOT frame it as a project or assignment

✅ WHAT THIS QUESTION MUST LOOK LIKE:
Just ask directly — like one engineer asking another over coffee.
"Hey, can you explain X?" / "What's the difference between X and Y?"

GOOD EXAMPLES:
- "Can you explain what a transformer architecture is and how it works?"
- "What's the difference between supervised and unsupervised learning?"
- "When would you choose to use an RNN over a transformer, and why?"
- "What does the attention mechanism actually do in a transformer model?"
- "Explain to me how backpropagation works — step by step."
- "What are embeddings and why are they useful in NLP?"

BAD: "Imagine you're building a model and you need to choose between X and Y..." ← SCENARIO, NOT CONCEPT!
BAD: "You're tasked with implementing {first_skill}..." ← SCENARIO, NOT CONCEPT!
BAD: "Your team needs to decide between..." ← SCENARIO!

GOOD: "Can you explain how {first_skill} works and why it's useful?"
GOOD: "What's the key difference between {first_skill} and [related concept]?"
""",
            "mid": f"""\
FORMAT: 💡 DIRECT CONCEPT QUESTION (AUDIO FRIENDLY)

⚠️ THIS IS A PURE THEORY / KNOWLEDGE QUESTION — NOT A SCENARIO!

Ask a DIRECT question about {first_skill} — testing deeper understanding 
of concepts, differences, trade-offs, or internal mechanics. NO scenario.

🚫 ABSOLUTELY FORBIDDEN:
- DO NOT start with "Imagine you're..."
- DO NOT start with "You're tasked with..."  
- DO NOT start with "Your team is..."
- DO NOT create any situational context or project setup
- DO NOT frame it as a task, assignment, or problem to solve

✅ WHAT THIS QUESTION MUST LOOK LIKE:
Ask DIRECTLY about the concept itself. Think of it as a technical discussion,
not a work assignment. "Explain X", "What's the difference between X and Y?",
"When would you use X vs Y?"

GOOD EXAMPLES:
- "Explain how the self-attention mechanism works in transformers — what exactly is happening with queries, keys, and values?"
- "What's the difference between LoRA and QLoRA, and when would you pick one over the other?"
- "Can you walk me through what happens during the fine-tuning process of a large language model?"
- "What are the key differences between FAISS and Pinecone as vector databases?"
- "Why do we use cosine similarity for comparing embeddings instead of Euclidean distance?"
- "What's retrieval-augmented generation, and why has it become so popular?"
- "Explain the difference between encoder-only, decoder-only, and encoder-decoder transformer architectures."

BAD: "Imagine you're developing a system that needs to choose between LoRA and QLoRA..." ← SCENARIO!
BAD: "You're working on a project where you need to implement {first_skill}..." ← SCENARIO!

GOOD: "What's the core principle behind {first_skill}, and what are its limitations?"
GOOD: "Can you explain how {first_skill} works under the hood?"
GOOD: "When would you use {first_skill} and when would you avoid it?"
""",
            "senior": f"""\
FORMAT: 💡 DIRECT CONCEPT QUESTION (AUDIO FRIENDLY — SENIOR DEPTH)

⚠️ THIS IS A PURE THEORY / KNOWLEDGE QUESTION — NOT A SCENARIO!

Ask a DIRECT, DEEP concept question about {first_skill}. Test edge-case knowledge,
design philosophy, and fundamental understanding. NO scenario framing whatsoever.

🚫 ABSOLUTELY FORBIDDEN:
- DO NOT start with "Imagine you're..."
- DO NOT start with "You're tasked with..."
- DO NOT start with "Your team is..."
- DO NOT set up any situational context, project, or hypothetical workplace setting
- DO NOT ask them to build, design, or implement anything

✅ WHAT THIS QUESTION MUST LOOK LIKE:
A senior-level theory question asked directly. Think textbook oral exam + engineering depth.
"Explain X at a deep level", "Why does X work this way?", "What breaks when Y happens?"

GOOD EXAMPLES:
- "Explain the transformer architecture in depth — specifically, why does multi-head attention work better than single-head, and what's the mathematical intuition behind it?"
- "What's the fundamental difference between LoRA, QLoRA, and full fine-tuning? When would each approach fail?"
- "Why do large language models hallucinate, and what are the current approaches to mitigate it?"
- "Explain the CAP theorem and tell me which trade-off you actually encounter most in production systems."
- "What are the limitations of FAISS for similarity search at scale, and when would you switch to something else?"
- "Why did the industry move from RNNs to transformers? What specific problem did attention solve that recurrence couldn't?"
- "Explain how LoRA reduces the number of trainable parameters — what's actually happening mathematically?"

BAD: "You're developing a system that uses {first_skill}..." ← SCENARIO!
BAD: "Your team is building a production pipeline with..." ← SCENARIO!

GOOD: "Explain how {first_skill} works at a deep level — what are the fundamental principles, and where does it break down?"
GOOD: "What's the most common misconception about {first_skill}, and what do people usually get wrong?"
"""
        }
        
        formats[QuestionFormat.CONCEPTUAL] = conceptual_prompts.get(role_key, conceptual_prompts["mid"])
        
        return formats.get(format_type, f"Test knowledge of {skill_str}")
    
    def get_status(self) -> Dict:
        """Get current format distribution status."""
        return {
            'format_counts': {fmt.value: count for fmt, count in self._format_count.items()},
            'question_formats': {q: fmt.value for q, fmt in self._question_formats.items()},
            'quotas_met': {
                'direct_experience': self._format_count[QuestionFormat.DIRECT_EXPERIENCE] <= self.max_direct_experience,
                'experimental': self.target_experimental[0] <= self._format_count[QuestionFormat.EXPERIMENTAL] <= self.target_experimental[1],
                'resume_project': self._format_count[QuestionFormat.RESUME_PROJECT] <= self.max_resume_questions,
            }
        }
    
    def to_dict(self) -> Dict:
        """Serialize to dict."""
        return {
            'format_count': {fmt.value: count for fmt, count in self._format_count.items()},
            'question_formats': {q: fmt.value for q, fmt in self._question_formats.items()},
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'QuestionFormatTracker':
        """Deserialize from dict."""
        tracker = cls()
        tracker._format_count = {
            QuestionFormat(k): v for k, v in data.get('format_count', {}).items()
        }
        tracker._question_formats = {
            int(q): QuestionFormat(fmt) for q, fmt in data.get('question_formats', {}).items()
        }
        return tracker
