#!/usr/bin/env python3
"""
pregenerate_question_bank.py  —  SELF-CONTAINED reference for the campus backend (Task 2).

WHY THIS EXISTS
---------------
Cartesia TTS is the dominant per-interview cost and has a hard 5-concurrent limit, so
synthesizing every question live for every candidate does not scale. The fix: generate
the JD/skill questions AND their TTS audio ONCE per (role_level, difficulty, skill), then
reuse that audio for EVERY candidate interviewing for that role — zero live TTS for these.

This is the generator. It takes a skill list (+ role, role level, difficulty), produces N
interview questions per skill using the SAME logic the live AIRA engine uses (the planner
prompt + difficulty-vector engine are embedded below, verbatim from the engine), renders
each to speech with the SAME Cartesia voice/format as the live bot, and emits the audio as
base64 in one JSON bank keyed by (role_level, difficulty, skill).

This file is fully self-contained: it has NO imports from the AIRA engine — the difficulty
engine, planner prompt, and Cartesia config are all inlined below. Only third-party deps are
`openai` and `cartesia` (pip install openai cartesia python-dotenv).

AUDIO FORMAT
------------
`audio_base64` decodes to RAW PCM: signed 16-bit little-endian, 24 kHz, mono
(encoding=pcm_s16le, sample_rate=24000, container=raw) — exactly what the live bot publishes
to LiveKit. Use pcm_to_wav_bytes() (bottom) to wrap it into a playable WAV.

USAGE
-----
    python pregenerate_question_bank.py \
        --skills "Python,SQL,System Design" \
        --role "Backend Engineer" --role-level mid --difficulty medium --count 3

Env: OPENAI_API_KEY, CARTESIA_API_KEY. Optional: CARTESIA_VOICE_ID, CARTESIA_MODEL,
QUESTION_GEN_MODEL (default gpt-4o-mini).

OUTPUT SCHEMA
-------------
{"meta": {role, role_level, difficulty, question_gen_model, difficulty_vector{A,C,Amb,S},
          audio{encoding,sample_rate,container,channels}, voice_id, cartesia_model, generated_at},
 "questions": [{skill, question_no, question_text, duration_ms, audio_bytes, audio_base64}, ...]}
"""

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import random
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv(override=True)

logger = logging.getLogger("pregenerate_question_bank")

# ═════════════════════════════════════════════════════════════════════════════════════
#  EMBEDDED (verbatim from the AIRA engine core/difficulty_policy.py) — DIFFICULTY ENGINE
#  DifficultyVector + paradigm tables + compute_difficulty_vector + compile_vector_prompt.
#  This is what scales a question's hardness by (skill, role level, difficulty).
# ═════════════════════════════════════════════════════════════════════════════════════
@dataclass
class DifficultyVector:
    abstraction_depth: int
    constraint_pressure: int
    ambiguity: int
    scale_complexity: int
    
    def to_dict(self) -> dict:
        return {
            "A": self.abstraction_depth,
            "C": self.constraint_pressure,
            "Amb": self.ambiguity,
            "S": self.scale_complexity
        }
    
    def __str__(self) -> str:
        return f"A={self.abstraction_depth} C={self.constraint_pressure} Amb={self.ambiguity} S={self.scale_complexity}"


# Base difficulty -> starting abstraction depth
_DIFFICULTY_TO_BASE_ABSTRACTION = {
    "easy": 1,
    "medium": 2,
    "hard": 3,
}

# Role seniority maps to baseline offset for abstraction
_ROLE_OFFSET = {
    "junior": 0,
    "mid": 0,
    "senior": 1,
}

# ═══════════════════════════════════════════════════════════════════════════
# VECTOR FRAGMENTS — Enforcement-Grade Behavioral Briefs
# Each fragment is a comprehensive instruction set that tells the LLM
# exactly what kind of question to generate, with examples and anti-patterns.
# High-vector fragments are deliberately verbose to outweigh the system
# prompt persona (~5000 tokens). Token weight = behavioral influence.
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# PARADIGM POOLS — Code-controlled question variety
# Each level has a pool of paradigms. One is selected via random.choice()
# BEFORE being sent to the LLM, giving us deterministic control.
# ═══════════════════════════════════════════════════════════════════════════

