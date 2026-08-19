"""
HRInterviewConfig — Configuration for behavioral HR interview rounds.

Parsed from the `interview_config` JSON column on the interviews table.
Separate from the technical InterviewConfig to avoid cross-contamination.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger("HRInterviewConfig")

# Default behavioral topics (conversation flow)
DEFAULT_HR_TOPICS = [
    "intro",
    "ownership",
    "conflict",
    "teamwork",
    "motivation",
    "adaptability",
    "reflection",
]

# Default evaluation dimensions (scoring)
DEFAULT_DIMENSIONS = [
    {"dimension_key": "communication_clarity", "dimension_name": "Communication Clarity",
     "description": "Ability to articulate thoughts clearly and adapt communication style."},
    {"dimension_key": "ownership_mindset", "dimension_name": "Ownership Mindset",
     "description": "Takes responsibility for outcomes and proactively addresses problems."},
    {"dimension_key": "conflict_handling", "dimension_name": "Conflict Handling",
     "description": "Manages disagreements constructively and maintains professional relationships."},
    {"dimension_key": "career_motivation", "dimension_name": "Career Motivation",
     "description": "Shows clear career direction and genuine interest in the role."},
    {"dimension_key": "team_collaboration", "dimension_name": "Team Collaboration",
     "description": "Works effectively with others and values diverse perspectives."},
    {"dimension_key": "adaptability", "dimension_name": "Adaptability",
     "description": "Handles change well and remains productive under uncertainty."},
    {"dimension_key": "integrity_signals", "dimension_name": "Integrity Signals",
     "description": "Demonstrates honesty and consistency between words and actions."},
    {"dimension_key": "culture_alignment", "dimension_name": "Culture Alignment",
     "description": "Values and work style align with the organization environment."},
]


@dataclass
class HRInterviewConfig:
    """Single source of truth for HR round parameters."""

    max_duration_minutes: int = 15
    min_duration_minutes: int = 8
    min_responses: int = 6
    topics: List[str] = field(default_factory=lambda: list(DEFAULT_HR_TOPICS))
    scoring_weights: dict = field(default_factory=lambda: {
        "technical_weight": 0.7,
        "hr_weight": 0.3,
    })

    def __post_init__(self):
        if self.max_duration_minutes < 5:
            logger.warning(
                f"Invalid max_duration_minutes={self.max_duration_minutes}, defaulting to 15"
            )
            self.max_duration_minutes = 15
        if self.min_responses < 3:
            logger.warning(
                f"Invalid min_responses={self.min_responses}, defaulting to 6"
            )
            self.min_responses = 6
        if not self.topics:
            logger.warning("Empty topics list, using defaults")
            self.topics = list(DEFAULT_HR_TOPICS)

    @classmethod
    def from_interview_config(cls, config_json: Optional[dict]) -> "HRInterviewConfig":
        """Build from the interview_config JSON column."""
        if not config_json or not isinstance(config_json, dict):
            return cls()

        return cls(
            max_duration_minutes=config_json.get("max_duration", 15),
            min_duration_minutes=config_json.get("min_duration", 8),
            min_responses=config_json.get("min_responses", 6),
            topics=config_json.get("topics", list(DEFAULT_HR_TOPICS)),
            scoring_weights=config_json.get("scoring_weights", {
                "technical_weight": 0.7,
                "hr_weight": 0.3,
            }),
        )

    def log_config(self, prefix: str = ""):
        logger.info(
            f"📋 {prefix}HRInterviewConfig: "
            f"duration={self.min_duration_minutes}-{self.max_duration_minutes}m, "
            f"min_responses={self.min_responses}, "
            f"topics={self.topics}, "
            f"weights={self.scoring_weights}"
        )
