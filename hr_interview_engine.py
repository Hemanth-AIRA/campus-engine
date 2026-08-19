"""
HR Interview Engine — Curious human interviewer.

The engine behaves like a thoughtful, curious human interviewer — not a script reader.
It listens to what the candidate says, asks intelligent follow-ups that reference
their actual words, and explores strong signals deeper in Phase 2.

Two-phase conversation:
  Phase 1: Cover all core behavioral topics (randomized order, intro first)
  Phase 2: Explore strongest signals deeper with LLM-generated follow-ups
"""

import random
import json
import logging
import time
from enum import Enum
from typing import List, Dict, Optional

from hr_interview_config import HRInterviewConfig

logger = logging.getLogger("HRInterviewEngine")


# ═══════════════════════════════════════════════════════════════════════════
# Structured Logging Events
# ═══════════════════════════════════════════════════════════════════════════

class HREvent(Enum):
    TOPIC_SELECTED = "HR_TOPIC_SELECTED"
    QUESTION_ASKED = "HR_QUESTION_ASKED"
    RESPONSE_RECEIVED = "HR_RESPONSE_RECEIVED"
    FOLLOWUP_PROBE = "HR_FOLLOWUP_PROBE"
    EXPLORATION_STARTED = "HR_EXPLORATION_STARTED"
    CURIOUS_FOLLOWUP = "HR_CURIOUS_FOLLOWUP"
    INTERVIEW_COMPLETE = "HR_INTERVIEW_COMPLETE"
    INTERVIEW_INCOMPLETE = "HR_INTERVIEW_INCOMPLETE"


# ═══════════════════════════════════════════════════════════════════════════
# Question Bank — Conversational, warm, human-sounding
# ═══════════════════════════════════════════════════════════════════════════

QUESTION_BANK = {
    "intro": [
        "I'd love to start by getting to know you a bit. What's your story — how did you end up doing what you do today?",
        "Before we dive into anything specific, tell me a bit about yourself. What's been the most interesting part of your career so far?",
        "Let's just start with you — what does a typical day look like in your current role, and what do you enjoy most about it?",
        "Walk me through your journey a little. What got you into this field, and what's kept you here?",
        "If you had to describe the thread that connects all your career moves so far, what would that be?",
    ],
    "ownership": [
        "Was there ever a time where something was falling through the cracks and you just decided, 'okay, I'm the one fixing this' — even though it wasn't technically your job?",
        "I'm curious — has there been a moment where you kind of went above and beyond what was expected? Like you saw a gap and just filled it without being asked?",
        "Sometimes the toughest wins are the ones where nobody told you what to do. Have you been in a spot like that — where you had to figure it all out and own the outcome?",
        "What's a project or initiative that you're genuinely proud of, where the success really came down to you personally driving it forward?",
        "Think about a time something important was about to fail. Did you step in? What happened?",
        "Have you ever volunteered for something that scared you a little — where the stakes felt high and you weren't 100% sure you could deliver?",
    ],
    "conflict": [
        "So here's one — we've all had moments where we didn't see eye to eye with someone we work with. What's your go-to approach when that happens?",
        "Think of a time when things got genuinely tense with a colleague. Like, not just a disagreement but real friction. How did you handle it?",
        "Has there been a time you thought a decision was flat-out wrong, but it wasn't your call? What did you do?",
        "Have you ever had to give someone hard feedback — like telling them something they really didn't want to hear? How did that conversation go?",
        "What's the hardest conversation you've ever had at work? I don't need all the details, but I'm curious about how you navigated it.",
        "Have you ever been in a situation where two people on your team were at odds, and you had to help resolve it? What was your role?",
    ],
    "teamwork": [
        "When you think about the best team you've ever been part of — what made it click? What was different about that one?",
        "How do you usually handle working with someone whose style is completely different from yours? Has that ever caused friction?",
        "Tell me about a time where you couldn't have succeeded alone — you truly needed the team. What was your role in making that happen?",
        "What's your honest take on remote vs in-person collaboration? Like, what do you think teams lose or gain?",
        "Have you ever been the new person on an established team? How did you build trust and find your footing?",
        "What do you think is the single most important thing for a team to function well? And do you actively try to create that?",
    ],
    "motivation": [
        "What kind of work genuinely lights you up? Like when you lose track of time because you're so into it — what does that look like?",
        "When you think about where you want to be in three years, not just the title — what does the day-to-day actually look like?",
        "I'm curious — was there a particular moment or project that made you think, 'this is exactly the kind of work I want to do'?",
        "What's motivating this move right now? Like, what's the gap between where you are and where you want to be?",
        "Is there a leader or mentor who really shaped how you think about your career? What did they teach you?",
        "If money and titles were off the table, what would you still want to work on?",
    ],
    "adaptability": [
        "Tell me about a time when the ground shifted under you — priorities changed, plans got scrapped, something unexpected happened. How did you roll with it?",
        "Have you ever had to learn something completely new in a really short timeframe? Not just a tool, but like a whole new domain or way of thinking?",
        "When unexpected changes hit — a reorg, a project cancellation, a team shake-up — are you the person who gets energized or the one who needs a minute to process?",
        "What's the most uncomfortable professional situation you've been in? How did you adapt?",
        "Has there been a time where you had to fundamentally change your approach to something because the old way just wasn't working anymore?",
        "How do you handle ambiguity? Like, when nobody knows the right answer and you just have to make your best call — are you comfortable with that?",
    ],
    "reflection": [
        "Here's one I love asking — can you tell me about a time you genuinely messed something up at work? Not a small thing, but a real mistake. What happened and what did you learn?",
        "Is there a decision you've made professionally that you look back on and think, 'yeah, I'd do that completely differently now'?",
        "What's something you're still working on — a growth area you're honest with yourself about?",
        "If you could go back five years and give yourself one piece of career advice, what would it be?",
        "What's the toughest feedback you've ever received? Like, the kind that stung a little. How did you process it?",
        "When you look back at your career so far, is there something you wish you'd said yes to — or said no to — earlier?",
    ],
    "closing": [
        "We've covered a lot of great ground. As we wrap up — beyond the job description, what are you really looking for in your next chapter?",
        "Before we finish up — is there anything you want to ask about the team, the culture, or what it's actually like to work here?",
        "We're coming to the end here. Is there anything we haven't talked about that you think is important for us to know about you?",
        "Last thing — if you got this role, what's the first thing you'd want to dig into? What would week one look like for you?",
    ],
}