_ABSTRACTION_PARADIGMS = {
    1: [
        {
            "name": "Definition",
            "instruction": (
                "  Ask about a specific concept naturally — like you're genuinely curious.\n"
                "  Vary your phrasing: don't always start with 'What is...'.\n"
                "  Examples of natural phrasings:\n"
                "    - 'So when people talk about [X], what does that actually mean in practice?'\n"
                "    - 'I hear [X] come up a lot — how would you explain it to someone new on your team?'\n"
                "  Keep it answerable in 2-3 sentences.\n"
            ),
        },
        {
            "name": "Mechanism",
            "instruction": (
                "  Ask how something works under the hood, as if you're debugging together.\n"
                "  Make it specific — don't ask about the entire technology, ask about ONE behavior.\n"
                "  Examples:\n"
                "    - 'So when you call [X], what's actually happening behind the scenes?'\n"
                "    - 'Walk me through what happens step by step when [specific action occurs].'\n"
            ),
        },
        {
            "name": "Purpose & Use Case",
            "instruction": (
                "  Ask about when and why someone would reach for a specific tool or pattern.\n"
                "  Ground it in a real scenario, not abstract theory.\n"
                "  Examples:\n"
                "    - 'When would you actually reach for [X] instead of just doing it the simple way?'\n"
                "    - 'What's a real situation where [X] saved the day for you or your team?'\n"
            ),
        },
    ],
    2: [
        {
            "name": "Cause & Effect",
            "instruction": (
                "  Set up a specific situation and ask what happens next.\n"
                "  The candidate should explain a reasoning chain, not just definitions.\n"
                "  Examples:\n"
                "    - 'So your app is doing [X] and suddenly [Y] happens. What breaks first and why?'\n"
                "    - 'Let's say you change [X] in production. What's the ripple effect?'\n"
            ),
        },
        {
            "name": "Decision Rationale",
            "instruction": (
                "  Put the candidate in a real decision moment and make them justify their pick.\n"
                "  They should explain their reasoning, not just name a preference.\n"
                "  Examples:\n"
                "    - 'You've got two perfectly valid ways to do this. What's actually going through\n"
                "      your mind when you pick one over the other?'\n"
                "    - 'Your teammate wants to do it [way A]. You think [way B] is better. Make your case.'\n"
            ),
        },
        {
            "name": "Behavioral Explanation",
            "instruction": (
                "  Describe a specific runtime condition and ask the candidate to predict what happens.\n"
                "  Make it concrete — give them a real scenario to trace through.\n"
                "  Examples:\n"
                "    - 'You run [operation on a listed skill] with [specific input] and [condition]\n"
                "      happens. Walk me through what actually happens at that moment.'\n"
                "    - 'You change [parameter/setting in a listed skill] and re-run. What's different,\n"
                "      and why?'\n"
            ),
        },
    ],
    3: [
        {
            "name": "Tool Comparison",
            "instruction": (
                "  Present TWO specific tools or technologies and make the candidate pick one.\n"
                "  Name the tools explicitly — don't be vague. Both must be reasonable choices.\n"
                "  Examples:\n"
                "    - 'For this use case, are you reaching for [A] or [B]? And be honest — what\n"
                "      are you giving up with that choice?'\n"
                "    - 'Your team is split on using [A] versus [B]. You're the tiebreaker. Go.'\n"
            ),
        },
        {
            "name": "Strategy Trade-off",
            "instruction": (
                "  Present a concrete scenario with two valid implementation strategies.\n"
                "  Force the candidate to commit and explain what they lose.\n"
                "  Examples:\n"
                "    - 'You could do this as a batch job that runs every night, or stream it in \n"
                "      real-time. What's your gut say, and what's the cost of being wrong?'\n"
                "    - 'Cache it aggressively or compute it fresh every time — for this workload,\n"
                "      which way do you lean?'\n"
            ),
        },
        {
            "name": "Design Pattern Choice",
            "instruction": (
                "  Set up a concrete feature requirement and offer two design approaches.\n"
                "  The candidate must pick one and own the downsides.\n"
                "  Examples:\n"
                "    - 'You're building [feature]. Do you go with [pattern A] or [pattern B]?\n"
                "      Walk me through why, and don't dodge the downsides.'\n"
                "    - 'Two senior engineers on your team disagree on the approach. One says\n"
                "      [A], the other says [B]. Who are you siding with?'\n"
            ),
        },
    ],
    4: [
        {
            "name": "Architectural Trade-off",
            "instruction": (
                "  Present a real-world system problem with TWO specific architectural choices\n"
                "  (e.g., monolith vs microservices, event-driven vs synchronous, SQL vs NoSQL).\n"
                "  Force the candidate to pick one, defend it, and explain what they lose.\n"
                "  E.g., 'You need to add semantic search to a PostgreSQL app. Would you embed pgvector\n"
                "  inside Postgres, or stand up a separate FAISS cluster? What breaks at 100K queries/day?'\n"
            ),
        },
        {
            "name": "System Design from Scratch",
            "instruction": (
                "  Ask the candidate to design a complete system given specific requirements and constraints.\n"
                "  Demand they name exact technologies, define data flow, and identify failure points.\n"
                "  E.g., 'Design a real-time notification system that must deliver to 1M users within 5 seconds.\n"
                "  What is your tech stack? Where is the bottleneck?'\n"
            ),
        },
        {
            "name": "Scaling an Existing System",
            "instruction": (
                "  Describe a system that currently works at small scale (e.g., 100 users, 1K requests/day).\n"
                "  Ask how they would redesign it to handle massive scale (e.g., 10M users).\n"
                "  Force them to identify which components break first and what needs to be replaced.\n"
                "  E.g., 'Your document processing pipeline handles 100 PDFs/day with a single server.\n"
                "  How would you scale it to 50K PDFs/day? What is the first thing that fails?'\n"
            ),
        },
        {
            "name": "Migration & Refactoring",
            "instruction": (
                "  Present a legacy or monolithic system that must be modernized with zero downtime.\n"
                "  Ask for a step-by-step migration strategy, risk assessment, and rollback plan.\n"
                "  E.g., 'Your team has a large legacy [component built on a listed skill] that must be\n"
                "  modernized to [target approach in the same skill area] with zero downtime over 3\n"
                "  months. How do you phase this? What is the riskiest step?' (Use ONLY the listed skills.)\n"
            ),
        },
        {
            "name": "Technology Evaluation",
            "instruction": (
                "  Ask the candidate to evaluate adopting a new technology for an existing production system.\n"
                "  They must discuss compatibility, learning curve, migration cost, and operational risk.\n"
                "  E.g., 'Your team is considering replacing RabbitMQ with Kafka for event streaming.\n"
                "  What questions do you ask before making this decision? What is the biggest risk?'\n"
            ),
        },
    ],
    5: [
        {
            "name": "Active Incident Triage",
            "instruction": (
                "  Describe a system that is ACTIVELY FAILING right now with cascading effects and time pressure.\n"
                "  The candidate must triage, stop the bleeding, identify root cause, and propose a permanent fix.\n"
                "  E.g., 'Your database connection pool is exhausted during peak traffic. Downstream services\n"
                "  are timing out, and the error rate just crossed 50%. You have 10 minutes. What do you do?'\n"
            ),
        },
        {
            "name": "Chaos Engineering & Resilience",
            "instruction": (
                "  Propose a catastrophic external event (AZ failure, 3rd-party API blackout, DNS outage).\n"
                "  Ask how the candidate would architect the system AHEAD OF TIME to survive automatically.\n"
                "  E.g., 'Your primary cloud region goes completely offline. How would you design your\n"
                "  system so that users never notice? What is the hardest part of multi-region failover?'\n"
            ),
        },
        {
            "name": "Mystery Bug (Deep Debugging)",
            "instruction": (
                "  Describe a bizarre, highly specific bug with limited observable symptoms.\n"
                "  Ask the candidate to hypothesize causes and walk through their exact debugging methodology.\n"
                "  E.g., 'Every night at 3 AM, memory spikes and 2 out of 8 nodes OOM. Logs show nothing\n"
                "  unusual. The same code runs fine in staging. How do you prove what is happening?'\n"
            ),
        },
        {
            "name": "Post-Mortem Analysis",
            "instruction": (
                "  Describe an incident that already happened. Give partial logs, a timeline, and symptoms.\n"
                "  Ask the candidate to perform a post-mortem: identify root cause, contributing factors,\n"
                "  and actionable follow-ups that prevent recurrence.\n"
                "  E.g., 'Yesterday at 2 PM, our payment service rejected 15%% of transactions for 40 minutes.\n"
                "  Here is what we know: [partial data]. What was the root cause? What action items come out of this?'\n"
            ),
        },
    ],
}


def abstraction_fragment(level: int, paradigm: dict = None) -> str:
    """Generates the abstraction depth fragment for the given level.
    
    If a paradigm dict is provided (selected via random.choice in compile_vector_prompt),
    it is injected as the SPECIFIC question style for that level.
    """
    if level == 1:
        base = (
            "• ABSTRACTION DEPTH: CONCEPT RECALL (Level 1)\n"
            "  GOAL: Test whether the candidate can accurately explain a single, well-defined concept.\n"
        )
        if paradigm:
            base += f"  QUESTION STYLE — {paradigm['name']}:\n{paradigm['instruction']}\n"
        else:
            base += (
                "  HOW TO ASK:\n"
                "    - Ask about ONE concept at a time.\n"
                "    - Sound naturally curious: 'So when people say [X], what does that actually mean?'\n"
                "    - Vary your openings — don't always start the same way.\n"
            )
        base += (
            "  WHAT TO ACCEPT: Clear, factually correct explanations. Textbook answers are fine.\n"
            "  WHAT TO REJECT: Do NOT require justification, trade-offs, or real-world examples.\n"
        )
        return base
    elif level == 2:
        base = (
            "• ABSTRACTION DEPTH: REASONING (Level 2)\n"
            "  GOAL: Test whether the candidate can explain HOW and WHY, not just WHAT.\n"
        )
        if paradigm:
            base += f"  QUESTION STYLE — {paradigm['name']}:\n{paradigm['instruction']}\n"
        else:
            base += (
                "  HOW TO ASK:\n"
                "    - Set up a situation and ask what happens — like you're working through it together.\n"
                "    - Make them explain cause-and-effect, not just definitions.\n"
            )
        base += (
            "  WHAT TO ACCEPT: Answers that explain reasoning chains, not just terminology.\n"
            "  WHAT TO REJECT:\n"
            "    - Pure definitions without WHY.\n"
            "    - 'It depends' without specifying what it depends ON.\n"
        )
        return base
    elif level == 3:
        base = (
            "• ABSTRACTION DEPTH: TRADE-OFFS (Level 3)\n"
            "  GOAL: Force the candidate to choose between TWO valid approaches and justify.\n"
            "\n"
            "  ROLE-SENSITIVE CALIBRATION:\n"
            "    Match the trade-off complexity to the candidate's experience level:\n"
            "    - JUNIOR: Two specific tools or simple approaches they'd actually encounter.\n"
            "    - MID: Two implementation strategies with real-world consequences.\n"
            "    - SENIOR: Two architectural directions with organizational implications.\n"
        )
        if paradigm:
            base += f"\n  QUESTION STYLE — {paradigm['name']}:\n{paradigm['instruction']}\n"
        else:
            base += (
                "\n  HOW TO ASK:\n"
                "    - Present TWO specific, named alternatives (not vague concepts).\n"
                "    - Frame as 'Would you use A or B? Why?' — force a choice.\n"
                "    - Both options must be reasonable — no obviously correct answer.\n"
            )
        base += (
            "  WHAT TO ACCEPT: A clear choice with reasoning and what is LOST.\n"
            "  WHAT TO REJECT: 'It depends' without specifics. Avoiding commitment.\n"
            "  ENFORCEMENT:\n"
            "    - The two named alternatives MUST appear in the question text.\n"
            "    - The complexity of the trade-off MUST match the role level.\n"
        )
        return base
    elif level == 4:
        base = (
            "• ABSTRACTION DEPTH: ARCHITECTURE & SYSTEM DESIGN (Level 4)\n"
            "  ═══════════════════════════════════════════════════════════════\n"
            "  THIS IS A HIGH-PRESSURE ABSTRACTION LEVEL.\n"
            "  DO NOT ASK 'Can you describe...', 'Tell me about your experience...'.\n"
            "  ═══════════════════════════════════════════════════════════════\n"
            "\n"
            "  GOAL: Test system-level thinking, architecture, and operational awareness.\n"
            "  The candidate must commit to a technical direction and defend it.\n"
        )
        if paradigm:
            base += f"\n  QUESTION STYLE — {paradigm['name']}:\n{paradigm['instruction']}\n"
        base += (
            "\n  WHAT TO ACCEPT:\n"
            "    - Explicit architectural decisions with quantitative justification.\n"
            "    - Identification of infrastructure bottlenecks or operational risks.\n"
            "    - References to concrete technologies, caching layers, DB types, or design patterns.\n"
            "\n"
            "  WHAT TO REJECT:\n"
            "    - 'I would evaluate the requirements first' without committing to a choice.\n"
            "    - Generic answers like 'I would use microservices' without decomposition details.\n"
            "    - Purely theoretical answers without operational awareness.\n"
        )
        return base
    elif level == 5:
        base = (
            "• ABSTRACTION DEPTH: FAILURE MODES, RESILIENCE & CHAOS (Level 5)\n"
            "  ═══════════════════════════════════════════════════════════════\n"
            "  THIS IS THE HIGHEST ABSTRACTION LEVEL.\n"
            "  DO NOT ASK 'Tell me about your experience with...'.\n"
            "  ═══════════════════════════════════════════════════════════════\n"
            "\n"
            "  GOAL: Test crisis management, resilience engineering, and deep root-cause analysis.\n"
        )
        if paradigm:
            base += f"\n  QUESTION STYLE — {paradigm['name']}:\n{paradigm['instruction']}\n"
        base += (
            "\n  WHAT TO ACCEPT:\n"
            "    - Clear prioritization of MTTR vs Root Cause Analysis.\n"
            "    - Understanding of circuit breakers, rate limiting, fallback caches, or dead letter queues.\n"
            "    - Methodical debugging steps (flame graphs, thread dumps, specific metrics).\n"
            "\n"
            "  WHAT TO REJECT:\n"
            "    - Generic 'I would add more logging' without specifics.\n"
            "    - Skipping diagnosis and jumping to solutions.\n"
            "    - Listing everything without prioritization.\n"
        )
        return base
    return ""

