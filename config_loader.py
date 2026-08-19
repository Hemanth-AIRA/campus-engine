import json
import os

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r") as f:
        return json.load(f)

CONFIG = load_config()

HARDCODED_EMAILS = CONFIG.get("HARDCODED_EMAILS", [])
INTERVIEW_PASS_THRESHOLD_PERCENTAGE = CONFIG.get("INTERVIEW_PASS_THRESHOLD_PERCENTAGE", 50)
SKILL_RATIO = CONFIG.get("SKILL_RATIO", {"mandatory": 4, "optional": 1})
NUMBER_OF_QUESTIONS = int(os.getenv("NUMBER_OF_QUESTIONS", 15))
MAX_INTERVIEW_DURATION_MINUTES = int(os.getenv("MAX_INTERVIEW_DURATION_MINUTES", 60))

def get_pass_threshold_percentage():
    """Helper function to get pass threshold percentage for templates"""
    return INTERVIEW_PASS_THRESHOLD_PERCENTAGE
#dummy push 3