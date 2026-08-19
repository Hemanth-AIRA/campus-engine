"""
Target company/role PROFILES for the campus engine.

Students prep for a *target* — "a product-company SDE", "a service-company fresher role",
"a data-analyst role". A profile is a preset bundle of (skills, difficulty, role level,
question style mix) so a whole practice session is aligned to that target in one setting.

Profiles are JSON in profiles/. A faculty/student picks one (CAMPUS_PROFILE=<name> or the
per-interview 'campus.profile'); the loader turns it into config overrides the interview
builder consumes.
"""
import os
import json
import glob
import logging
from typing import Optional, Dict, List

logger = logging.getLogger("campus_profiles")


def _profiles_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profiles")


def list_profiles() -> List[str]:
    return sorted(
        os.path.splitext(os.path.basename(p))[0]
        for p in glob.glob(os.path.join(_profiles_dir(), "*.json"))
    )


def load_profile(name: str) -> Optional[Dict]:
    if not name:
        return None
    path = os.path.join(_profiles_dir(), f"{name.strip().lower()}.json")
    if not os.path.isfile(path):
        logger.warning(f"Campus profile '{name}' not found in {_profiles_dir()} ({list_profiles()})")
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load campus profile '{name}': {e}")
        return None


def profile_to_overrides(profile: Dict) -> Dict:
    """Turn a profile into interview-config overrides (skills + difficulty + style mix).
    Keys mirror what webrtc's parsed_config already understands so wiring is a dict merge."""
    if not profile:
        return {}
    skills = profile.get("skills", [])
    out = {
        "mandatory_skills": ", ".join(skills) if isinstance(skills, list) else skills,
        "base_level": profile.get("base_difficulty", "medium"),
        "role_experience_required": profile.get("role_level", "mid"),
    }
    if "conceptual_ratio" in profile:
        out["conceptual_question_ratio"] = profile["conceptual_ratio"]
    if "resume_ratio" in profile:
        out["resume_question_ratio"] = profile["resume_ratio"]
    return {k: v for k, v in out.items() if v not in (None, "")}