def constraint_fragment(level: int) -> str:
    if level == 0:
        return (
        "• CONSTRAINT PRESSURE: NONE\n"
        "  Keep it open-ended — no artificial resource, budget, or performance limits.\n"
        )
    elif level == 1:
        return (
        "• CONSTRAINT PRESSURE: ONE REAL CONSTRAINT\n"
        "  Add ONE realistic limiting condition that an engineer would ACTUALLY hit for this\n"
        "  specific skill/scenario — invent it from the real context, don't paste a generic phrase.\n"
        "  ROTATE the KIND of constraint; do NOT default to a deadline or 'it's 2 AM' time-pressure.\n"
        "  Pick whichever genuinely fits: a resource/memory/compute limit, a compatibility or\n"
        "  legacy constraint, a correctness/edge-case requirement, a scale target, a cost ceiling,\n"
        "  a 'has to keep working without X', or a compliance need. Use a time crunch only rarely.\n"
        "  Mention it the way a colleague would in passing, woven into the question — NOT bolted on\n"
        "  as 'the catch is…'. It should feel like a real situation, not a puzzle rule.\n"
        )
    elif level == 2:
        return (
        "• CONSTRAINT PRESSURE: TWO COMPETING PRESSURES\n"
        "  Put the candidate between TWO realistic pressures that genuinely trade off for this\n"
        "  scenario (speed vs. cost, accuracy vs. latency, ship-now vs. do-it-right, etc.).\n"
        "  Both must feel real to the situation, and the tension should force a judgment call.\n"
        "  Keep it conversational — do NOT use a 'Given [X] and [Y], how would you…' formula.\n"
        )
    elif level == 3:
        return (
        "• CONSTRAINT PRESSURE: MULTIPLE COMPOUNDING PRESSURES\n"
        "  Describe a realistic squeeze where three or more pressures stack at once (e.g. budget +\n"
        "  time + reliability/compliance). It should read like a real high-stakes moment they'd\n"
        "  recognize, with the constraints emerging from the scenario — not a checklist of limits.\n"
        )
    return ""

def ambiguity_fragment(level: int) -> str:
    if level == 0:
        return (
        "• AMBIGUITY: CLEAR REQUIREMENTS (Level 0)\n"
        "  Provide ALL specifications upfront. The question must contain:\n"
        "    - The exact context (what exists, what is new)\n"
        "    - The expected outcome\n"
        "    - All parameters needed to answer\n"
        "  Do NOT intentionally omit any detail.\n"
        )
    elif level == 1:
        return (
        "• AMBIGUITY: PARTIAL INFORMATION (Level 1)\n"
        "  ═══════════════════════════════════════════════════════════════\n"
        "  INTENTIONALLY OMIT ONE CRITICAL DETAIL FROM THE QUESTION.\n"
        "  PRESENT THE QUESTION AS IF IT IS COMPLETE.\n"
        "  ═══════════════════════════════════════════════════════════════\n"
        "\n"
        "  GOAL: Test whether the candidate recognizes missing info.\n"
        "\n"
        "  DIVERSITY RULE — rotate through these omission categories:\n"
        "    a) SCALE: Omit how many users/records/requests.\n"
        "    b) TIMING: Omit whether real-time or batch, or the deadline.\n"
        "    c) ACCURACY: Omit the target metric (F1, latency, error rate).\n"
        "    d) ENVIRONMENT: Omit cloud vs on-prem, team size, or existing infra.\n"
        "    e) DATA: Omit data format, volume, or labeling status.\n"
        "    f) CONSTRAINTS: Omit budget, hardware, or compliance needs.\n"
        "\n"
        "  HOW TO APPLY:\n"
        "    - Pick ONE omission that would change the approach significantly.\n"
        "    - Do NOT hint that something is missing.\n"
        "    - A strong candidate will ask or state assumptions.\n"
        )
    elif level == 2:
        return (
        "• AMBIGUITY: UNDERSPECIFIED PROBLEM (Level 2)\n"
        "  ═══════════════════════════════════════════════════════════════\n"
        "  THE QUESTION MUST SOUND LIKE A NON-TECHNICAL STAKEHOLDER.\n"
        "  OMIT ARCHITECTURE, TECH STACK, SCALE, AND METRICS.\n"
        "  ═══════════════════════════════════════════════════════════════\n"
        "\n"
        "  GOAL: Test the candidate's ability to STRUCTURE ambiguity.\n"
        "\n"
        "  DIVERSITY RULE — vary the stakeholder voice:\n"
        "    a) EXECUTIVE: 'Search is slow. Fix it.' (no metrics, no context)\n"
        "    b) PRODUCT: 'Users are churning after the recommendations update.' (no data)\n"
        "    c) SUPPORT: 'Customers keep complaining about wrong results.' (vague)\n"
        "    d) BUSINESS: 'We need AI. What should we build?' (no scope)\n"
        "\n"
        "  WHAT TO ACCEPT: Candidate defines the problem before solving it.\n"
        "  WHAT TO REJECT: Jumping to solutions without scoping.\n"
        )
    return ""

