-- ===============================
-- ✅ AIRA Final Schema - MariaDB
-- ===============================
-- === Candidates ===
CREATE TABLE candidates (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    candidate_jd_id VARCHAR(50),
    candidate_name VARCHAR(255),
    candidate_phone VARCHAR(50),
    candidate_email VARCHAR(255) UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- === Recruiters ===
CREATE TABLE recruiters (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    recruiter_jd_id bigint DEFAULT NULL,
    recruiter_name varchar(200) DEFAULT NULL,
    recruiter_email varchar(200) DEFAULT NULL,
    is_active tinyint(1) DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- === Jobs ===
CREATE TABLE jobs (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    job_jd_ref_number VARCHAR(50) UNIQUE,
    job_jd_id int UNIQUE,
    job_title VARCHAR(255),
    job_description LONGTEXT,
    company_name VARCHAR(255),
    company_id int,
    job_status VARCHAR(100) DEFAULT 'Created',
    -- ENUM(
    --     'Created',
    --     'Open',
    --     'On Hold',
    --     'Closed',
    --     'Filled',
    --     'Cancelled'
    -- ) DEFAULT 'Created',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- === Interviews ===
CREATE TABLE interviews (
    id bigint(20) NOT NULL AUTO_INCREMENT,
    candidate_id bigint(20) DEFAULT NULL,
    job_id bigint(20) DEFAULT NULL,
    recruiter_name varchar(255) DEFAULT NULL,
    recruiter_email varchar(255) DEFAULT NULL,
    resume_text longtext DEFAULT NULL,
    resume_job_analysis longtext DEFAULT NULL,
    resume_match_score float NOT NULL,
    interview_time datetime DEFAULT NULL,
    interview_timezone varchar(255) DEFAULT NULL,
    interview_questions longtext DEFAULT NULL,
    interview_score float DEFAULT NULL,
    interview_analysis longtext DEFAULT NULL,
    interview_verdict varchar(50) DEFAULT NULL,
    interview_status varchar(200) DEFAULT 'Not Started',
    is_scheduled tinyint(1) DEFAULT 0,
    is_email_sent tinyint(1) DEFAULT 0,
    is_jd_interview_updated tinyint(1) DEFAULT NULL,
    is_jd_interview_result_updates tinyint(1) DEFAULT NULL,
    created_at timestamp NULL DEFAULT current_timestamp(),
    updated_at timestamp NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
    zoom_host_url text DEFAULT NULL,
    zoom_join_url text DEFAULT NULL,
    is_reminder_email_sent tinyint(1) DEFAULT 0,
    recruiter_id tinyint(1) DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY interview_uniq (candidate_id, job_id),
    KEY job_id (job_id),
    CONSTRAINT interviews_ibfk_1 FOREIGN KEY (candidate_id) REFERENCES candidates (id),
    CONSTRAINT interviews_ibfk_2 FOREIGN KEY (job_id) REFERENCES jobs (id)
) ENGINE = InnoDB AUTO_INCREMENT = 24 DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci;
-- === Interview - qa - analysis===
CREATE TABLE interviewqa (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    interview_id bigint DEFAULT NULL,
    question text DEFAULT NULL,
    answer text DEFAULT NULL,
    analysis text DEFAULT NULL,
    score FLOAT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT interviews_idfk_1 FOREIGN KEY (interview_id) REFERENCES interviews (id)
);
-- Create the interviews_log table to store historical data
CREATE TABLE interviews_log (
    log_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    action_type VARCHAR(50) NOT NULL,
    action_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    id BIGINT,
    candidate_id BIGINT,
    job_id BIGINT,
    recruiter_name VARCHAR(255),
    recruiter_email VARCHAR(255),
    resume_text LONGTEXT,
    resume_job_analysis LONGTEXT,
    resume_match_score FLOAT,
    interview_time DATETIME,
    interview_timezone VARCHAR(255),
    interview_questions LONGTEXT,
    interview_score FLOAT,
    interview_analysis LONGTEXT,
    interview_verdict VARCHAR(50),
    interview_status VARCHAR(200),
    --      ENUM(
    --     'Not Started',
    --     "Zoom Meeting Failed",
    --     "InProgress-Initiated",
    --     'InProgress-Waiting for participant',
    --     'InProgress-Participant joined',
    --     'InProgress-Participant not joined',
    --     'InProgress-Participant left',
    --     'InProgress-Recording',
    --     'Analyzing verdict',
    --     'Analyzing interview',
    --     'Follow-Up',
    --     'Cancelled',
    --     'Completed'
    
    is_scheduled BOOLEAN,
    is_email_sent BOOLEAN,
    is_jd_interview_updated BOOLEAN,
    is_jd_interview_result_updates BOOLEAN,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);
DROP TRIGGER if exists after_interviews_update;
DROP TRIGGER IF EXISTS after_interviews_update;
DROP TRIGGER IF EXISTS after_interviews_delete;

DELIMITER $$

CREATE TRIGGER after_interviews_update
AFTER UPDATE ON interviews
FOR EACH ROW
BEGIN
    INSERT INTO interviews_log (
        action_type,
        id,
        candidate_id,
        job_id,
        recruiter_name,
        recruiter_email,
        resume_text,
        resume_job_analysis,
        resume_match_score,
        interview_time,
        interview_timezone,
        interview_questions,
        interview_score,
        interview_analysis,
        interview_verdict,
        interview_status,
        is_scheduled,
        is_email_sent,
        is_jd_interview_updated,
        is_jd_interview_result_updates,
        created_at,
        updated_at
    )
    VALUES (
        'UPDATE',
        OLD.id,
        OLD.candidate_id,
        OLD.job_id,
        OLD.recruiter_name,
        OLD.recruiter_email,
        OLD.resume_text,
        OLD.resume_job_analysis,
        OLD.resume_match_score,
        OLD.interview_time,
        OLD.interview_timezone,
        OLD.interview_questions,
        OLD.interview_score,
        OLD.interview_analysis,
        OLD.interview_verdict,
        OLD.interview_status,
        OLD.is_scheduled,
        OLD.is_email_sent,
        OLD.is_jd_interview_updated,
        OLD.is_jd_interview_result_updates,
        OLD.created_at,
        OLD.updated_at
    );
END $$

CREATE TRIGGER after_interviews_delete
AFTER DELETE ON interviews
FOR EACH ROW
BEGIN
    INSERT INTO interviews_log (
        action_type,
        id,
        candidate_id,
        job_id,
        recruiter_name,
        recruiter_email,
        resume_text,
        resume_job_analysis,
        resume_match_score,
        interview_time,
        interview_timezone,
        interview_questions,
        interview_score,
        interview_analysis,
        interview_verdict,
        interview_status,
        is_scheduled,
        is_email_sent,
        is_jd_interview_updated,
        is_jd_interview_result_updates,
        created_at,
        updated_at
    )
    VALUES (
        'DELETE',
        OLD.id,
        OLD.candidate_id,
        OLD.job_id,
        OLD.recruiter_name,
        OLD.recruiter_email,
        OLD.resume_text,
        OLD.resume_job_analysis,
        OLD.resume_match_score,
        OLD.interview_time,
        OLD.interview_timezone,
        OLD.interview_questions,
        OLD.interview_score,
        OLD.interview_analysis,
        OLD.interview_verdict,
        OLD.interview_status,
        OLD.is_scheduled,
        OLD.is_email_sent,
        OLD.is_jd_interview_updated,
        OLD.is_jd_interview_result_updates,
        OLD.created_at,
        OLD.updated_at
    );
END $$

DELIMITER ;
