from dataclasses import dataclass, asdict
from typing import List, Dict, Optional


@dataclass
class AnalysisInput:
    session_id: str
    question_id: str
    question_text: str
    question_type: str
    answer_text: str
    answer_duration_sec: float
    resume_text: Optional[str]
    session_context: Dict


@dataclass
class TextFeatures:
    char_count: int
    word_count: int
    sentence_count: int
    avg_sentence_length: float
    lexical_diversity: float
    abstraction_score: float
    personal_markers: int
    numeric_presence: int
    raw_text: str  # FIX: Store raw text for downstream analysis
    
    def to_dict(self) -> Dict:
        """Serialize to dict for storage."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'TextFeatures':
        """Deserialize from dict."""
        return cls(**data)


@dataclass
class ExpectationAlignment:
    specificity_met: float
    personal_context_met: float
    sequencing_met: float
    tradeoff_met: float


@dataclass
class TemporalAnalysis:
    effort_plausibility: float
    latency_complexity_alignment: float


@dataclass
class ContinuityAnalysis:
    style_continuity: float
    complexity_drift: float
    genericness_drift: float
    specificity_consistency: float


@dataclass
class AnalysisOutput:
    session_id: str
    question_id: str
    authorship_confidence: str
    confidence_delta: float
    explanations: List[str]
    signals: Dict[str, float]
    llm_assessment: Optional[Dict] = None  # LLMAssessment.to_dict() or None
    llm_enabled: bool = False  # Whether LLM was used
    llm_adjustment: float = 0.0  # How much LLM contributed to delta



@dataclass
class CandidateSemanticProfile:
    recurring_concepts: List[str]
    preferred_tradeoffs: List[str]
    reasoning_patterns: List[str]
    vocabulary_fingerprint: List[str]

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def empty(cls) -> "CandidateSemanticProfile":
        return cls([], [], [], [])


# -----------------------------
# LLM semantic deviation result
# -----------------------------

@dataclass
class SemanticDeviationResult:
    self_consistency: float          # 0–1
    conceptual_novelty: float        # 0–1
    semantic_alignment: float        # 0–1
    observations: List[str]
    uncertainty_note: str

    def to_dict(self) -> Dict:
        return asdict(self)


# -----------------------------
# Per-answer stored signal
# -----------------------------

@dataclass
class AnswerAssistanceSignal:
    answer_signal: float             # 0–1
    semantic_result: SemanticDeviationResult
    anomaly_flags: List[str]
