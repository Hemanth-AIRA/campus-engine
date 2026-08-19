
# --- repo-root on sys.path (script lives in scripts/) ---
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import sys
import os

# Add Echo-1 to path to import integrations.MSEmailClient
sys.path.append(os.path.join(os.path.dirname(__file__), 'Echo-1'))

from integrations.MSEmailClient import convert_interview_html_to_pdf

def main():
    if os.path.exists('detailed_html.html'):
        with open('detailed_html.html', 'r', encoding='utf-8') as f:
            detailed_html = f.read()
        print("Converting detailed_html.html to PDF...")
        convert_interview_html_to_pdf(detailed_html, 'detailed_pdf.pdf', 'detailed')
    else:
        print("detailed_html.html not found.")

    if os.path.exists('summary_html.html'):
        with open('summary_html.html', 'r', encoding='utf-8') as f:
            summary_html = f.read()
        print("Converting summary_html.html to PDF...")
        convert_interview_html_to_pdf(summary_html, 'summary_pdf.pdf', 'summary')
    else:
        print("summary_html.html not found.")

if __name__ == "__main__":
    main()
