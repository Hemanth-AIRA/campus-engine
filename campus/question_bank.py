"""
Campus hybrid question sourcing — curated banks + on-target validation.

Phase 3 of the campus redesign. Two goals, both aimed at the "questions drift off-target"
pain:

  1. CURATED BANKS — vetted practice questions per (skill, level, difficulty_tier), stored
     as JSON in banks/. For college prep these give repeatable, quality-controlled questions.
  2. ON-TARGET VALIDATION — a cheap skill-match check applied to BOTH bank hits and the LLM
     fallback, so every asked question is anchored to the skill it is meant to test.

Selection order for each content question (see responses_client integration):
    target (skill, level, difficulty_tier)
      → draw an UNUSED bank entry that matches      → use it
      → else LLM-generate anchored to skill+vector  → validate on-target → regenerate if drift

The difficulty_tier is derived from the DifficultyVector so the bank and the adaptive engine
speak the same language.
"""
import json
import os
import glob
import logging
import re
from typing import Optional, Dict, List

logger = logging.getLogger("question_bank")

# Difficulty tiers — coarse buckets over the DifficultyVector's abstraction depth (1-5).
FOUNDATIONAL, APPLIED, ADVANCED = "foundational", "applied", "advanced"
_TIER_ORDER = [FOUNDATIONAL, APPLIED, ADVANCED]


def vector_to_tier(vector) -> str:
    """Map a DifficultyVector (or plain abstraction int) to a bank difficulty tier."""
    a = getattr(vector, "abstraction_depth", vector if isinstance(vector, int) else 2)
    c = getattr(vector, "constraint_pressure", 0)
    if a <= 2 and c == 0:
        return FOUNDATIONAL
    if a >= 4 or c >= 2:
        return ADVANCED
    return APPLIED


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+#. ]", " ", (s or "").lower()).strip()


def is_on_target(question_text: str, skill: str, aliases: Optional[List[str]] = None,
                 topics: Optional[List[str]] = None) -> bool:
    """Cheap guard: does the question actually reference the target skill, an alias, or one
    of the skill's TOPIC terms? Topics matter because a good question is often about the
    skill without naming it ("use a decorator..." is a Python question). Word-boundary match
    so 'go' doesn't match 'algorithm'. Used for BOTH bank and LLM questions — the core
    defence against off-target drift."""
    if not question_text or not skill:
        return False
    hay = " " + _norm(question_text) + " "
    terms = [skill] + list(aliases or []) + list(topics or [])
    for t in terms:
        t = _norm(t)
        if not t:
            continue
        # token/phrase boundary match
        if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", hay):
            return True
    return False


class QuestionBank:
    """Loads banks/*.json and serves unused, on-target, tier-matched questions."""

    def __init__(self, banks_dir: str = None):
        if banks_dir is None:
            banks_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "banks")
        self.banks_dir = banks_dir
        # skill_lower -> {"aliases": [...], "levels": {level: {tier: [entries]}}}
        self._banks: Dict[str, dict] = {}
        self._load()

    def _load(self):
        if not os.path.isdir(self.banks_dir):
            logger.warning(f"Question bank dir not found: {self.banks_dir} (LLM-only mode)")
            return
        for path in sorted(glob.glob(os.path.join(self.banks_dir, "*.json"))):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                skill = _norm(data["skill"])
                self._banks[skill] = {
                    "canonical": data["skill"],
                    "aliases": data.get("aliases", []),
                    "topics": data.get("topics", []),
                    "levels": data.get("levels", {}),
                }
            except Exception as e:
                logger.warning(f"Skipping malformed bank {path}: {e}")
        total = sum(
            len(entries)
            for b in self._banks.values()
            for lvl in b["levels"].values()
            for entries in lvl.values()
        )
        logger.info(f"📚 Question bank loaded: {len(self._banks)} skills, {total} questions from {self.banks_dir}")

    def has_skill(self, skill: str) -> bool:
        return _norm(skill) in self._banks

    def aliases(self, skill: str) -> List[str]:
        b = self._banks.get(_norm(skill))
        return b["aliases"] if b else []

    def topics(self, skill: str) -> List[str]:
        """Skill-specific TOPIC terms — a bank question is often about the skill without
        naming it ('use a decorator...' is a Python question). is_on_target trusts these."""
        b = self._banks.get(_norm(skill))
        return b["topics"] if b else []

    def get(self, skill: str, level: str, tier: str, used_ids: set = None) -> Optional[dict]:
        """Return an unused bank entry for (skill, level, tier), falling back to adjacent
        tiers (nearest first) and then any level. Returns None if the skill isn't banked or
        everything is used — the caller then uses the LLM fallback."""
        used_ids = used_ids or set()
        b = self._banks.get(_norm(skill))
        if not b:
            return None
        level = level if level in b["levels"] else next(iter(b["levels"]), None)
        if not level:
            return None
        by_tier = b["levels"].get(level, {})

        # Try the requested tier, then the nearest tiers (so difficulty stays close).
        try:
            ti = _TIER_ORDER.index(tier)
        except ValueError:
            ti = 1
        order = sorted(_TIER_ORDER, key=lambda t: abs(_TIER_ORDER.index(t) - ti))
        for t in order:
            for entry in by_tier.get(t, []):
                if entry.get("id") not in used_ids:
                    out = dict(entry)
                    out["_skill"] = b["canonical"]
                    out["_level"] = level
                    out["_tier"] = t
                    return out
        return None
