
# --- repo-root on sys.path (script lives in scripts/) ---
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from aira_3_webrtc_orchestration import get_db_connection

def main():
    conn, tunnel = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                # Let's find an interview
                cur.execute("SELECT id, interview_status FROM interviews ORDER BY id DESC LIMIT 1")
                row = cur.fetchone()
                if row:
                    cur.execute("UPDATE interviews SET interview_status='Not Started' WHERE id=%s", (row['id'],))
                    conn.commit()
                    print(f"Updated interview {row['id']} to 'Not Started'. Previous status was: {row['interview_status']}")
                else:
                    cur.execute("INSERT INTO interviews (candidate_name, candidate_email, job_id, candidate_id, interview_status, mandatory_skills) VALUES ('Test Cand', 'test@example.com', 1, 101, 'Not Started', 'Communication, Python, SQL')")
                    conn.commit()
                    print("Inserted a dummy interview with 'Not Started'")
    finally:
        if tunnel:
            tunnel.stop()

if __name__ == '__main__':
    main()
