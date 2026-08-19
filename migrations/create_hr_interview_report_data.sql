-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration: Create hr_interview_report_data table
-- Mirrors interview_report_data but for HR behavioral interviews.
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS hr_interview_report_data (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    interview_id    BIGINT NOT NULL UNIQUE,

    -- Overall score (0-5 scale)
    overall_score   FLOAT DEFAULT 0.0,
    summary         TEXT NULL,

    -- Verdict
    verdict_passed  BOOLEAN DEFAULT FALSE,
    verdict_status  VARCHAR(100) NULL,

    -- Dimension scorecard (JSON array)
    dimension_scores JSON NULL,

    -- Strengths & concerns (JSON arrays)
    strengths       JSON NULL,
    concerns        JSON NULL,

    -- Full transcript (JSON array)
    transcript      JSON NULL,

    -- Completion metadata
    topics_covered      BIGINT DEFAULT 0,
    topics_total        BIGINT DEFAULT 0,
    total_responses     BIGINT DEFAULT 0,
    explorations        BIGINT DEFAULT 0,
    duration_minutes    FLOAT DEFAULT 0.0,
    completion_status   VARCHAR(50) NULL,
    phase               BIGINT DEFAULT 1,

    -- Raw LLM evaluation JSON
    raw_evaluation  JSON NULL,

    -- Timestamps
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    -- Indexes
    INDEX idx_hrd_interview_id (interview_id),
    INDEX idx_hrd_verdict (verdict_passed),
    INDEX idx_hrd_score (overall_score),
    INDEX idx_hrd_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