# ═══════════════════════════════════════════════════════════════════════════
# Warm Acknowledgments
# ═══════════════════════════════════════════════════════════════════════════

WARM_ACKS = [
    "That's really great to hear.",
    "I appreciate you sharing that — it says a lot.",
    "Thanks for walking me through that.",
    "That sounds like a really meaningful experience.",
    "I can see why that stood out to you.",
    "That's a great perspective to have.",
    "I love that you brought that up.",
    "That makes a lot of sense.",
    "Interesting — I really like how you approached that.",
    "That's honest, and I respect that.",
    "That says a lot about you, actually.",
    "I hear you — that's not an easy thing to navigate.",
    "That's a really mature way to look at it.",
    "It sounds like that was a real turning point for you.",
    "Yeah, I think a lot of people underestimate how hard that is.",
    "That's the kind of thing that really builds character, honestly.",
    "I can tell that experience really stuck with you.",
    "You clearly thought a lot about how to handle that.",
    "That's not something everyone would admit — I appreciate the honesty.",
]

# Topic transition phrases — natural conversation pivots
TOPIC_TRANSITIONS = {
    "ownership": [
        "That actually connects to something I'm curious about —",
        "You know what's interesting about what you just said? It makes me wonder —",
        "Building on that —",
        "That's a nice segue, actually, because I wanted to ask —",
    ],
    "conflict": [
        "Now, shifting gears a little —",
        "Okay, here's one that's a little different.",
        "This one's always interesting to me.",
        "Along those lines — working with people isn't always smooth, right?",
    ],
    "teamwork": [
        "I'm curious about the team side of things.",
        "Let me ask you something about collaboration.",
        "That's great context. On the people front —",
        "So, thinking about how you work with others —",
    ],
    "motivation": [
        "I'd love to switch gears and talk about what drives you.",
        "Okay, different question.",
        "Let me ask you something more personal.",
        "This is one of my favorite questions to ask —",
    ],
    "adaptability": [
        "One thing I'm always curious about is how people handle change.",
        "Alright, here's a different kind of question.",
        "Thanks for sharing that. Now,",
        "Let me throw something different at you.",
    ],
    "reflection": [
        "I appreciate your openness so far. Here's a deeper one —",
        "You've been really thoughtful. I'm curious —",
        "This one's a bit more personal, if you're comfortable with it.",
        "Here's one I love asking because it tells me a lot —",
    ],
    "closing": [
        "We've had a really great conversation. As we wrap up,",
        "I've really enjoyed hearing your perspective. Before we finish,",
        "This has been a great chat. One last thing —",
        "Okay, we're coming to the end here.",
    ],
}