def scale_fragment(level: int) -> str:
    if level == 0:
        return (
        "• SCALE CONTEXT: SMALL SYSTEM (Level 0)\n"
        "  Assume single application, local environment, minimal traffic.\n"
        "  Do NOT mention scale numbers or large datasets.\n"
        )
    elif level == 1:
        return (
        "• SCALE CONTEXT: MODERATE SCALE (Level 1)\n"
        "  GOAL: Test awareness of basic scaling considerations.\n"
        "\n"
        "  DIVERSITY RULE — pick a scale reference that fits the SKILL:\n"
        "    Do NOT always use '10K daily users'. Choose from:\n"
        "      a) USERS: '~10K daily active users', '~50K monthly users'\n"
        "      b) DATASET: '~1M training samples', '~500K documents to index'\n"
        "      c) MODEL: 'model with 7B parameters', '~200 embeddings per request'\n"
        "      d) STORAGE: '~50GB of logs per day', '~10TB data lake'\n"
        "      e) API: '~5K API calls/hour', '~100 concurrent connections'\n"
        "      f) PIPELINE: '~10K records processed nightly', '~1K inference requests/minute'\n"
        "\n"
        "  ENFORCEMENT:\n"
        "    - A specific scale NUMBER must appear in the question text.\n"
        "    - Pick the scale type most natural for the skills being tested.\n"
        )
    elif level == 2:
        return (
        "• SCALE CONTEXT: HIGH CONCURRENCY (Level 2)\n"
        "  ═══════════════════════════════════════════════════════════════\n"
        "  YOUR QUESTION MUST STATE A SPECIFIC THROUGHPUT NUMBER.\n"
        "  ═══════════════════════════════════════════════════════════════\n"
        "\n"
        "  READY-TO-EMBED SCALE PHRASES (pick one and paste into your question):\n"
        "    • '...handling 50K API requests per minute...'\n"
        "    • '...processing 1M events per hour from IoT sensors...'\n"
        "    • '...serving 100K concurrent WebSocket connections...'\n"
        "    • '...indexing 500K documents per day for search...'\n"
        "    • '...running inference on 10K requests per second...'\n"
        "\n"
        "  ENFORCEMENT:\n"
        "    - A throughput number MUST be visible in the question text.\n"
        "    - 'High traffic' or 'at scale' are BANNED — use a NUMBER.\n"
        )
    elif level == 3:
        return (
        "• SCALE CONTEXT: DISTRIBUTED PRODUCTION (Level 3)\n"
        "  ═══════════════════════════════════════════════════════════════\n"
        "  YOUR QUESTION MUST STATE SPECIFIC PRODUCTION PARAMETERS.\n"
        "  ═══════════════════════════════════════════════════════════════\n"
        "\n"
        "  READY-TO-EMBED PRODUCTION PARAMETERS (pick 2+ and paste):\n"
        "    • '...500K QPS across 3 regions with 99.95% uptime SLA...'\n"
        "    • '...10TB daily data ingest, multi-region, 5-minute recovery...'\n"
        "    • '...1M daily users, 99.9% availability, sub-100ms P99...'\n"
        "    • '...serving from US-East and EU-West with GDPR, 200K QPS...'\n"
        "\n"
        "  ENFORCEMENT:\n"
        "    - At least 2 production parameters must appear as numbers.\n"
        "    - Include one real-world friction point (legacy, compliance, team).\n"
        )
    return ""

_ROLE_OVERLAYS = {
    "junior": (
        "\nROLE CALIBRATION (Junior):\n"
        "  ═══════════════════════════════════════════════════\n"
        "  THIS CANDIDATE IS JUNIOR. CALIBRATE YOUR QUESTION ACCORDINGLY.\n"
        "  ═══════════════════════════════════════════════════\n"
        "  • Expect foundational knowledge, not extensive professional experience.\n"
        "  • Accept textbook or training-based explanations — depth comes from clarity, not war stories.\n"
        "  • When asking trade-offs, compare specific TOOLS or APPROACHES, not high-level strategies.\n"
        "  • Do NOT ask about large-scale operations, multi-team coordination, or multi-year experience.\n"
        "  • Keep questions answerable by someone with 0-2 years of experience."
    ),
    "mid": (
        "\nROLE CALIBRATION (Mid-Level):\n"
        "  • Expect hands-on professional experience with concrete examples.\n"
        "  • Ask about real decisions they made and what they would change.\n"
        "  • Trade-offs should involve methods, strategies, or implementation approaches.\n"
        "  • Test ability to work independently on well-defined problems.\n"
        "  • Probe for self-awareness: 'What did you learn?' or 'What would you do differently?'"
    ),
    "senior": (
        "\nROLE CALIBRATION (Senior):\n"
        "  • Enforce strategic-level thinking in every question.\n"
        "  • Expect production trade-offs, operational concerns, and cross-team impact.\n"
        "  • Trade-offs should involve high-level decisions with organizational implications.\n"
        "  • Questions must probe decision-making under uncertainty.\n"
        "  • Do NOT accept surface-level answers — push for reasoning depth."
    ),
}

# ═══════════════════════════════════════════════════════════════════════════
# VECTOR COMPUTATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
# ROUND PRESSURE ENVELOPES
# ═══════════════════════════════════════════════════════════════════════════
# Maps (role, base_difficulty, round) to strictly enforced (min, max) bounds
# for each dimension: Abstraction (A), Constraint (C), Ambiguity (Amb), Scale (S)
# This replaces scalar escalations and locks the difficulty identity per round.

ROUND_PRESSURE_CAPS = {
    # ── JUNIOR ─────────────────────────────────────────────────────────────────
    ("junior", "easy", 1):   {"A": (1, 2), "C": (0, 0), "Amb": (0, 0), "S": (0, 0)},
    ("junior", "easy", 2):   {"A": (1, 2), "C": (0, 1), "Amb": (0, 0), "S": (0, 0)},
    ("junior", "easy", 3):   {"A": (2, 3), "C": (0, 1), "Amb": (0, 1), "S": (0, 0)},
    
    ("junior", "medium", 1): {"A": (2, 2), "C": (0, 1), "Amb": (0, 0), "S": (0, 0)},
    ("junior", "medium", 2): {"A": (2, 3), "C": (0, 1), "Amb": (0, 1), "S": (0, 0)},
    ("junior", "medium", 3): {"A": (2, 3), "C": (1, 1), "Amb": (0, 1), "S": (0, 1)},
    
    ("junior", "hard", 1):   {"A": (2, 3), "C": (0, 1), "Amb": (0, 0), "S": (0, 0)},
    ("junior", "hard", 2):   {"A": (2, 3), "C": (0, 1), "Amb": (0, 1), "S": (0, 1)},
    ("junior", "hard", 3):   {"A": (2, 3), "C": (1, 1), "Amb": (0, 1), "S": (0, 1)},

    # ── MID ────────────────────────────────────────────────────────────────────
    ("mid", "easy", 1):      {"A": (1, 3), "C": (0, 1), "Amb": (0, 0), "S": (0, 0)},
    ("mid", "easy", 2):      {"A": (2, 3), "C": (0, 1), "Amb": (0, 1), "S": (0, 1)},
    ("mid", "easy", 3):      {"A": (3, 4), "C": (1, 2), "Amb": (0, 1), "S": (0, 1)},

    ("mid", "medium", 1):    {"A": (2, 3), "C": (0, 1), "Amb": (0, 1), "S": (0, 1)},
    ("mid", "medium", 2):    {"A": (3, 4), "C": (1, 2), "Amb": (0, 1), "S": (0, 1)},
    ("mid", "medium", 3):    {"A": (3, 5), "C": (1, 2), "Amb": (1, 2), "S": (1, 2)},
    
    ("mid", "hard", 1):      {"A": (3, 4), "C": (1, 2), "Amb": (0, 1), "S": (0, 1)},
    ("mid", "hard", 2):      {"A": (4, 5), "C": (1, 2), "Amb": (1, 2), "S": (1, 2)},
    ("mid", "hard", 3):      {"A": (4, 5), "C": (2, 3), "Amb": (1, 2), "S": (1, 2)},

    # ── SENIOR ─────────────────────────────────────────────────────────────────
    ("senior", "easy", 1):   {"A": (2, 3), "C": (0, 1), "Amb": (0, 1), "S": (0, 1)},
    ("senior", "easy", 2):   {"A": (3, 4), "C": (1, 2), "Amb": (0, 1), "S": (1, 2)},
    ("senior", "easy", 3):   {"A": (4, 5), "C": (1, 2), "Amb": (1, 2), "S": (1, 2)},

    ("senior", "medium", 1): {"A": (3, 4), "C": (0, 1), "Amb": (0, 1), "S": (0, 1)},
    ("senior", "medium", 2): {"A": (4, 5), "C": (1, 2), "Amb": (1, 2), "S": (1, 2)},
    ("senior", "medium", 3): {"A": (5, 5), "C": (1, 2), "Amb": (1, 2), "S": (1, 2)},

    ("senior", "hard", 1):   {"A": (4, 5), "C": (1, 2), "Amb": (1, 2), "S": (1, 2)},
    ("senior", "hard", 2):   {"A": (5, 5), "C": (1, 2), "Amb": (2, 2), "S": (1, 2)},
    ("senior", "hard", 3):   {"A": (5, 5), "C": (2, 3), "Amb": (2, 2), "S": (2, 3)},
}

