-- ===============================
-- AIRA Mandatory Skills Migration
-- ===============================
-- Add mandatory skills fields to jobs and interviews tables
-- Check if columns exist before adding them
-- Add mandatory skills fields to jobs table (avoiding conflict with existing 'skills' column)
SET @sql = IF(
        (
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME = 'jobs'
                AND COLUMN_NAME = 'mandatory_skills'
        ) = 0,
        'ALTER TABLE jobs ADD COLUMN mandatory_skills LONGTEXT DEFAULT NULL',
        'SELECT "mandatory_skills column already exists in jobs table"'
    );
PREPARE stmt
FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
SET @sql = IF(
        (
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME = 'jobs'
                AND COLUMN_NAME = 'has_mandatory_skills'
        ) = 0,
        'ALTER TABLE jobs ADD COLUMN has_mandatory_skills TINYINT(1) DEFAULT 0',
        'SELECT "has_mandatory_skills column already exists in jobs table"'
    );
PREPARE stmt
FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
-- Add mandatory skills fields to interviews table
SET @sql = IF(
        (
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME = 'interviews'
                AND COLUMN_NAME = 'mandatory_skills'
        ) = 0,
        'ALTER TABLE interviews ADD COLUMN mandatory_skills LONGTEXT DEFAULT NULL',
        'SELECT "mandatory_skills column already exists in interviews table"'
    );
PREPARE stmt
FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
SET @sql = IF(
        (
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME = 'interviews'
                AND COLUMN_NAME = 'has_mandatory_skills'
        ) = 0,
        'ALTER TABLE interviews ADD COLUMN has_mandatory_skills TINYINT(1) DEFAULT 0',
        'SELECT "has_mandatory_skills column already exists in interviews table"'
    );
PREPARE stmt
FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
-- Update existing jobs to populate has_mandatory_skills based on job_description content
UPDATE jobs
SET has_mandatory_skills = 1
WHERE job_description REGEXP '(mandatory|required|must have|essential)\\s+(skills?|requirements?)';
-- Update existing interviews to populate has_mandatory_skills based on related job
UPDATE interviews i
    JOIN jobs j ON i.job_id = j.id
SET i.has_mandatory_skills = j.has_mandatory_skills,
    i.mandatory_skills = j.mandatory_skills;
-- Add indexes for better query performance (check if they exist first)
SET @sql = IF(
        (
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME = 'jobs'
                AND INDEX_NAME = 'idx_jobs_has_mandatory_skills'
        ) = 0,
        'CREATE INDEX idx_jobs_has_mandatory_skills ON jobs(has_mandatory_skills)',
        'SELECT "Index idx_jobs_has_mandatory_skills already exists"'
    );
PREPARE stmt
FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
SET @sql = IF(
        (
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME = 'interviews'
                AND INDEX_NAME = 'idx_interviews_has_mandatory_skills'
        ) = 0,
        'CREATE INDEX idx_interviews_has_mandatory_skills ON interviews(has_mandatory_skills)',
        'SELECT "Index idx_interviews_has_mandatory_skills already exists"'
    );
PREPARE stmt
FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
-- Verify the changes
SELECT 'jobs' as table_name,
    COUNT(*) as total_records,
    SUM(has_mandatory_skills) as records_with_mandatory_skills
FROM jobs
UNION ALL
SELECT 'interviews' as table_name,
    COUNT(*) as total_records,
    SUM(has_mandatory_skills) as records_with_mandatory_skills
FROM interviews;