# Patterns that indicate the candidate is asking to repeat/clarify the question
REPEAT_PATTERNS = [
    "repeat", "say that again", "come again", "didn't catch",
    "could you repeat", "can you repeat", "one more time",
    "sorry what", "what was the question", "lost you",
    "didn't hear", "say again", "pardon", "excuse me",
    "can you say", "could you say", "rephrase",
    "what did you say", "what do you mean", "mind repeating",
    "didn't understand", "went blank", "mind went blank",
]

# Follow-up probes for short answers
FOLLOWUP_PROBES = [
    "That's interesting — can you unpack that a little more?",
    "I'd love to hear the details — like, what actually happened step by step?",
    "Can you give me a specific example of when that played out?",
    "I'm curious — what was the outcome? Like, how did it actually end?",
    "There's more to that story, isn't there? I'd love to hear it.",
    "What was going through your head at that point?",
    "And how did the other person receive that?",
    "What would you have done differently if you could rewind?",
]

# Short-answer threshold (word count)
SHORT_ANSWER_THRESHOLD = 20

# Phase 2: Exploration follow-up prompt
EXPLORATION_PROMPT = """You are a warm, genuinely curious HR interviewer having a natural conversation.

The candidate just shared this about {topic}:
"{answer}"

Generate ONE short, conversational follow-up question (8-18 words) that:
- References ONE specific detail from their answer (a name, a decision, an emotion, a consequence)
- Probes WHY they made that choice or HOW they felt — not just what happened
- Sounds like something a friend at a dinner party would ask, not a corporate interviewer
- Does NOT start with "Tell me", "Can you describe", or "Could you elaborate"
- Does NOT repeat what they said back to them as a question

Good examples:
- "What was going through your head when you decided to push back on that?"
- "How did the team react when you made that call?"
- "Was there a moment where you genuinely thought it wasn't going to work?"
- "Did that change how you approach those situations now?"
- "What would you have done differently if you knew then what you know now?"

Return ONLY the follow-up question, nothing else."""

# Micro-reflection prompt — generates acks that paraphrase what the candidate said
MICRO_REFLECTION_PROMPT = """The candidate just said this in an interview:
"{answer}"

Generate a short, warm acknowledgment (8-15 words) that shows you were actually LISTENING.
Paraphrase the most emotionally charged or interesting part of their answer.
Do NOT ask a question. Just acknowledge what they said.

Good examples:
- "It sounds like that project really pushed you out of your comfort zone."
- "So the coordination across teams was the hardest part of that."
- "That kind of pressure from leadership can be really challenging."
- "Yeah, that sounds like one of those moments that really shapes how you work."
- "I can see why that was frustrating — it sounds like the process was working against you."

Bad examples (too generic — DO NOT use these):
- "That's great to hear."
- "Thank you for sharing."
- "That's really interesting."
- "I appreciate you telling me that."

Return ONLY the acknowledgment, nothing else."""


# ═══════════════════════════════════════════════════════════════════════════
# HR Interview Engine — Curious Human Interviewer
# ═══════════════════════════════════════════════════════════════════════════

