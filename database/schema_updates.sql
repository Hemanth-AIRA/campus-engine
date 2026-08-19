--  add unique constraint to recruiter_email in recruiters table
ALTER TABLE recruiters
MODIFY recruiter_email varchar(200) UNIQUE;
-- Update duplicate recruiter emails by appending a sequence number to each duplicate
CREATE TEMPORARY TABLE ranked_emails AS
SELECT id,
    recruiter_email,
    ROW_NUMBER() OVER (
        PARTITION BY recruiter_email
        ORDER BY id
    ) AS rn
FROM recruiters
WHERE recruiter_email IS NOT NULL;
UPDATE recruiters r
    JOIN ranked_emails re ON r.id = re.id
SET r.recruiter_email = CONCAT(r.recruiter_email, '_', re.rn - 1)
WHERE re.rn > 1;
DROP TEMPORARY TABLE ranked_emails;