def get_round_caps(role: str, base_difficulty: str, round_number: int, total_rounds: int = 3) -> dict:
    """Retrieves the strict envelope caps for a given round identity.
    
    Maps current_round/total_rounds proportionally to canonical rounds 1-3:
      - 1-round interview:  R1 → canonical 3 (peak)
      - 2-round interview:  R1 → canonical 2, R2 → canonical 3
      - 3-round interview:  R1 → 1, R2 → 2, R3 → 3 (standard)
      - 4+ round interview: spreads across 1-3, last rounds clamp to 3
    """
    if total_rounds <= 0:
        total_rounds = 1
    
    # Proportional mapping: (current / total) * 3, clamped to [1, 3]
    canonical_round = max(1, min(3, round((round_number / total_rounds) * 3)))
    
    caps = ROUND_PRESSURE_CAPS.get((role, base_difficulty, canonical_round))
    if not caps:
        logger.warning(f"Missing caps for ({role}, {base_difficulty}, {canonical_round}), falling back to mid/medium.")
        caps = ROUND_PRESSURE_CAPS.get(("mid", "medium", canonical_round))
    
    logger.debug(
        f"🎯 Round caps: R{round_number}/{total_rounds} → canonical R{canonical_round} "
        f"({role}/{base_difficulty}) → {caps}"
    )
    return caps


def get_escalation_strategy(role: str, difficulty: str, round_number: int) -> str:
    """
    Phase 3C+: Determines the escalation order for mid-interview adaptation.
    
    Returns 'abstraction_first' or 'constraint_first':
    - abstraction_first: A → C → Amb → S  (grow conceptual depth before adding operational pressure)
    - constraint_first:  C → Amb → S → A  (add operational pressure before conceptual depth)
    """
    role = role.lower()
    difficulty = difficulty.lower()
    
    if role == "junior":
        # Juniors grow conceptual understanding before constraints
        if difficulty in ("easy", "medium"):
            return "abstraction_first"
        # Junior hard: R1 = abstraction_first, R2+ = constraint_first
        return "abstraction_first" if round_number == 1 else "constraint_first"
    
    if role == "mid":
        if difficulty in ("easy", "medium"):
            return "abstraction_first"
        return "constraint_first"
    
    # Senior: always constraint_first (operational pressure first)
    return "constraint_first"


def compute_difficulty_vector(
    base_difficulty: str = "medium",
    role_experience_required: str = "mid",
    round_number: int = 1,
    skill_baseline: dict = None,
    total_rounds: int = 3,
) -> DifficultyVector:
    """
    Computes the Cognitive Pressure Vector for a specific skill.
    Maps baseline complexity to abstraction depth, and adapts other 
    dimensions based on the candidate's historical confidence.
    """
    # 1. Session Initialization
    caps = get_round_caps(role_experience_required, base_difficulty, round_number, total_rounds)
    
    if not skill_baseline:
        # Static fallback without baseline: Start at the bottom of the envelope
        return DifficultyVector(
            abstraction_depth=caps["A"][0],
            constraint_pressure=caps["C"][0],
            ambiguity=caps["Amb"][0],
            scale_complexity=caps["S"][0]
        )
    
    # 2. Extract baseline data
    baseline_complexity = skill_baseline.get("baseline_complexity", 0.0)
    avg_confidence = skill_baseline.get("baseline_confidence", 0.0)
    
    # Initialize from baseline complexity (treating it as abstraction depth)
    abstraction_depth = round(baseline_complexity)
    
    # Session dimensions start at 0
    constraint_pressure = 0
    ambiguity = 0
    scale_complexity = 0
    
    # 3. Between-Round Aggressive Adaptation
    # Thresholds calibrated to GPT simulation distribution:
    #   P50=0.775, P75=0.843, P90=0.850, Max=0.900
    # Real humans will score BELOW GPT, so these thresholds
    # are set to fire within GPT's achievable range.
    #
    # Tier 1 (>= 0.65): Competent — add constraint pressure
    # Tier 2 (>= 0.75): Strong    — escalate abstraction + constraint
    # Tier 3 (>= 0.80): Elite     — add ambiguity + scale
    if avg_confidence >= 0.65:
        constraint_pressure += 1
    if avg_confidence >= 0.75:
        abstraction_depth += 1
        constraint_pressure += 1   # C can reach 2 at this tier
    if avg_confidence >= 0.80:
        ambiguity += 1
        scale_complexity += 1
    if avg_confidence < 0.30:
        abstraction_depth -= 1
        constraint_pressure = 0
        ambiguity = 0
        scale_complexity = 0
        
    # 4. Bounded Constraints (Round Pressure Envelopes)
    # The envelope strictly limits adaptation
    abstraction_depth = min(max(abstraction_depth, caps["A"][0]), caps["A"][1])
    constraint_pressure = min(max(constraint_pressure, caps["C"][0]), caps["C"][1])
    ambiguity = min(max(ambiguity, caps["Amb"][0]), caps["Amb"][1])
    scale_complexity = min(max(scale_complexity, caps["S"][0]), caps["S"][1])
    
    # 5. Structural Coupling: C3 implies A≥4
    # Production-incident pressure without architectural depth produces
    # incoherent questions. If C reaches 3, ensure A is at least 4.
    if constraint_pressure >= 3 and abstraction_depth < 4:
        abstraction_depth = 4
    
    # 6. Safety Cap (Cognitive Overload Protection)
    while (constraint_pressure + ambiguity + scale_complexity) > 4:
        if ambiguity > 0:
            ambiguity -= 1
        elif scale_complexity > 0:
            scale_complexity -= 1
        elif constraint_pressure > 0:
            constraint_pressure -= 1

    vector = DifficultyVector(abstraction_depth, constraint_pressure, ambiguity, scale_complexity)
    
    logger.info(f"⚡ Vector adaptation for skill [conf={avg_confidence:.2f}]: {vector}")
    return vector


def compile_conceptual_prompt(vector: DifficultyVector, role_experience_required: str = "mid") -> str:
    """Difficulty instructions for a PURE CONCEPTUAL / mechanism question.

    Scales the DEPTH of the concept by abstraction_depth — NOT by wrapping it in a
    scenario or bolting on constraints (those turn concept questions into scenarios).
    """
    a = vector.abstraction_depth
    role_lower = (role_experience_required or "mid").lower()
    if a <= 1:
        depth = ("DEFINITION level: ask what something is, or what it's for, in plain terms. "
                 "Answerable in 2-3 sentences.")
    elif a == 2:
        depth = ("MECHANISM level: ask HOW something works under the hood, or what actually happens "
                 "step by step when a specific thing occurs.")
    elif a == 3:
        depth = ("REASONING level: ask WHY it behaves the way it does, or the real difference between "
                 "two related concepts and when each applies — the trade-off at the idea level, no scenario.")
    else:
        depth = ("DEEP level: ask about the underlying mechanism, edge cases, failure modes, or the "
                 "mathematical/architectural basis of the concept — still as a direct question, no scenario.")
    role_note = {
        "junior": "Keep it foundational — no production/architecture framing.",
        "senior": "Expect real depth: internals, edge cases, and the 'why' behind the design.",
    }.get(role_lower, "Hands-on depth: how it works in practice and why.")
    return (
        "DIFFICULTY & DELIVERY (CONCEPTUAL QUESTION):\n"
        f"  • {depth}\n"
        f"  • {role_note}\n"
        "  • Ask it DIRECTLY, like one engineer asking another — 'How does X actually work?',\n"
        "    'Why does Y happen?', 'What's the real difference between A and B?'.\n"
        "  • Do NOT wrap it in a scenario ('imagine you're…', 'your team…') and do NOT add\n"
        "    artificial constraints/deadlines/scale numbers. Naming the concept directly is fine.\n"
        "  • ONE short question. Output ONLY the question."
    )


