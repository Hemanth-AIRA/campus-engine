"""
Non-blocking integration for answer analysis system.
Triggers analysis asynchronously without blocking interview flow.
"""

import asyncio
import threading
import logging
from typing import Dict, Optional
from pathlib import Path


logger = logging.getLogger('AnswerAnalysisIntegration')


def trigger_answer_analysis_async(
    session_id: str,
    question_id: str,
    question_text: str,
    question_type: str,
    answer_text: str,
    answer_duration_sec: float,
    resume_text: Optional[str] = None,
    session_context: Optional[Dict] = None
) -> None:
    """
    Non-blocking trigger for answer analysis.
    Returns immediately while starting analysis in background thread.
    
    Args:
        session_id: Unique session identifier
        question_id: Question identifier
        question_text: The question asked
        question_type: Type of question (resume|troubleshooting|experimental|etc)
        answer_text: Candidate's answer
        answer_duration_sec: Time taken to answer
        resume_text: Optional resume text for grounding
        session_context: Full session history and prior analysis
    
    Returns:
        None - Analysis runs in background
    """
    
    # Prepare analysis input
    analysis_input = {
        "session_id": session_id,
        "question_id": question_id,
        "question_text": question_text,
        "question_type": question_type,
        "answer_text": answer_text,
        "answer_duration_sec": answer_duration_sec,
        "resume_text": resume_text,
        "session_context": session_context or {}
    }
    
    # Start analysis in background thread
    thread = threading.Thread(
        target=_run_analysis_in_thread,
        args=(analysis_input,),
        daemon=True,
        name=f"AnswerAnalysis-{session_id}-{question_id}"
    )
    thread.start()
    
    logger.info(
        f"✓ Analysis triggered for session={session_id}, question={question_id} "
        f"[Background thread started]"
    )


def _run_analysis_in_thread(analysis_input: Dict) -> None:
    """
    Run async analysis in dedicated thread.
    This function bridges threading and asyncio.
    """
    try:
        # Create new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Run async analysis
        loop.run_until_complete(_execute_analysis(analysis_input))
        
    except Exception as e:
        logger.error(
            f"Analysis failed for session={analysis_input['session_id']}, "
            f"question={analysis_input['question_id']}: {e}",
            exc_info=True
        )
    finally:
        loop.close()


async def _execute_analysis(analysis_input: Dict) -> None:
    """
    Execute the actual analysis pipeline.
    Imports are done here to avoid circular dependencies.
    """
    try:
        # Import analysis modules (lazy import to avoid blocking main thread)
        from answer_analysis.analyze_answer import analyze_answer
        
        # Run analysis
        result = await analyze_answer(analysis_input)
        
        logger.info(
            f"✓ Analysis complete: session={analysis_input['session_id']}, "
            f"confidence={result.get('authorship_confidence')}, "
            f"delta={result.get('confidence_delta')}"
        )
        
    except ImportError as e:
        logger.error(f"Analysis modules not found: {e}. Ensure answer_analysis/ exists.")
    except Exception as e:
        logger.error(f"Analysis execution failed: {e}", exc_info=True)


# ============================================================================
# INTEGRATION HELPER - Map question format to type
# ============================================================================

def map_question_format_to_type(format_enum) -> str:
    """
    Map QuestionFormat enum to question_type string expected by analysis.
    
    Args:
        format_enum: QuestionFormat enum value
    
    Returns:
        question_type string (resume|troubleshooting|experimental|etc)
    """
    from core.responses_client import QuestionFormat
    
    mapping = {
        QuestionFormat.RESUME_PROJECT: "resume",
        QuestionFormat.TROUBLESHOOTING: "troubleshooting",
        QuestionFormat.EXPERIMENTAL: "experimental",
        QuestionFormat.COMPARISON: "comparison",
        QuestionFormat.IMPLEMENTATION_WALKTHROUGH: "implementation",
        QuestionFormat.VERBAL_ARCHITECTURE: "architecture",
        QuestionFormat.DIRECT_EXPERIENCE: "resume",  # Treat as resume-style
        QuestionFormat.SCENARIO_BASED: "implementation",  # Treat as implementation-style
    }
    
    return mapping.get(format_enum, "implementation")


# ============================================================================
# USAGE EXAMPLE - Call from interview orchestrator
# ============================================================================

"""
USAGE IN INTERVIEW ORCHESTRATOR:

from answer_analysis_integration import (
    trigger_answer_analysis_async,
    map_question_format_to_type
)

# After candidate answers (in your orchestrator):
def process_candidate_answer(
    session_id: str,
    question_number: int,
    question_text: str,
    question_format: QuestionFormat,  # Your format enum
    answer_text: str,
    answer_start_time: float,
    answer_end_time: float,
    resume_text: str,
    session_state: dict
):
    # Calculate duration
    duration_sec = answer_end_time - answer_start_time
    
    # Map format to type
    question_type = map_question_format_to_type(question_format)
    
    # Build session context (your existing session data)
    session_context = {
        "questions": session_state.get("all_questions", []),
        "answers": session_state.get("all_answers", []),
        "timings": session_state.get("all_timings", []),
        "prior_analysis": session_state.get("prior_analysis", [])
    }
    
    # Trigger analysis (non-blocking - returns immediately!)
    trigger_answer_analysis_async(
        session_id=session_id,
        question_id=f"Q{question_number}",
        question_text=question_text,
        question_type=question_type,
        answer_text=answer_text,
        answer_duration_sec=duration_sec,
        resume_text=resume_text,
        session_context=session_context
    )
    
    # Continue interview immediately - analysis runs in background
    # No waiting, no blocking!
    
    return "continue"  # Or whatever your flow needs

# Analysis results are stored in: sessions/session_<session_id>.json
# You can read them later for reporting/review
"""