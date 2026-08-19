"""
Deterministic FAKE CANDIDATE + fixtures for engine tests.

Lets tests drive the interview logic without a human or (for the cheap tests) an LLM:
  • FakeCandidate.answer(question, skill) returns a canned reply by 'mode', so the full
    ack/intent/scoring path can be exercised reproducibly and for free.
  • INTENT_CORPUS is the ground-truth (candidate utterance → expected intent) taken from
    the engine's own few-shot examples — used by the --llm intent test.
  • sample_configs() is a matrix of interview configs for question-generation validation.

Import from tests: `from tests.fake_candidate import FakeCandidate, INTENT_CORPUS, sample_configs`
"""

# ── Fake candidate ───────────────────────────────────────────────────────────
_ANSWERS = {
    "strong": (
        "In my final-year project I used {skill} extensively. I compared two approaches, "
        "measured the trade-offs, and chose the one with better complexity. I can explain "
        "exactly why it worked and what I'd change."
    ),
    "weak": "Um, I think {skill} is something about computers. I'm not really sure.",
    "skip": "I don't know, sorry. Can we skip this one?",
    "rephrase": "Sorry, what do you mean exactly? Could you rephrase the question?",
    "repeat": "Can you repeat that? I couldn't hear you clearly.",
    "offtopic": "Honestly I'd rather talk about my hobbies and the weather today.",
    "silent": "",
}


class FakeCandidate:
    """A scripted candidate. mode ∈ {strong, weak, skip, rephrase, repeat, offtopic, silent}
    or 'mixed' to cycle through strong→weak→strong…"""

    def __init__(self, mode: str = "strong"):
        self.mode = mode
        self._n = 0

    def answer(self, question: str = "", skill: str = "the topic") -> str:
        self._n += 1
        mode = self.mode
        if mode == "mixed":
            mode = "strong" if self._n % 2 else "weak"
        return _ANSWERS.get(mode, _ANSWERS["strong"]).format(skill=skill)

    def confidence(self) -> float:
        """The score a fair evaluator would give this mode (for logic tests that skip the LLM)."""
        return {"strong": 0.9, "weak": 0.3, "skip": 0.05, "rephrase": 0.5,
                "repeat": 0.5, "offtopic": 0.15, "silent": 0.05}.get(
            "strong" if self.mode == "mixed" else self.mode, 0.5)


# ── Intent ground-truth corpus (the "many prompts") ──────────────────────────
# Expected intents match the engine's classifier vocabulary: repeat / rephrase / skip / others.
INTENT_CORPUS = [
    ("Can you repeat that? I couldn't hear you.", "repeat"),
    ("Sorry, you're breaking up, say again?", "repeat"),
    ("Hello? Can you hear me?", "repeat"),
    ("What do you mean by that? Can you rephrase?", "rephrase"),
    ("I'm not sure I understand the question, can you explain it differently?", "rephrase"),
    ("I don't know, let's skip this one.", "skip"),
    ("No idea, sorry, move on.", "skip"),
    ("Yes, I built a REST API in Python for my capstone; it used FastAPI and handled auth.",
     "others"),
    ("I used indexing and query planning to make the SQL 10x faster in my internship.",
     "others"),
    ("A hash map gives O(1) average lookup by hashing the key to a bucket.", "others"),
]


# ── Config matrix for question-generation validation ─────────────────────────
def sample_configs():
    """A spread of interview configs (role × difficulty × counts) whose question plan
    must be honored exactly by the deterministic format tracker."""
    return [
        {"questions_per_round": 4, "jd_question_count": 2, "resume_question_count": 2,
         "conceptual_question_count": 4, "scenario_question_count": 0},
        {"questions_per_round": 4, "jd_question_count": 1, "resume_question_count": 3,
         "conceptual_question_count": 3, "scenario_question_count": 1},
        {"questions_per_round": 6, "jd_question_count": 4, "resume_question_count": 2,
         "conceptual_question_count": 4, "scenario_question_count": 2},
        {"questions_per_round": 8, "jd_question_count": 6, "resume_question_count": 2,
         "conceptual_question_count": 7, "scenario_question_count": 1},
        {"questions_per_round": 10, "jd_question_count": 6, "resume_question_count": 4,
         "conceptual_question_count": 8, "scenario_question_count": 2},
        {"questions_per_round": 5, "jd_question_count": 5, "resume_question_count": 0,
         "conceptual_question_count": 0, "scenario_question_count": 5},
        {"questions_per_round": 5, "jd_question_count": 0, "resume_question_count": 5,
         "conceptual_question_count": 5, "scenario_question_count": 0},
        {"questions_per_round": 12, "jd_question_count": 8, "resume_question_count": 4,
         "conceptual_question_count": 10, "scenario_question_count": 2},
    ]
