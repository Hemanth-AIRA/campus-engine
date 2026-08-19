"""Skill coverage + selection — SkillTracker, generate_skill_groups, decide_skill_count.
Split out of responses_client for modularity. Imports shared pieces from question_format."""
import os
import json
import logging
from typing import List, Dict, Set, Optional, Tuple
from dataclasses import dataclass, field
from openai import OpenAI
from config_loader import NUMBER_OF_QUESTIONS
from core.question_format import DIFFICULTY_STRATEGIES, _det_frac

logger = logging.getLogger("skill_tracker")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


def generate_skill_groups(skills: List[str], api_key: str = None) -> Dict[str, List[str]]:
    """
    Uses a fast LLM call to group related skills into logical clusters.
    Called ONCE at interview start. Groups enable multi-skill questions
    that test related skills together (e.g., DB skills in one question).
    
    Returns: {"Databases": ["MySQL", "PostgreSQL", "MongoDB"], ...}
    Fallback: Naive chunking if LLM fails.
    """
    if not skills or len(skills) <= 3:
        return {"General": skills[:]}
    
    try:
        client = OpenAI(api_key=api_key or OPENAI_API_KEY)
        
        prompt = f"""Group these technical skills into logical clusters for an interview.
Each group should contain skills that are RELATED and can be tested together in a single question.

Skills: {', '.join(skills)}

Rules:
- Group size: 2-5 skills per group
- Every skill must appear in exactly ONE group
- Group names should be short (1-3 words)
- Skills that are naturally compared/contrasted should be in the same group
  (e.g., MySQL + PostgreSQL + MongoDB = "Databases")
- Frameworks for the same purpose go together
  (e.g., React + Angular + Vue = "Frontend Frameworks")
- If a skill doesn't fit any group, put it in a small group with the closest match

Respond with ONLY valid JSON, no markdown:
{{"group_name": ["skill1", "skill2"], ...}}"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.1,
        )
        
        raw = response.choices[0].message.content.strip()
        # Clean markdown wrapping if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        
        groups = json.loads(raw)
        
        # Validate: every skill must be in exactly one group
        all_grouped = set()
        for grp_skills in groups.values():
            all_grouped.update(s.strip() for s in grp_skills)
        
        missing = set(skills) - all_grouped
        if missing:
            # Add missing skills to an "Other" group
            groups["Other"] = list(missing)
            logger.warning(f"Skill grouping: Added {len(missing)} missing skills to 'Other' group: {missing}")
        
        logger.info(f"✅ Skill groups generated: {json.dumps(groups, indent=2)}")
        return groups
        
    except Exception as e:
        logger.warning(f"⚠️ Skill grouping LLM call failed: {e}. Using naive fallback.")
        # Fallback: chunk skills into groups of 3 (stable order — no random.shuffle).
        shuffled = skills[:]
        groups = {}
        for i in range(0, len(shuffled), 3):
            chunk = shuffled[i:i+3]
            groups[f"Group {i//3 + 1}"] = chunk
        return groups


@dataclass
class SkillTracker:
    """
    Generic skill coverage tracker.
    Works with ANY list of mandatory skills.
    Priority: Cover ALL skills first, then revisit if needed.
    
    FIXED: Ensures NO skill is repeated until ALL skills are covered first!
    
    Tracks how many times a skill has been skipped (candidate doesn't know).
    If a skill is skipped 2+ times, we stop asking about it (unless we have very few skills).
    
    NEW: Tracks all questions asked to prevent repetition within the interview.
    """
    mandatory_skills: List[str]
    optional_skills: List[str] = field(default_factory=list)
    skill_ratio: Dict[str, int] = field(default_factory=lambda: {"mandatory": 4, "optional": 1})
    
    _total_questions: int = NUMBER_OF_QUESTIONS  # Set from config at runtime
    
    _covered: Set[str] = field(default_factory=set)
    _skipped: Set[str] = field(default_factory=set)
    _skip_count: Dict[str, int] = field(default_factory=dict)
    _blacklisted: Set[str] = field(default_factory=set)
    
    # State flags for ratio tracking
    _mandatory_questions_asked: int = 0
    _optional_questions_asked: int = 0
    _current_skills: List[str] = field(default_factory=list)
    _question_history: Dict[int, List[str]] = field(default_factory=dict)
    _shuffled_skills: List[str] = field(default_factory=list)
    
    # Track actual questions asked to prevent repetition
    _asked_questions: Dict[int, str] = field(default_factory=dict)
    _asked_answers: Dict[int, str] = field(default_factory=dict)  # Q&A full history
    
    # Track difficulty cycle (1 = initial, 2+ = refresh)
    _cycle_count: int = 1
    
    # NEW: Skill grouping for multi-skill questions
    _skill_groups: Dict[str, List[str]] = field(default_factory=dict)
    _skill_to_group: Dict[str, str] = field(default_factory=dict)
    
    def __post_init__(self):
        self._covered = set()
        self._skipped = set()
        self._skip_count = {}
        self._blacklisted = set()
        self._current_skills = []
        self._question_history = {}
        self._asked_questions = {}
        self._asked_answers = {}
        self._cycle_count = 1
        # CAMPUS DETERMINISM: stable JD order (no random.shuffle) so skill sequencing is
        # a pure function of coverage state — reproducible across runs of the same config.
        self._shuffled_skills = self.mandatory_skills.copy()
        if self.optional_skills:
            self._shuffled_skills.extend(self.optional_skills)
        
        # Generate skill groups at init (one-time LLM call)
        all_skills = self.mandatory_skills.copy()
        if self.optional_skills:
            all_skills.extend(self.optional_skills)
            
        self._skill_groups = generate_skill_groups(all_skills)
        self._skill_to_group = {}
        for group_name, group_skills in self._skill_groups.items():
            for skill in group_skills:
                self._skill_to_group[skill] = group_name
        logger.info(f"Skill groups initialized: {list(self._skill_groups.keys())}")
    
    def get_next_skills(self, count: int = 1, allow_repeat: bool = False, skill_states: dict = None) -> List[str]:
        """
        Get next skill(s) to test.
        Logic enforces the mandatory:optional skill ratio.
        """
        
        # 1. Ratio determination
        pick_mandatory = True
        
        # Get ratios safely with defaults 4:1
        mand_ratio = self.skill_ratio.get("mandatory", 4)
        opt_ratio = self.skill_ratio.get("optional", 1)
        
        if self.optional_skills and opt_ratio > 0:
            if self._mandatory_questions_asked >= mand_ratio:
                if self._optional_questions_asked < opt_ratio:
                    pick_mandatory = False
                else:
                    # Reset cycle
                    self._mandatory_questions_asked = 0
                    self._optional_questions_asked = 0
                    pick_mandatory = True

        target_pool = self.mandatory_skills if pick_mandatory else self.optional_skills
        
        # 2. Total Failure Check: Are ALL mandatory skills blacklisted?
        num_blacklisted_mand = len([s for s in self.mandatory_skills if s in self._blacklisted])
        if self.mandatory_skills and num_blacklisted_mand == len(self.mandatory_skills):
            logger.warning("⛔ ALL mandatory skills blacklisted! Triggering end of interview.")
            return []

        # 3. Refresh Cycle Check: Are we out of uncovered skills in the target pool?
        uncovered = [s for s in target_pool if s not in self._covered and s not in self._blacklisted]
        
        # Phase 3C: Filter out dynamically removed skills
        if skill_states:
            uncovered = [s for s in uncovered if skill_states.get(s) and getattr(skill_states[s], "status", "active") != "removed"]
            
        if not uncovered:
            # If we wanted optional but exhausted it, fallback to mandatory
            if not pick_mandatory:
                pick_mandatory = True
                self._mandatory_questions_asked = 0
                self._optional_questions_asked = 0
                target_pool = self.mandatory_skills
                uncovered = [s for s in target_pool if s not in self._covered and s not in self._blacklisted]
                if skill_states:
                    uncovered = [s for s in uncovered if skill_states.get(s) and getattr(skill_states[s], "status", "active") != "removed"]

        if not uncovered:
            self._cycle_count += 1
            pool_type = "mandatory" if pick_mandatory else "optional"
            logger.info(f"🔄 Starting Skill Refresh Cycle {self._cycle_count} for {pool_type} skills")
            
            # Reset covered/skipped for this pool
            for s in target_pool:
                self._covered.discard(s)
                self._skipped.discard(s)
                
            uncovered = [s for s in target_pool if s not in self._blacklisted]
            # CAMPUS DETERMINISM: keep skills in stable JD order (no random.shuffle) so the
            # skill sequence is a pure function of coverage state — same config → same order.
            self._shuffled_skills = uncovered.copy()
            
        # 3. Build pool (prioritize non-skipped)
        non_skipped = [s for s in uncovered if s not in self._skipped]
        pool = non_skipped if non_skipped else uncovered
        
        logger.info(f"Skill Selection: Uncovered={len(uncovered)}, Pool={len(pool)}, Count={count}")
        
        # 4. GROUP-BASED SELECTION for multi-skill questions
        if count >= 2 and self._skill_groups:
            # Find groups that have uncovered skills in the pool
            group_candidates = {}  # {group_name: [available_skills_from_pool]}
            for skill in pool:
                grp = self._skill_to_group.get(skill)
                if grp:
                    group_candidates.setdefault(grp, []).append(skill)
            
            # Sort groups by number of available uncovered skills (prefer bigger groups)
            sorted_groups = sorted(group_candidates.items(), key=lambda x: len(x[1]), reverse=True)
            
            # Try to find a group with enough skills for `count`
            for grp_name, grp_skills in sorted_groups:
                if len(grp_skills) >= count:
                    # CAMPUS DETERMINISM: first `count` in stable order (was random.sample).
                    selected = grp_skills[:count]
                    logger.info(f"🎯 Group-based selection from '{grp_name}': {selected}")
                    return selected
            
            # No single group has enough — pick from largest group + fill from others
            if sorted_groups:
                best_grp_name, best_grp_skills = sorted_groups[0]
                selected = best_grp_skills[:]  # Take all from best group
                remaining_needed = count - len(selected)
                # Fill remaining from other groups
                other_skills = [s for s in pool if s not in selected]
                if other_skills and remaining_needed > 0:
                    # CAMPUS DETERMINISM: take the first needed in stable order (was random.sample).
                    selected.extend(other_skills[:remaining_needed])
                logger.info(f"🎯 Mixed group selection (primary '{best_grp_name}'): {selected}")
                return selected
        
        # 5. Single skill or no groups: deterministic coverage-order pick (first uncovered
        #    skills in stable JD order — was random.sample, the main run-to-run variance source).
        selected = pool[:min(count, len(pool))]
        logger.info(f"Skill Selection (deterministic): {selected}")
        return selected
    
    def register_question(self, question_number: int, skills: List[str], question_text: str = ""):
        """
        Register which skills a question tests and the question text.
        
        Args:
            question_number: Question number (1-{NUMBER_OF_QUESTIONS})
            skills: List of skills being tested
            question_text: The actual question text asked
        """
        self._current_skills = skills
        self._question_history[question_number] = skills
        
        # NEW: Store the question text for deduplication
        if question_text:
            self._asked_questions[question_number] = question_text.strip().lower()
            
        if skills:
            if self.optional_skills and skills[0] in self.optional_skills:
                self._optional_questions_asked += 1
            else:
                self._mandatory_questions_asked += 1
        
        logger.info(f"Q{question_number} will test: {skills}")
    
    def record_answer(self, question_number: int, answer_text: str):
        """
        Record the candidate's answer to a previously registered question.
        This enables full Q+A history to be injected into subsequent prompts
        to prevent question-type repetition without any retry logic.
        """
        if answer_text and answer_text.strip():
            # Store a 150-char snippet of the answer for prompt conciseness
            self._asked_answers[question_number] = answer_text.strip()[:150]
    
    def mark_covered(self, skills: List[str] = None):
        """Mark skill(s) as covered after candidate answers successfully."""
        skills = skills or self._current_skills
        for skill in skills:
            self._covered.add(skill)
            self._skipped.discard(skill)
            # Reset skip count when covered
            if skill in self._skip_count:
                self._skip_count[skill] = 0
            if skill in self._blacklisted:
                self._blacklisted.remove(skill)
        logger.info(f"Marked covered: {skills}")
    
    def _compute_question_similarity(self, q1: str, q2: str) -> float:
        """
        Compute simple word-based similarity between two questions.
        Returns: 0.0 to 1.0, where 1.0 means identical
        """
        if not q1 or not q2:
            return 0.0
        
        # Normalize
        q1 = q1.lower().strip()
        q2 = q2.lower().strip()
        
        if q1 == q2:
            return 1.0
        
        # Extract significant words (remove common question words)
        stop_words = {'can', 'you', 'the', 'a', 'an', 'how', 'what', 'when', 'where', 'why', 
                     'would', 'could', 'should', 'is', 'are', 'was', 'were', 'do', 'does',
                     'tell', 'me', 'about', 'describe', 'explain', 'walk', 'through', 'your'}
        
        words1 = set(w for w in q1.split() if w not in stop_words and len(w) > 2)
        words2 = set(w for w in q2.split() if w not in stop_words and len(w) > 2)
        
        if not words1 or not words2:
            return 0.0
        
        # Jaccard similarity
        intersection = len(words1 & words2)
        union = len(words1 | words2)
        
        return intersection / union if union > 0 else 0.0
    
    def is_question_too_similar(self, new_question: str, threshold: float = 0.6) -> Tuple[bool, Optional[int]]:
        """
        Check if a new question is too similar to any previously asked question.
        
        Args:
            new_question: The new question text to check
            threshold: Similarity threshold (0.0 to 1.0). Default 0.6 means 60% word overlap
        
        Returns:
            Tuple of (is_too_similar, similar_question_number)
        """
        if not new_question:
            return False, None
        
        for q_num, asked_q in self._asked_questions.items():
            similarity = self._compute_question_similarity(new_question, asked_q)
            
            if similarity >= threshold:
                logger.warning(
                    f"⚠️ Question too similar to Q{q_num}! "
                    f"Similarity: {similarity:.1%}\n"
                    f"  Previous: {asked_q[:80]}...\n"
                    f"  New: {new_question[:80]}..."
                )
                return True, q_num
        
        return False, None
    
    def get_asked_questions_summary(self) -> str:
        """
        Get a summary of ALL previously asked Q+A pairs for prompt injection.
        CRITICAL: Shows ALL questions + candidate answers so the LLM can see
        exact content and type of question already asked — preventing any repetition
        without needing retry logic.
        """
        if not self._asked_questions:
            return "No questions asked yet."
        
        # Sort by question number (ascending order)
        all_questions = sorted(self._asked_questions.items())
        
        summary_lines = []
        for q_num, q_text in all_questions:
            # Restore original casing (stored as lowercase)
            answer = self._asked_answers.get(q_num, "[no answer recorded]")
            summary_lines.append(f"  Q{q_num}: {q_text}")
            summary_lines.append(f"       ↳ Candidate answered: {answer}")
        
        return "\n".join(summary_lines)
    
    def mark_skipped(self, skills: List[str] = None):
        """
        Mark skill(s) as skipped (candidate didn't know).

        Skips NO LONGER blacklist a skill here. On 'I don't know' the difficulty
        softener (InterviewContext.soften_skill_on_skip, driven from the planner)
        lowers the skill and re-asks it easier, and removes it ONLY once it is at
        the easiest level and still skipped. Here we just record the skip.
        """
        skills = skills or self._current_skills
        for skill in skills:
            if skill not in self._covered:
                self._skipped.add(skill)
                self._skip_count[skill] = self._skip_count.get(skill, 0) + 1
                logger.info(
                    f"Marked skipped: {skill} (skip_count={self._skip_count[skill]}). "
                    "Difficulty will be lowered and the skill re-asked; removal only at the easiest level."
                )
    
    def process_response(self, intent: str):
        """Process candidate's response based on intent."""
        if intent.upper() == "OTHERS":
            self.mark_covered()
        elif intent.upper() == "SKIP":
            self.mark_skipped()
    
    @property
    def uncovered(self) -> List[str]:
        return [s for s in self.mandatory_skills if s not in self._covered]
    
    @property
    def covered(self) -> List[str]:
        return [s for s in self.mandatory_skills if s in self._covered]
    
    @property
    def skipped(self) -> List[str]:
        return [s for s in self.mandatory_skills if s in self._skipped and s not in self._covered]
    
    @property
    def blacklisted(self) -> List[str]:
        """Skills that have been skipped 2+ times and should be avoided."""
        return list(self._blacklisted)
    
    @property
    def all_covered(self) -> bool:
        return len(self._covered) >= len(self.mandatory_skills)
    
    def coverage_pct(self) -> float:
        if not self.mandatory_skills:
            return 100.0
        return (len(self._covered) / len(self.mandatory_skills)) * 100
    
    def get_status(self, current_q: int) -> Dict:
        """Get current tracking status."""
        remaining_q = max(0, self._total_questions - current_q)
        uncovered_count = len(self.uncovered)
        
        return {
            'covered': self.covered,
            'uncovered': self.uncovered,
            'skipped': self.skipped,
            'blacklisted': self.blacklisted,
            'skip_counts': {k: v for k, v in self._skip_count.items() if v > 0},
            'coverage_pct': self.coverage_pct(),
            'all_covered': self.all_covered,
            'remaining_questions': remaining_q,
            'skills_per_question_needed': uncovered_count / max(remaining_q, 1) if uncovered_count else 0,
        }
    
    def format_for_prompt(self, current_q: int, next_skills: List[str], 
                          blend_mode: Optional[str] = None) -> str:
        """
        Format tracking info for prompt injection.
        
        Args:
            current_q: Current question number
            next_skills: Skills to test in next question
            blend_mode: "resume", "jd", or None
        """
        status = self.get_status(current_q)
        remaining_q = status['remaining_questions']
        uncovered_count = len(status['uncovered'])
        
        if status['all_covered']:
            urgency = "✅ ALL SKILLS COVERED - Ask depth/advanced questions on previously covered skills"
        elif remaining_q < uncovered_count:
            urgency = f"🚨 CRITICAL: {uncovered_count} skills left, only {remaining_q} questions! COMBINE SKILLS!"
        elif remaining_q == uncovered_count:
            urgency = f"⚠️ TIGHT: Must cover 1 new skill per question!"
        else:
            urgency = f"✓ On track: {uncovered_count} skills, {remaining_q} questions"
        
        # Build blend instruction based on mode
        blend_instruction = ""
        if blend_mode == "resume":
            blend_instruction = "\n🔗 BLEND WITH RESUME: Connect this question to candidate's past experience from their resume"
        elif blend_mode == "jd":
            blend_instruction = "\n🔗 BLEND WITH JD: Connect this question to specific job requirements from the JD"
        
        next_skills_str = ', '.join(next_skills) if next_skills else 'advanced topics'
        
        # Build group context for the LLM
        group_context = ""
        if next_skills and self._skill_to_group:
            groups_used = set()
            for skill in next_skills:
                grp = self._skill_to_group.get(skill)
                if grp:
                    groups_used.add(grp)
            if groups_used:
                group_context = f"\n🔗 SKILL GROUP: {', '.join(groups_used)} — These skills are RELATED. Frame a question that tests how they work TOGETHER or compares/contrasts them."
        
        # Build blacklist warning
        blacklist_warning = ""
        if status['blacklisted']:
            few_skills = len(self.mandatory_skills) <= 2
            if few_skills:
                blacklist_warning = f"\n⚠️ BLACKLISTED (but asking anyway due to few skills): {', '.join(status['blacklisted'])}"
            else:
                blacklist_warning = f"\n🚫 BLACKLISTED (DON'T ASK - candidate doesn't know): {', '.join(status['blacklisted'])}"
        
        skip_count_info = ""
        if status['skip_counts']:
            skip_count_info = "\n📊 SKIP COUNTS: " + ", ".join(
                f"{skill}({count}x)" for skill, count in status['skip_counts'].items()
            )
        
        # Add ALL previously asked questions to prevent ANY repetition
        all_questions = self.get_asked_questions_summary()
        questions_section = f"""

🚨 ALL QUESTIONS ASKED SO FAR - DO NOT REPEAT ANY! 🚨
═══════════════════════════════════════════════════════════════════════════════
{all_questions}

⚠️ Your next question MUST be completely different from ALL of the above!
⚠️ Check: different scenario, different angle, different context, different format!
⚠️ Even if testing the same skill, approach it from a NEW perspective!
═══════════════════════════════════════════════════════════════════════════════
"""
        
        # Build full skill group overview
        groups_overview = ""
        if self._skill_groups:
            groups_overview = "\n📋 SKILL GROUPS (for context):\n"
            for grp_name, grp_skills in self._skill_groups.items():
                grp_status = []
                for s in grp_skills:
                    if s in self._blacklisted:
                        grp_status.append(f"~~{s}~~")
                    elif s in status['covered']:
                        grp_status.append(f"✅{s}")
                    else:
                        grp_status.append(f"❌{s}")
                groups_overview += f"  • {grp_name}: {', '.join(grp_status)}\n"
        
        return f"""
═══════════════════════════════════════════════════════════════════════════════
📊 SKILL COVERAGE (Q{current_q}/{self._total_questions}) - {status['coverage_pct']:.0f}% Complete
═══════════════════════════════════════════════════════════════════════════════

✅ COVERED ({len(status['covered'])}): {', '.join(status['covered']) if status['covered'] else 'None yet'}
❌ NOT COVERED ({len(status['uncovered'])}): {', '.join(status['uncovered']) if status['uncovered'] else 'All done!'}
⚠️ SKIPPED: {', '.join(status['skipped']) if status['skipped'] else 'None'}{blacklist_warning}{skip_count_info}
{groups_overview}
{urgency}

🚨 CRITICAL RULES:
1. NEVER repeat a skill until ALL skills are covered first!
2. If a skill is skipped 2+ times, STOP asking about it (unless we have ≤2 total skills)
3. NEVER ask the same or similar question twice in one interview!
{questions_section}
═══════════════════════════════════════════════════════════════════════════════
🎯 NEXT QUESTION MUST TEST: {next_skills_str}{group_context}
═══════════════════════════════════════════════════════════════════════════════
{blend_instruction}
"""
    
    def to_dict(self) -> Dict:
        return {
            'mandatory_skills': self.mandatory_skills,
            'optional_skills': self.optional_skills,
            'covered': list(self._covered),
            'skipped': list(self._skipped),
            'skip_count': self._skip_count,
            'blacklisted': list(self._blacklisted),
            'question_history': self._question_history,
            'asked_questions': self._asked_questions,  # NEW: Include asked questions
            '_mandatory_questions_asked': self._mandatory_questions_asked,
            '_optional_questions_asked': self._optional_questions_asked,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'SkillTracker':
        tracker = cls(
            mandatory_skills=data['mandatory_skills'],
            optional_skills=data.get('optional_skills', [])
        )
        tracker._covered = set(data.get('covered', []))
        tracker._skipped = set(data.get('skipped', []))
        tracker._skip_count = data.get('skip_count', {})
        tracker._blacklisted = set(data.get('blacklisted', []))
        tracker._question_history = {int(k): v for k, v in data.get('question_history', {}).items()}
        tracker._asked_questions = {int(k): v for k, v in data.get('asked_questions', {}).items()}  # NEW
        tracker._mandatory_questions_asked = data.get('_mandatory_questions_asked', 0)
        tracker._optional_questions_asked = data.get('_optional_questions_asked', 0)
        return tracker


def decide_skill_count(question_number: int, tracker: SkillTracker, role: str = "mid") -> int:
    """Decide how many skills to test in next question based on role strategy."""
    status = tracker.get_status(question_number)
    remaining_q = status['remaining_questions']
    uncovered = len(status['uncovered'])

    # Get strategy
    role_key = role.lower() if role else "mid"
    strategy = DIFFICULTY_STRATEGIES.get(role_key, DIFFICULTY_STRATEGIES["mid"])
    weights = strategy["skill_count_weights"]

    # In Cycle 2+ (refresh cycles): allow multi-skill for depth
    if uncovered > remaining_q and remaining_q > 0:
        return min(3, uncovered)

    if question_number == tracker._total_questions - 1:
        # Last question is usually complex — deterministic (low-discrepancy) instead of RNG.
        if role_key == "junior":
            return 1 if _det_frac(question_number) < 0.7 else 2
        return 3 if _det_frac(question_number) < 0.5 else 4

    # Deterministic weighted pick over [1,2,3] (was random.choices) keyed by question
    # index via a low-discrepancy fraction — preserves the weighting, reproducibly.
    r = _det_frac(question_number)
    total = float(sum(weights)) or 1.0
    cum = 0.0
    for val, w in zip((1, 2, 3), weights):
        cum += w / total
        if r < cum:
            return val
    return 3