def compile_vector_prompt(
    vector: DifficultyVector,
    role_experience_required: str = "mid",
    concept_focus: str = None,
    concept_mode: str = None,
    conceptual: bool = False,
) -> str:
    """
    Assembles the difficulty fragments into a cohesive LLM prompt instruction.
    Uses primacy-recency sandwich: role context FIRST + LAST for maximum compliance.
    """
    assert isinstance(vector, DifficultyVector), "CRITICAL: Concept Layer must pass unmutated DifficultyVector."

    # Pure conceptual questions scale by DEPTH, not scenario/constraint scaffolding.
    if conceptual:
        return compile_conceptual_prompt(vector, role_experience_required)

    role_overlay = _ROLE_OVERLAYS.get(role_experience_required, _ROLE_OVERLAYS["mid"])
    role_upper = role_experience_required.upper()
    role_lower = role_experience_required.lower()
    
    # ── Select paradigm via code-level random.choice ──
    paradigm_pool = _ABSTRACTION_PARADIGMS.get(vector.abstraction_depth, [])
    selected_paradigm = random.choice(paradigm_pool) if paradigm_pool else None
    if selected_paradigm:
        logger.info(f"🎲 Paradigm selected for A={vector.abstraction_depth}: {selected_paradigm['name']}")
    
    abs_frag = abstraction_fragment(vector.abstraction_depth, paradigm=selected_paradigm)
    con_frag = constraint_fragment(vector.constraint_pressure)
    amb_frag = ambiguity_fragment(vector.ambiguity)
    scl_frag = scale_fragment(vector.scale_complexity)

    # ── CONCEPT STEERING BLOCK (Phase 4) ──
    concept_frag = ""
    if concept_focus and concept_mode:
        if concept_mode == "reinforce":
            concept_frag = (
                f"\nCONCEPT STEERING (Reinforce):\n"
                f"  Focus specifically on testing the candidate's understanding of '{concept_focus}'.\n"
                f"  Test fundamentals before expanding scope. Keep difficulty within current vector bounds."
            )
        elif concept_mode == "deepen":
            concept_frag = (
                f"\nCONCEPT STEERING (Deepen):\n"
                f"  Push the candidate deeper into the advanced mechanics of '{concept_focus}'.\n"
                f"  Explore trade-offs, edge cases, failure scenarios, and scaling concerns."
            )
        elif concept_mode == "explore":
            # Just a gentle push away from old concepts if provided
            concept_frag = (
                f"\nCONCEPT STEERING (Explore):\n"
                f"  Introduce a new sub-area of the skill not previously tested.\n"
                f"  Avoid repeating previous themes."
            )
    
    # ── PRIMACY BLOCK: Role identity comes FIRST ──
    if role_lower == "junior":
        primacy = (
            "═══════════════════════════════════════════════════════════\n"
            f"  CANDIDATE ROLE: {role_upper} (0-2 years experience)\n"
            "  ALL QUESTIONS MUST BE ANSWERABLE BY A JUNIOR ENGINEER.\n"
            "  Do NOT ask about system architecture, distributed systems,\n"
            "  infrastructure decisions, or production-scale operations.\n"
            "═══════════════════════════════════════════════════════════\n"
        )
    elif role_lower == "senior":
        primacy = (
            "═══════════════════════════════════════════════════════════\n"
            f"  CANDIDATE ROLE: {role_upper}\n"
            "  Questions MUST require architecture-level thinking,\n"
            "  production trade-offs, and operational awareness.\n"
            "═══════════════════════════════════════════════════════════\n"
        )
    else:
        primacy = (
            "═══════════════════════════════════════════════════════════\n"
            f"  CANDIDATE ROLE: {role_upper}\n"
            "  Questions should test hands-on implementation experience.\n"
            "═══════════════════════════════════════════════════════════\n"
        )
    
    # ── Natural-delivery guard (matters as much as the difficulty) ──
    output_guard = (
        "\nHOW TO ASK IT — natural delivery matters as much as the difficulty:\n"
        "  • Talk like a real senior interviewer in conversation, not a quiz engine.\n"
        "  • ONE focused question, SHORT — aim for 1-2 sentences (~35 words). Long, multi-clause\n"
        "    word-problems are the #1 thing that makes this sound robotic and templated.\n"
        "  • Any example phrasings in the guidance above are INSPIRATION for the STYLE only —\n"
        "    never reuse their wording, scenarios, or scaffolding (e.g. 'You're building X, your\n"
        "    teammate suggests Y, the catch is Z'). Invent fresh wording grounded in the candidate's\n"
        "    actual skill and what they just said.\n"
        "  • Vary how you open — don't start every question the same way.\n"
        "  • Output ONLY the question itself: no preamble, bullets, meta commentary, or multi-part format."
    )
    
    # ── ENFORCEMENT CHECKLIST with self-check ──
    checklist_items = []
    
    # Constraint self-check (natural, scenario-derived — not canned phrases)
    if vector.constraint_pressure == 0:
        checklist_items.append("□ CONSTRAINTS: None. Keep it open-ended — no limits or deadlines.")
    elif vector.constraint_pressure == 1:
        checklist_items.append(
            "□ CONSTRAINT (C=1): One realistic constraint, drawn from THIS scenario and woven in\n"
            "    naturally (not a bolted-on 'the catch is…'). If the question has no real limiting\n"
            "    condition at all, add one that genuinely fits."
        )
    elif vector.constraint_pressure == 2:
        checklist_items.append(
            "□ CONSTRAINT (C=2): Two realistic pressures that genuinely trade off, phrased\n"
            "    conversationally — no 'Given X and Y' formula."
        )
    elif vector.constraint_pressure >= 3:
        checklist_items.append(
            "□ CONSTRAINT (C=3): A realistic squeeze with three+ pressures stacking at once,\n"
            "    emerging from the scenario — not a checklist of limits."
        )
    
    # Scale enforcement with self-check
    if vector.scale_complexity == 0:
        checklist_items.append("□ SCALE: None needed. Small system, no scale numbers.")
    elif vector.scale_complexity >= 1:
        scale_type = {1: "scale number (users, records, dataset size)",
                      2: "throughput number (req/min, events/hour)",
                      3: "production parameters (QPS, SLA, multi-region)"}
        checklist_items.append(
            f"□ SCALE SELF-CHECK (S={vector.scale_complexity}): "
            f"Does your question contain a {scale_type.get(vector.scale_complexity, 'scale number')}?\n"
            "    If no specific number is visible in the question text, REWRITE before outputting."
        )
    
    # Ambiguity enforcement
    if vector.ambiguity == 0:
        checklist_items.append("□ AMBIGUITY: Provide ALL specs upfront. Do NOT omit details.")
    elif vector.ambiguity >= 1:
        checklist_items.append(
            "□ AMBIGUITY: Intentionally OMIT one critical detail "
            "(scale, timing, accuracy, environment). Present as complete."
        )
    
    # Abstraction enforcement (role-specific)
    if vector.abstraction_depth <= 2:
        if role_lower == "junior":
            checklist_items.append(
                f"□ ABSTRACTION (A={vector.abstraction_depth}, {role_upper}): "
                "Simple, focused question about concepts or reasoning. "
                "BANNED: high-level strategy, complex operational scenarios, advanced trade-offs."
            )
        else:
            checklist_items.append(
                f"□ ABSTRACTION (A={vector.abstraction_depth}): "
                "Keep the question focused on concepts or reasoning. No high-level strategy."
            )
    elif vector.abstraction_depth == 3:
        if role_lower == "junior":
            checklist_items.append(
                "□ ABSTRACTION (A=3, JUNIOR): Compare TWO specific TOOLS or APPROACHES.\n"
                "    Ask the candidate to choose between two concrete options relevant to the role.\n"
                "    BANNED: high-level strategy, organizational decisions, multi-team coordination.\n"
                "    If your question involves strategic decisions, REWRITE as a tool/approach comparison."
            )
        elif role_lower == "mid":
            checklist_items.append(
                "□ ABSTRACTION (A=3, MID): Compare TWO implementation strategies or methods.\n"
                "    Keep at hands-on level, not high-level organizational strategy."
            )
        else:
            checklist_items.append(
                "□ ABSTRACTION (A=3, SENIOR): Compare TWO strategic approaches.\n"
                "    Force an explicit choice with operational implications."
            )
    elif vector.abstraction_depth == 4:
        checklist_items.append(
            "□ ABSTRACTION (A=4, ARCHITECTURE): Ensure the question uses ONE of the allowed architectural paradigms.\n"
            "    Verify: Is this forcing a structural decision, a system design, or a migration strategy? If no, REWRITE."
        )
    elif vector.abstraction_depth >= 5:
        checklist_items.append(
            "□ ABSTRACTION (A=5, RESILIENCE): Ensure the question uses ONE of the allowed crisis/resilience paradigms.\n"
            "    Verify: Is this testing deep debugging, cascading failure triage, or chaos engineering? If no, REWRITE."
        )
    
    checklist_text = "\n".join(f"  {item}" for item in checklist_items)
    
    # ── RECENCY BLOCK: Role reminder as the VERY LAST thing the LLM reads ──
    if role_lower == "junior":
        recency = (
            "\n\n⚠️ FINAL REMINDER: This is a JUNIOR candidate (0-2 years).\n"
            "  Your question MUST be answerable with foundational knowledge.\n"
            "  If your question involves advanced strategy, complex operations,\n"
            "  or senior-level decision-making — SIMPLIFY IT NOW before outputting."
        )
    elif role_lower == "senior":
        recency = (
            "\n\n⚠️ FINAL REMINDER: This is a SENIOR candidate.\n"
            "  Ensure your question demands strategic-level thinking\n"
            "  and professional operational awareness."
        )
    else:
        recency = (
            "\n\n⚠️ FINAL REMINDER: This is a MID-LEVEL candidate.\n"
            "  Ensure hands-on practical focus, not pure theory or high-level strategy."
        )
    
    enforcement = (
        "\n\n═══════════════════════════════════════════════════════════\n"
        "MANDATORY CHECKLIST — VERIFY BEFORE OUTPUTTING:\n"
        "═══════════════════════════════════════════════════════════\n"
        f"{checklist_text}\n"
        "\nIf ANY item is violated, REWRITE your question before outputting."
        f"{recency}"
    )
    
    # FINAL PROMPT: primacy → fragments → concept steering → role overlay → output guard → enforcement + recency
    prompt = (
        f"{primacy}\n"
        "DIFFICULTY & COGNITIVE PRESSURE:\n"
        f"{abs_frag}\n"
        f"{con_frag}\n"
        f"{amb_frag}\n"
        f"{scl_frag}\n"
        f"{concept_frag}\n"
        f"{role_overlay}\n"
        f"{output_guard}"
        f"{enforcement}"
    )
    
    logger.debug(
        f"🧩 COMPILED PROMPT ORDER VERIFIED: "
        f"[Vector Fragments (A,C,Amb,S) first] -> [Concept '{concept_focus}' ({concept_mode}) appended AFTER] -> [Role Overlay]"
    )
    
    return prompt


