import pymysql
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # Python 3.9+
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv
import os

# === Load environment variables ===
load_dotenv(override=True)
JD_TIMEZONE = os.getenv("JOBDIVA_TIMEZONE")  # Example: Asia/Kolkata or America/New_York

# === DB Configuration ===
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "password",
    "database": "aira_db",
    "cursorclass": pymysql.cursors.DictCursor
}

# === Email Configuration ===
SENDER_EMAIL = "kothasridhar@gmail.com"
SENDER_PASSWORD = "rych anux turp hbcd"  # Gmail App Password
FEEDBACK_FORM_LINK = "https://docs.google.com/forms/d/e/1FAIpQLSezA7WbO61a9EFtc6PzbWdrbgvFrChC0dUr8HMMiMDSHlnnOA/viewform?usp=dialog"

# === Send Feedback Email ===
def send_feedback_email(candidate_email, candidate_name):
    subject = "We'd love your feedback!"
    body = f"""Hi {candidate_name},

Thank you for attending the interview today. We appreciate your time and the insights you shared.

We’d love to hear your feedback to help us improve: {FEEDBACK_FORM_LINK}

Warm regards,  
AIRA Hiring Team
"""
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = SENDER_EMAIL
    msg['To'] = candidate_email

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
            print(f"✅ Email sent to {candidate_name} at {candidate_email}")
    except Exception as e:
        print(f"❌ FAILED to send to {candidate_email} — {e}")

# === Log Sent Email to File ===
def log_feedback_email(interview):
    with open("feedback_log.txt", "a") as log_file:
        log_file.write(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"Sent to: {interview['candidate_name']} "
            f"<{interview['candidate_email']}> | "
            f"Interview ID: {interview['id']} | "
            f"Time: {interview['interview_time']} | "
            f"Timezone: {interview['interview_timezone']}\n"
        )

# === Feedback Email Logic (JD_TIMEZONE aware, 45-minute delay) ===
def check_and_send_feedback():
    local_now = datetime.now(ZoneInfo(JD_TIMEZONE))
    feedback_cutoff = local_now - timedelta(minutes=45)
    feedback_window_start = feedback_cutoff.replace(second=0, microsecond=0)
    feedback_window_end = feedback_window_start + timedelta(minutes=1)

    print(f"\n📅 Current time in {JD_TIMEZONE}: {local_now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"⏱️ Sending feedback for interviews between: {feedback_window_start.strftime('%Y-%m-%d %H:%M:%S')} and {feedback_window_end.strftime('%Y-%m-%d %H:%M:%S')}")

    conn = pymysql.connect(**DB_CONFIG)
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT i.id, i.candidate_id, i.interview_time,
                   i.interview_status, i.interview_timezone,
                   c.candidate_name, c.candidate_email
            FROM interviews i
            JOIN candidates c ON i.candidate_id = c.id
            WHERE i.interview_status = 'Complete'
              AND i.interview_time >= %s
              AND i.interview_time < %s
        """, (feedback_window_start, feedback_window_end))

        interviews = cursor.fetchall()

        if not interviews:
            print("✅ No feedback emails to send.")
            return

        for interview in interviews:
            try:
                print(f"\n📨 Sending to {interview['candidate_name']} ({interview['candidate_email']})")
                send_feedback_email(interview['candidate_email'], interview['candidate_name'])
                log_feedback_email(interview)
                # No longer updating feedback_sent flag
            except Exception as e:
                print(f"❌ Error processing interview ID {interview['id']}: {e}")

        conn.commit()
        conn.close()

# === Run the Script ===
if __name__ == "__main__":
    check_and_send_feedback()