class HRInterviewEngine:
    """
    Two-phase conversational behavioral interview engine.

    Phase 1: Cover all core behavioral topics (intro first, rest randomized)
    Phase 2: Explore the strongest signals deeper with context-aware follow-ups

    The engine generates warm, natural dialogue that references what the candidate
    actually said — like a curious human interviewer.
    """

    def __init__(self, interview_id: int, config: HRInterviewConfig):
        self.interview_id = interview_id
        self.config = config
        self.transcript: List[Dict] = []
        self.asked_questions: set = set()
        self.response_count: int = 0
        self.start_time: float = time.time()
        self._last_topic: Optional[str] = None
        self._used_acks: set = set()
        self._phase: int = 1  # 1 = topic coverage, 2 = exploration
        self._exploration_count: int = 0
        self._max_explorations: int = 3  # Max Phase 2 follow-ups

        # Coverage tracks CORE BEHAVIORAL topics only
        self.coverage_state: Dict[str, bool] = {
            topic: False for topic in config.topics
        }

        self._log(HREvent.TOPIC_SELECTED, detail=f"Engine initialized. Topics: {config.topics}")

    # ── Phase Detection ──────────────────────────────────────────

    @property
    def phase(self) -> int:
        return self._phase

    def _check_phase_transition(self):
        """Move to Phase 2 when all topics covered."""
        if self._phase == 1 and all(self.coverage_state.values()):
            self._phase = 2
            self._log(
                HREvent.EXPLORATION_STARTED,
                detail=f"All topics covered. Entering Phase 2 exploration (max {self._max_explorations} follow-ups)"
            )

    # ── Topic Selection ──────────────────────────────────────────

    def select_next_topic(self) -> str:
        """
        Phase 1: Intro first, rest randomized, closing last.
        Phase 2: Pick the topic with the richest signal for deeper exploration.
        """
        self._check_phase_transition()

        if self._phase == 2:
            elapsed = (time.time() - self.start_time) / 60.0
            time_limit = self.config.max_duration_minutes * 0.8  # 80% of max
            if self._exploration_count >= self._max_explorations or elapsed >= time_limit:
                if elapsed >= time_limit:
                    self._log(HREvent.INTERVIEW_COMPLETE, detail=f"Phase 2 time cutoff: {elapsed:.1f}m >= {time_limit:.1f}m")
                return "closing"
            return self._pick_exploration_topic()

        remaining = [t for t, covered in self.coverage_state.items() if not covered]
        if not remaining:
            return "closing"

        # Intro always first
        if "intro" in remaining and self.response_count == 0:
            topic = "intro"
        else:
            choices = [t for t in remaining if t != "intro" or self.response_count == 0]
            topic = random.choice(choices) if choices else remaining[0]

        self._log(HREvent.TOPIC_SELECTED, topic=topic, detail=f"Phase 1. Remaining: {remaining}")
        return topic

    def _pick_exploration_topic(self) -> str:
        """Pick the topic that had the strongest/most interesting signal."""
        # Find entries with the longest answers (strongest signals)
        scored = {}
        for entry in self.transcript:
            if entry["type"] != "primary":
                continue
            topic = entry["topic"]
            word_count = len(entry.get("answer", "").split())
            if topic not in scored or word_count > scored[topic]:
                scored[topic] = word_count

        # Sort by answer length (proxy for signal strength), pick from top 3
        ranked = sorted(scored.items(), key=lambda x: x[1], reverse=True)
        # Filter out topics we've already explored in Phase 2
        explored = {e["topic"] for e in self.transcript if e.get("type") == "exploration"}
        candidates = [t for t, _ in ranked if t not in explored and t not in ("intro", "closing")]

        if not candidates:
            return "closing"

        topic = candidates[0]
        self._log(HREvent.TOPIC_SELECTED, topic=topic, detail=f"Phase 2 exploration. Signal strength ranking: {ranked[:3]}")
        return topic

    # ── Question Generation ──────────────────────────────────────

    def generate_question(self, topic: str) -> str:
        """
        Phase 1: Warm ack + transition + banked question.
        Phase 2: Returns None — use generate_exploration_question() instead.
        """
        if self._phase == 2 and topic != "closing":
            return None  # Signal to caller to use generate_exploration_question

        # Pick question (no repeats)
        bank = QUESTION_BANK.get(topic, QUESTION_BANK.get("closing", []))
        available = [q for q in bank if q not in self.asked_questions]
        if not available:
            available = bank

        question = random.choice(available)
        self.asked_questions.add(question)

        # First question — no ack needed
        if self.response_count == 0:
            self._log(HREvent.QUESTION_ASKED, topic=topic, detail=f"Q: {question[:60]}...")
            return question

        # Build conversational turn: micro-reflection + transition + question
        parts = []

        # 1. Micro-reflection (LLM) or warm ack (fallback)
        ack = self._get_micro_reflection(llm_client=None)  # No client in Phase 1
        parts.append(ack)

        # 2. Topic transition
        if topic != self._last_topic and topic in TOPIC_TRANSITIONS:
            transition = random.choice(TOPIC_TRANSITIONS[topic])
            parts.append(transition)

        # 3. The question (with smart casing)
        if len(parts) > 1 and question[0].isupper():
            last_part = parts[-1].strip()
            if last_part.endswith(",") or last_part.endswith("—") or last_part.endswith("–"):
                question = question[0].lower() + question[1:]

        parts.append(question)

        full_turn = " ".join(parts)
        self._log(HREvent.QUESTION_ASKED, topic=topic, detail=f"Q: {full_turn[:80]}...")
        return full_turn

    def generate_exploration_question(self, topic: str, llm_client=None) -> str:
        """
        Phase 2: Generate a curious follow-up that references what the candidate
        actually said about this topic.
        """
        # Find the candidate's original answer for this topic
        original = None
        for entry in reversed(self.transcript):
            if entry["topic"] == topic and entry["type"] == "primary":
                original = entry
                break

        if not original:
            return self.generate_question("closing")

        answer_text = original.get("answer", "")

        # Get micro-reflection ack (LLM-powered if client available)
        ack = self._get_micro_reflection(llm_client=llm_client, answer_override=answer_text)

        # Try LLM-generated follow-up question
        if llm_client and answer_text:
            try:
                response = llm_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": EXPLORATION_PROMPT.format(
                            topic=topic, answer=answer_text[:500]
                        )},
                    ],
                    temperature=0.8,
                    max_tokens=40,  # Shorter questions
                )
                followup_q = response.choices[0].message.content.strip().strip('"')
                full_turn = f"{ack} {followup_q}"

                self._exploration_count += 1
                self._log(
                    HREvent.CURIOUS_FOLLOWUP, topic=topic,
                    detail=f"Phase 2 LLM: {full_turn[:80]}..."
                )
                return full_turn

            except Exception as e:
                logger.warning(f"LLM exploration follow-up failed: {e}")

        # Fallback: template-based follow-up
        fallback_templates = [
            f"Going back to what you said about {topic} — what did you take away from that?",
            f"That {topic} example was interesting. Would you approach it differently now?",
            f"What was the hardest part of that {topic} situation for you?",
        ]
        followup = random.choice(fallback_templates)
        full_turn = f"{ack} {followup}"

        self._exploration_count += 1
        self._log(
            HREvent.CURIOUS_FOLLOWUP, topic=topic,
            detail=f"Phase 2 fallback: {full_turn[:80]}..."
        )
        return full_turn

    def _get_micro_reflection(self, llm_client=None, answer_override: str = None) -> str:
        """
        Get a micro-reflection acknowledgment.
        
        If LLM client is available, generates a paraphrase of what the candidate said.
        Otherwise falls back to generic warm acks.
        """
        # Try LLM micro-reflection
        if llm_client:
            # Use provided answer or get the last one from transcript
            answer_text = answer_override
            if not answer_text and self.transcript:
                answer_text = self.transcript[-1].get("answer", "")

            if answer_text and len(answer_text.split()) > 10:
                try:
                    response = llm_client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[
                            {"role": "system", "content": MICRO_REFLECTION_PROMPT.format(
                                answer=answer_text[:300]
                            )},
                        ],
                        temperature=0.7,
                        max_tokens=30,
                    )
                    reflection = response.choices[0].message.content.strip().strip('"')
                    if reflection and len(reflection.split()) <= 20:
                        return reflection
                except Exception:
                    pass  # Fall through to generic

        # Fallback: generic warm ack
        available = [a for a in WARM_ACKS if a not in self._used_acks]
        if not available:
            self._used_acks.clear()
            available = list(WARM_ACKS)

        ack = random.choice(available)
        self._used_acks.add(ack)
        return ack

    # ── Answer Recording ─────────────────────────────────────────

    def _is_repeat_request(self, answer: str) -> bool:
        """Detect if the candidate is asking to repeat/clarify the question."""
        if not answer:
            return False
        answer_lower = answer.lower().strip()
        return any(pattern in answer_lower for pattern in REPEAT_PATTERNS)

    def record_answer(
        self, topic: str, question: str, answer: str, entry_type: str = "primary"
    ) -> str:
        """Record the candidate's answer. Returns 'repeat', 'probe', or 'none'.

        - 'repeat': candidate asked to repeat the question → re-ask same question
        - 'probe':  answer too short → ask a follow-up probe
        - 'none':   answer is fine → move on
        """
        # Check for repeat request BEFORE recording
        if self._is_repeat_request(answer):
            self._log(
                HREvent.RESPONSE_RECEIVED, topic=topic,
                detail=f"REPEAT REQUEST detected: '{answer[:50]}'"
            )
            # Don't count this as a real answer — un-mark coverage
            # Don't add to transcript (it's noise)
            return "repeat"

        self.transcript.append({
            "topic": topic,
            "question": question,
            "answer": answer,
            "type": entry_type,
            "timestamp": time.time(),
        })
        self.response_count += 1
        self._last_topic = topic

        if topic in self.coverage_state:
            self.coverage_state[topic] = True

        word_count = len(answer.split()) if answer else 0
        self._log(
            HREvent.RESPONSE_RECEIVED,
            topic=topic,
            detail=f"Words: {word_count}, Phase: {self._phase}, Responses: {self.response_count}",
        )

        # Probe if answer is too short (only in Phase 1)
        if word_count < SHORT_ANSWER_THRESHOLD and self._phase == 1:
            return "probe"
        return "none"

    def get_repeat_response(self, original_question: str) -> str:
        """Generate a warm repeat of the question."""
        prefixes = [
            "Of course, no worries!",
            "Sure thing!",
            "No problem at all!",
            "Absolutely, let me rephrase that.",
            "Sure, I'll say that again.",
        ]
        prefix = random.choice(prefixes)
        self._log(HREvent.QUESTION_ASKED, detail=f"Repeating question: {original_question[:50]}...")
        return f"{prefix} {original_question}"

    def get_followup_probe(self) -> str:
        """Get a friendly follow-up probe for a short answer."""
        probe = random.choice(FOLLOWUP_PROBES)
        self._log(HREvent.FOLLOWUP_PROBE, detail=f"Probe: {probe}")
        return probe

    def record_followup(self, topic: str, probe: str, answer: str) -> None:
        """Record a follow-up answer."""
        self.transcript.append({
            "topic": topic,
            "question": probe,
            "answer": answer,
            "type": "followup",
            "timestamp": time.time(),
        })
        self.response_count += 1
        self._log(
            HREvent.RESPONSE_RECEIVED,
            topic=topic,
            detail=f"Follow-up recorded. Words: {len(answer.split()) if answer else 0}",
        )

    # ── Completion Checks ────────────────────────────────────────

    def is_complete(self) -> bool:
        """Check if the interview should end."""
        elapsed_minutes = (time.time() - self.start_time) / 60.0

        if elapsed_minutes >= self.config.max_duration_minutes:
            self._log(HREvent.INTERVIEW_COMPLETE, detail=f"Duration limit: {elapsed_minutes:.1f}m")
            return True

        # Phase 2 complete (all explorations done or closing reached)
        if self._phase == 2 and self._exploration_count >= self._max_explorations:
            self._log(HREvent.INTERVIEW_COMPLETE, detail=f"Phase 2 complete: {self._exploration_count} explorations")
            return True

        # Phase 1: not done until all topics covered
        if self._phase == 1:
            return False

        return False

    def get_completion_status(self) -> Dict:
        """Get detailed completion status for reporting."""
        elapsed_minutes = (time.time() - self.start_time) / 60.0
        covered = sum(1 for v in self.coverage_state.values() if v)
        total = len(self.coverage_state)

        is_sufficient = (
            self.response_count >= self.config.min_responses
            and elapsed_minutes >= self.config.min_duration_minutes
        )

        status = "complete" if is_sufficient else "incomplete"

        if not is_sufficient:
            self._log(
                HREvent.INTERVIEW_INCOMPLETE,
                detail=(
                    f"Insufficient: responses={self.response_count}/{self.config.min_responses}, "
                    f"duration={elapsed_minutes:.1f}/{self.config.min_duration_minutes}m"
                ),
            )

        return {
            "status": status,
            "phase": self._phase,
            "topics_covered": covered,
            "topics_total": total,
            "explorations": self._exploration_count,
            "responses": self.response_count,
            "duration_minutes": round(elapsed_minutes, 1),
            "coverage": dict(self.coverage_state),
        }

    def get_transcript(self) -> List[Dict]:
        return list(self.transcript)

    def get_transcript_text(self) -> str:
        """Return transcript as formatted string for the evaluator."""
        lines = []
        for entry in self.transcript:
            phase_tag = "[Phase 2 Exploration]" if entry.get("type") == "exploration" else f"[Topic: {entry['topic']}]"
            lines.append(phase_tag)
            lines.append(f"Interviewer: {entry['question']}")
            lines.append(f"Candidate: {entry['answer']}")
            lines.append("")
        return "\n".join(lines)

    # ── Structured Logging ───────────────────────────────────────

    def _log(self, event: HREvent, topic: str = "", detail: str = ""):
        logger.info(
            f"📋 [{event.value}] interview={self.interview_id} "
            + (f"topic='{topic}' " if topic else "")
            + (f"| {detail}" if detail else "")
        )