# ═══════════════════════════════════════════════════════════════════════════
# FOLLOW-UP PARADIGM POOLS — For the follow-up planner (separate from main)
# ═══════════════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════════════════════════
#  EMBEDDED (verbatim from the AIRA engine responses_client.py) — PLANNER SYSTEM PROMPT
#  The exact interviewer persona + rules the live engine uses to write a question.
# ═════════════════════════════════════════════════════════════════════════════════════
PLANNER_SYSTEM_PROMPT = """You are a senior technical interviewer. You've done hundreds of interviews.
You don't read from a script. You have a conversation.

Your job: generate ONE question that tests specific skills. That's it.

HOW YOU SOUND:
- Like a real person sitting across a table, genuinely curious about their work
- You vary your energy — sometimes you're casual ("So let's say..."), sometimes direct ("Walk me through...")
- You paint vivid, specific situations — not abstract concepts
- You make the candidate THINK, not recite definitions

THE GOLDEN RULE:
Your question must be impossible to answer without knowing the required skills.
If a random person off the street could attempt an answer — your question is garbage. Rewrite.

WHAT MAKES A GREAT INTERVIEW QUESTION:
1. It grounds the skill in a REAL situation — a design choice, a tradeoff, something they'd actually hit at work
2. It usually asks for a DECISION or a TRADEOFF — BUT a direct conceptual/mechanism question is
   also great when the format calls for it ("How does X actually work?", "Why does Y happen?").
   A real interviewer mixes both. What's banned is VAGUE recitation ("tell me about your experience with X").
3. It sounds different from every question you've asked before — different opener, different structure, different energy
4. It tests the skill without NAMING the skill — embed it in the scenario (except for pure concept questions,
   where naming the concept directly is fine and expected)

WHEN THE FORMAT SAYS CONCEPTUAL / DIRECT CONCEPT QUESTION:
Honor it. Ask a crisp theory/mechanism question directly — "What actually happens when…", "Explain how…
works", "What's the difference between… and…". Do NOT wrap it in a scenario or bolt on artificial
constraints. Scale the DEPTH to the difficulty (a junior gets a definition; a senior gets the mechanism,
edge cases, and the 'why'), not the amount of scenario scaffolding.

NOT EVERY QUESTION IS A CRISIS — AND VARY THE PRESSURE:
Match the intensity to the moment. Fundamentals and simpler skills deserve simple, direct,
curious questions — "How do you usually handle X?", "What's actually happening when Y occurs?".
Do NOT lean on the same device every time. The "it's 2 AM, the demo is in 15 minutes, production
is on fire" time-pressure framing is ONE option — and if you've used it recently, DON'T use it
again. Rotate the kind of pressure: a resource limit, a compatibility/legacy constraint, a
correctness/edge-case requirement, a scale target, a cost ceiling, a team/process disagreement,
or just a clean open design question with NO crisis at all. A wall of urgent incidents back-to-back
is just another template.

KEEP IT SHORT:
One or two sentences. If you're writing a paragraph-long word problem with a "the catch is…"
twist, you've lost the thread — cut it to the single sharp question underneath.

EXAMPLE — SAME SKILL, THREE DIFFERENT VIBES:
Skill: Redis caching

Vibe 1 (Debugging): "Your API response times just spiked from 50ms to 3 seconds. You check CloudWatch and your Redis hit rate dropped from 95% to 12% overnight. Nothing deployed. What do you look at first?"

Vibe 2 (Design decision): "You're building a recommendation engine. Your data scientist wants results in under 100ms but the model inference takes 800ms. How would you architect the caching layer?"

Vibe 3 (Tradeoff): "Your Redis cluster is eating 40GB of memory and your CTO is asking why the AWS bill doubled. You can't just throw more memory at it. What's your move?"

NEVER sound like:
✗ "Can you explain your experience with Redis caching and how you would use it in a distributed system?"
✗ "How would you implement caching using Redis to improve application performance?"
✗ "In your previous experience, how have you used Redis for caching purposes?"

THESE ARE DEAD. They sound like ChatGPT wrote them. Don't do it.

HARD CONSTRAINTS:
- Test ONLY the skills listed in the user message. No substitutions.
- Audio-friendly — no code writing, drawing, or visual tasks.
- Output ONLY valid JSON: {"question_no": N, "next_question": "..."}
- ONE question. Not two. Not three. One.
- If intent is "repeat" → return the exact same question.
- If intent is "skip" → completely new topic, new angle.
"""


