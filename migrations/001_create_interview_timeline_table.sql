-- Migration: Create interview_timeline table for storing complete conversation history
-- Date: 2025-10-26
-- Purpose: Store all AIRA-candidate conversations chronologically for future report regeneration

CREATE TABLE IF NOT EXISTS `interview_timeline` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `interview_id` BIGINT NOT NULL,
  `timestamp` DATETIME NOT NULL,
  `conversation_type` VARCHAR(50) NOT NULL COMMENT 'Type: setup, technical_qa, warning, repeat, clarification, exit',
  `aira_said` TEXT COMMENT 'What AIRA said in this exchange',
  `candidate_said` TEXT COMMENT 'What candidate said in this exchange',
  `question_index` BIGINT NULL COMMENT 'Related question number (null for pre-interview)',
  `flag` BOOLEAN DEFAULT FALSE COMMENT 'Flagged/skipped question indicator',
  `sequence_number` BIGINT NOT NULL COMMENT 'Order of conversation in interview',
  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  INDEX `idx_interview_id` (`interview_id`),
  INDEX `idx_timestamp` (`timestamp`),
  INDEX `idx_sequence` (`interview_id`, `sequence_number`),
  INDEX `idx_conversation_type` (`conversation_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Complete conversation timeline for interviews';

-- Add foreign key constraint (optional - if interviews table exists)
-- ALTER TABLE `interview_timeline` 
-- ADD CONSTRAINT `fk_timeline_interview` 
-- FOREIGN KEY (`interview_id`) REFERENCES `interviews`(`id`) 
-- ON DELETE CASCADE;