# ═════════════════════════════════════════════════════════════════════════════════════
#  EMBEDDED (verbatim from the AIRA engine cartesia_tts.py) — CARTESIA VOICE/FORMAT CONFIG
#  Same voice + raw pcm_s16le 24kHz the live bot publishes, so cached clips are drop-in.
# ═════════════════════════════════════════════════════════════════════════════════════
CARTESIA_API_KEY  = os.environ.get("CARTESIA_API_KEY", "")
CARTESIA_VOICE_ID = os.environ.get("CARTESIA_VOICE_ID", "b7d50908-b17c-442d-ad8d-810c63997ed9")
CARTESIA_MODEL    = os.environ.get("CARTESIA_MODEL", "sonic-2")
CARTESIA_SAMPLE_RATE = 24_000
# Speed of bot speech: "slowest" | "slow" | "normal" | "fast" | "fastest"
# Set CARTESIA_SPEED=normal to restore default pace.
CARTESIA_SPEED = os.environ.get("CARTESIA_SPEED", "slow")

_OUTPUT_FORMAT = {
    "container": "raw",
    "encoding":  "pcm_s16le",
    "sample_rate": CARTESIA_SAMPLE_RATE,
}

def _voice_spec():
    """Build voice spec with current speed setting."""
    spec = {"mode": "id", "id": CARTESIA_VOICE_ID}
    if CARTESIA_SPEED and CARTESIA_SPEED != "normal":
        spec["__experimental_controls"] = {"speed": CARTESIA_SPEED}
    return spec


# ── Public API ────────────────────────────────────────────────────────────────


QUESTION_GEN_MODEL = os.getenv("QUESTION_GEN_MODEL", "gpt-4o-mini")


# ═════════════════════════════════════════════════════════════════════════════════════
#  1) QUESTION GENERATION — same planner prompt + difficulty vector the live engine uses
# ═════════════════════════════════════════════════════════════════════════════════════
def generate_questions_for_skill(client, skill, role, role_level, difficulty, count):
    """Generate `count` distinct questions testing `skill` at the given role level + difficulty."""
    vector = compute_difficulty_vector(base_difficulty=difficulty,
                                       role_experience_required=role_level,
                                       round_number=1, total_rounds=1)
    questions = []
    for i in range(count):
        difficulty_instructions = compile_vector_prompt(
            vector, role_experience_required=role_level)
        avoid = ""
        if questions:
            joined = "\n".join(f"  - {q}" for q in questions)
            avoid = ("\n\nYou have ALREADY asked these — ask something clearly different "
                     f"(new angle, new framing):\n{joined}")
        user_msg = (
            f"ROLE CONTEXT: {role} ({role_level}-level).\n"
            f"SKILL TO TEST: {skill}\n\n"
            f"{difficulty_instructions}{avoid}\n\n"
            'Generate ONE question. Output ONLY JSON: {"question_no": %d, "next_question": "..."}'
            % (i + 1)
        )
        resp = client.chat.completions.create(
            model=QUESTION_GEN_MODEL,
            messages=[{"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                      {"role": "user", "content": user_msg}],
            response_format={"type": "json_object"},
            temperature=0.7,
        )
        text = json.loads(resp.choices[0].message.content).get("next_question", "").strip()
        if text:
            questions.append(text)
    return questions


# ═════════════════════════════════════════════════════════════════════════════════════
#  2) TEXT -> SPEECH — same Cartesia voice/model/format as the live bot -> raw PCM bytes
# ═════════════════════════════════════════════════════════════════════════════════════
async def synthesize_pcm(cartesia_client, text):
    """Render one line to raw PCM (pcm_s16le, 24kHz, mono) via the Cartesia websocket."""
    voice = _voice_spec()
    pcm = bytearray()
    async with cartesia_client.tts.websocket_connect() as ws:
        ctx = ws.context(context_id=str(uuid.uuid4()), model_id=CARTESIA_MODEL,
                         voice=voice, output_format=_OUTPUT_FORMAT)
        await ctx.send(model_id=CARTESIA_MODEL, transcript=text, voice=voice,
                       output_format=_OUTPUT_FORMAT, continue_=False)
        async for event in ctx.receive():
            if event.type == "done":
                break
            if event.type == "error":
                raise RuntimeError(f"cartesia error: {event}")
            raw = getattr(event, "data", None)
            if raw:
                pcm.extend(base64.b64decode(raw))
    return bytes(pcm)


# ═════════════════════════════════════════════════════════════════════════════════════
#  3) ORCHESTRATION — build the bank (questions + base64 audio) and write JSON
# ═════════════════════════════════════════════════════════════════════════════════════
async def build_bank(skills, role, role_level, difficulty, count):
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("❌ OPENAI_API_KEY not set")
    if not CARTESIA_API_KEY:
        raise SystemExit("❌ CARTESIA_API_KEY not set")
    from openai import OpenAI
    from cartesia import AsyncCartesia
    openai_client = OpenAI()
    cartesia_client = AsyncCartesia(api_key=CARTESIA_API_KEY.split(",")[0].strip())

    vector = compute_difficulty_vector(difficulty, role_level, 1, total_rounds=1)
    out_questions = []
    try:
        for skill in skills:
            print(f"🧠 generating {count} question(s) for '{skill}' [{role_level}/{difficulty}]…")
            for n, qtext in enumerate(generate_questions_for_skill(
                    openai_client, skill, role, role_level, difficulty, count), 1):
                pcm = await synthesize_pcm(cartesia_client, qtext)
                dur_ms = int(len(pcm) / (CARTESIA_SAMPLE_RATE * 2) * 1000)
                out_questions.append({
                    "skill": skill, "question_no": n, "question_text": qtext,
                    "duration_ms": dur_ms, "audio_bytes": len(pcm),
                    "audio_base64": base64.b64encode(pcm).decode("ascii"),
                })
                print(f"   ✓ [{skill}] Q{n} {dur_ms:>5}ms  {qtext[:70]}…")
    finally:
        close = getattr(cartesia_client, "close", None)
        if close:
            try:
                await close()
            except Exception:
                pass
    return {
        "meta": {
            "role": role, "role_level": role_level, "difficulty": difficulty,
            "question_gen_model": QUESTION_GEN_MODEL,
            "difficulty_vector": {"A": vector.abstraction_depth, "C": vector.constraint_pressure,
                                  "Amb": vector.ambiguity, "S": vector.scale_complexity},
            "audio": {"encoding": "pcm_s16le", "sample_rate": CARTESIA_SAMPLE_RATE,
                      "container": "raw", "channels": 1},
            "voice_id": CARTESIA_VOICE_ID, "cartesia_model": CARTESIA_MODEL,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "questions": out_questions,
    }


def pcm_to_wav_bytes(pcm, sample_rate=CARTESIA_SAMPLE_RATE):
    """Wrap raw pcm_s16le mono bytes in a WAV container (for saving/inspection)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description="Pre-generate JD question bank with base64 TTS audio.")
    ap.add_argument("--skills", required=True, help="Comma-separated mandatory skills.")
    ap.add_argument("--role", default="Software Engineer", help="Job title / role (context).")
    ap.add_argument("--role-level", default="mid", choices=["junior", "mid", "senior"])
    ap.add_argument("--difficulty", default="medium", choices=["easy", "medium", "hard"])
    ap.add_argument("--count", type=int, default=3, help="Questions to pre-generate per skill.")
    ap.add_argument("--out", default=None, help="Output JSON path.")
    args = ap.parse_args()
    skills = [s.strip() for s in args.skills.split(",") if s.strip()]
    bank = asyncio.run(build_bank(skills, args.role, args.role_level, args.difficulty, args.count))
    out = args.out or f"question_bank_{args.role.replace(' ', '_')}_{args.role_level}_{args.difficulty}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(bank, f, indent=2, ensure_ascii=False)
    q = bank["questions"]
    print(f"\n✅ wrote {len(q)} questions ({sum(x['audio_bytes'] for x in q)/1024:.0f} KB raw audio) -> {out}")
    print("   backend: key by (role_level, difficulty, skill); serve audio_base64 as raw pcm_s16le 24kHz.")


if __name__ == "__main__":
    main()
