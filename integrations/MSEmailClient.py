import requests
import msal
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
import base64
import logging
from datetime import datetime
import time

# Optional pdfkit import - not currently used but kept for compatibility
try:
    import pdfkit
    PDFKIT_AVAILABLE = True
except ImportError:
    PDFKIT_AVAILABLE = False
    pdfkit = None

import tempfile
import os
import re

# Optional face extractor import
try:
    import report_face_extractor as face_extractor
    FACE_EXTRACTOR_AVAILABLE = True
except ImportError:
    FACE_EXTRACTOR_AVAILABLE = False
    face_extractor = None
from config_loader import HARDCODED_EMAILS, INTERVIEW_PASS_THRESHOLD_PERCENTAGE
from jinja2 import Environment, FileSystemLoader, select_autoescape

from interview_template_handler import InterviewReportTemplate, sanitize_filename
from logger_config import setup_logging
import asyncio
import subprocess
import sys

from pathlib import Path
setup_logging()
logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])


async def convert_html_to_pdf_browser(html_content, output_pdf_path, candidate_name="",process_id=0,room_id=""):
    """Convert HTML to PDF using browser-based conversion for perfect layout preservation"""

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.info(f"[process id: {process_id}][room id: {room_id}] 📦 Installing Playwright...")
        subprocess.run([sys.executable, '-m', 'pip', 'install', 'playwright'], check=True)
        subprocess.run([sys.executable, '-m', 'playwright', 'install', 'chromium'], check=True)
        from playwright.async_api import async_playwright

    # Add print-specific optimizations to the HTML
    print_css = """
    <style>
    /* Force desktop layout for PDF - prevent mobile responsive behavior */
    body {
        min-width: 1200px !important;
        width: 100% !important;
        max-width: none !important;
    }
   
    .wrap {
        max-width: 1100px !important;
        width: 100% !important;
        margin: 20px auto !important;
    }
   
    /* Universal color preservation - CRITICAL for PDF */
    *, *:before, *:after {
        -webkit-print-color-adjust: exact !important;
        color-adjust: exact !important;
        print-color-adjust: exact !important;
    }
   
    /* Force timeline elements to preserve colors */
    .timeline-item, .event-image, .event-timestamp, .timeline::before {
        -webkit-print-color-adjust: exact !important;
        color-adjust: exact !important;
        print-color-adjust: exact !important;
    }
 
    @media print {
        /* Optimized layout for A4 PDF pages */
        body {
            min-width: 900px !important;
            width: 100% !important;
            max-width: 100% !important;
            overflow-x: hidden !important;
        }
       
        .wrap {
            max-width: 900px !important;
            width: 95% !important;
            margin: 15px auto !important;
            padding: 15px 20px 15px 35px !important;
            box-sizing: border-box !important;
        }
       
        /* Add extra left spacing to section headers and content */
        .section h2, .section-title {
            padding-left: 10px !important;
        }
       
        .card, .q-card {
            margin-left: 5px !important;
            padding: 16px 20px 16px 25px !important;
        }
       
        /* Ensure all text content has proper left spacing */
        .detail-item, .score-big, .score-sub, .rec {
            padding-left: 8px !important;
        }
       
        /* Override responsive layouts */
        .col-2, .col-3, .col-wide {
            min-width: 250px !important;
            max-width: none !important;
            flex: 1 !important;
        }
       
        .flex-row {
            flex-direction: row !important;
            display: flex !important;
            flex-wrap: wrap !important;
        }
       
        /* Force all inline styles to be preserved */
        [style] {
            -webkit-print-color-adjust: exact !important;
            color-adjust: exact !important;
            print-color-adjust: exact !important;
        }
       
        .metric-fill {
            -webkit-print-color-adjust: exact !important;
            color-adjust: exact !important;
            print-color-adjust: exact !important;
        }
       
        .card, .q-card {
            page-break-inside: avoid !important;
            break-inside: avoid !important;
            padding: 16px 20px 16px 25px !important;
            margin-left: 5px !important;
        }
       
        .section {
            page-break-inside: avoid !important;
            break-inside: avoid !important;
        }
       
        /* Fix flexbox for PDF and prevent overflow */
        .flex-row {
            display: flex !important;
            flex-wrap: wrap !important;
            overflow: hidden !important;
        }
       
        .col-2, .col-3, .col-wide {
            flex: 1 !important;
            min-width: 220px !important;
            max-width: calc(50% - 10px) !important;
            overflow: hidden !important;
        }
       
        /* Only apply word wrapping to specific content, not metric values */
        p, .rec, .detail-item {
            word-wrap: break-word !important;
            overflow-wrap: break-word !important;
        }
       
        /* Prevent metric values from breaking */
        .metric-value, .metric-score, .score-big, .metric-row {
            white-space: nowrap !important;
            word-wrap: normal !important;
            overflow-wrap: normal !important;
        }
       
        /* Ensure metric row layout stays intact */
        .metric-row {
            display: flex !important;
            align-items: center !important;
            gap: 12px !important;
            white-space: nowrap !important;
        }
       
        .metric-score {
            min-width: 40px !important;
            text-align: right !important;
            font-weight: 600 !important;
            white-space: nowrap !important;
            flex-shrink: 0 !important;
        }
       
        /* Ensure text content doesn't overflow */
        * {
            box-sizing: border-box !important;
        }
        /* Force timeline to respect spacing - reinforces template styles */
        .timeline-wrapper {
            padding-top: 20px !important;
            padding-bottom: 60px !important;
            margin-top: 0 !important;
            overflow: visible !important;
        }
       
        /* Ensure center line is visible */
        .timeline::before {
            background: rgba(243,156,18,0.25) !important;
            z-index: 1 !important;
        }
    }
   
    @page {
        size: A4;
        margin: 0.5in 0.4in 0.5in 0.5in;
    }
    </style>
    """

    # Insert print CSS before closing head tag
    if '</head>' in html_content:
        html_content = html_content.replace('</head>', f'{print_css}</head>')

    # Create temporary HTML file
    temp_html = f'temp_interview_pdf_{candidate_name}.html'
    with open(temp_html, 'w', encoding='utf-8') as f:
        f.write(html_content)

    logger.info(f"[process id: {process_id}][room id: {room_id}] 🚀 Converting HTML to PDF for {candidate_name}...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # Set optimized viewport size for A4 PDF content
        await page.set_viewport_size({'width': 1000, 'height': 800})

        # Load the HTML file
        file_url = f"file://{os.path.abspath(temp_html)}"
        await page.goto(file_url, wait_until='networkidle', timeout=30000)

        await page.add_script_tag(url="https://twemoji.maxcdn.com/v/latest/twemoji.min.js")

        await page.evaluate("""
            try {
                if (typeof twemoji !== 'undefined') {
                // Use svg for best quality - set attributes to keep size consistent
                twemoji.parse(document.body, {
                    folder: 'svg',
                    ext: '.svg',
                    attributes: function(icon, variant) {
                    return {'width':'1em','height':'1em','style':'vertical-align:middle;'}; 
                    }
                });
                }
            } catch (e) {
                console.warn('twemoji.parse failed', e);
            }
            """)
        
        await page.wait_for_timeout(400)

        # Force color preservation with JavaScript
        await page.evaluate("""
            // Force all elements with inline background styles to preserve colors
            const elementsWithBg = document.querySelectorAll('[style*="background"]');
            elementsWithBg.forEach(el => {
                el.style.setProperty('-webkit-print-color-adjust', 'exact', 'important');
                el.style.setProperty('color-adjust', 'exact', 'important');
                el.style.setProperty('print-color-adjust', 'exact', 'important');
            });
           
            // Specifically target metric-fill elements
            const metricFills = document.querySelectorAll('.metric-fill');
            metricFills.forEach(el => {
                el.style.setProperty('-webkit-print-color-adjust', 'exact', 'important');
                el.style.setProperty('color-adjust', 'exact', 'important');
                el.style.setProperty('print-color-adjust', 'exact', 'important');
            });
           
            // CRITICAL: Force timeline elements to preserve colors and spacing
            const timelineElements = document.querySelectorAll('.timeline-item, .timeline::before, .event-image, .event-timestamp, .timeline-critical, .timeline-warning, .timeline-low');
            timelineElements.forEach(el => {
                el.style.setProperty('-webkit-print-color-adjust', 'exact', 'important');
                el.style.setProperty('color-adjust', 'exact', 'important');
                el.style.setProperty('print-color-adjust', 'exact', 'important');
            });
           
            // Force timeline wrapper spacing
            const timelineWrapper = document.querySelector('.timeline-wrapper');
            if (timelineWrapper) {
                timelineWrapper.style.setProperty('padding-bottom', '60px', 'important');
                timelineWrapper.style.setProperty('padding-top', '20px', 'important');
                timelineWrapper.style.setProperty('margin-top', '0', 'important');
            }
        """)

        # Wait for content to fully load
        await page.wait_for_timeout(3000)

        # Generate PDF with optimized settings
        await page.pdf(
            path=output_pdf_path,
            format='A4',
            margin={
                'top': '0.5in',
                'right': '0.4in',
                'bottom': '0.5in',
                'left': '0.5in'
            },
            print_background=True,
            prefer_css_page_size=False,
            scale=0.85
        )

        await browser.close()

    # Clean up temporary file
    try:
        os.remove(temp_html)
    except:
        pass

    if os.path.exists(output_pdf_path):
        logger.info(f"[process id: {process_id}][room id: {room_id}] ✅ PDF generated successfully: {output_pdf_path}")
        return output_pdf_path
    else:
        logger.error("❌ PDF file was not created")
        return None


def convert_interview_html_to_pdf(html_content, output_pdf_path, candidate_name="",process_id=0,room_id=""):
    """Synchronous wrapper for browser-based PDF conversion"""
    try:
        return asyncio.run(convert_html_to_pdf_browser(html_content, output_pdf_path, candidate_name,process_id,room_id))
    except Exception as e:
        logger.error(f"PDF conversion failed: {e}")
        return None


def _resolve_email_recipients(interview):
    """Return recipient list from email_config if set, else recruiter_email, plus HARDCODED_EMAILS."""
    recipients = []
    email_config = interview.get('email_config') if isinstance(interview, dict) else getattr(interview, 'email_config', None)
    if email_config:
        # DB may return a JSON string — parse it first
        if isinstance(email_config, str):
            try:
                import json as _json
                email_config = _json.loads(email_config)
            except (ValueError, TypeError):
                # Plain email string, not JSON
                recipients = [email_config]
        if not recipients:
            if isinstance(email_config, list):
                recipients = [e for e in email_config if e]
            elif isinstance(email_config, dict):
                recipients = [e for e in email_config.get('to', []) if e]
                
    if not recipients:
        recruiter = interview.get('recruiter_email') if isinstance(interview, dict) else getattr(interview, 'recruiter_email', None)
        if recruiter:
            recipients.append(recruiter)
            
    # Add hardcoded emails from config
    for email in HARDCODED_EMAILS:
        if email and email not in recipients:
            recipients.append(email)
            
    return recipients


from dotenv import load_dotenv
load_dotenv(override=True)

CLIENT_ID = os.getenv('MS_GRAPH_CLIENT_ID')
CLIENT_SECRET = os.getenv('MS_GRAPH_CLIENT_SECRET')
TENANT_ID = os.getenv('MS_GRAPH_TENANT_ID')
AUTHORITY = f'https://login.microsoftonline.com/{TENANT_ID}'
SCOPES = ['https://graph.microsoft.com/.default']


def send_email(subject, body, recipients, sender, email_ics_msg=None, cc=None, bcc=None, max_retries=3):
    """Send email with exponential backoff retry for throttling (429 errors)"""
    app = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET
    )
    result = app.acquire_token_for_client(scopes=SCOPES)
    access_token = result.get('access_token')
    if not access_token:
        logger.info("Failed to acquire token:", result.get("error_description"))
        return False

    graph_url = f'https://graph.microsoft.com/v1.0/users/{sender}/sendMail'
    # Ensure recipients, cc, and bcc are lists
    if not recipients:
        logger.error(f"Cannot send email to empty/None recipients for subject: {subject}")
        return False
    if isinstance(recipients, str):
        recipients = [recipients]
    if cc and isinstance(cc, str):
        cc = [cc]
    if bcc and isinstance(bcc, str):
        bcc = [bcc]


    email_msg = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": body
            },
            "toRecipients": [
                {"emailAddress": {"address": addr}} for addr in recipients
            ],
            "ccRecipients": [
                {"emailAddress": {"address": addr}} for addr in cc
            ] if cc else [],
            "bccRecipients": [
                {"emailAddress": {"address": addr}} for addr in bcc
            ] if bcc else []
        },
        "saveToSentItems": True  # Explicitly save to Sent Items folder
    }

    email_msg["message"]["attachments"] = email_ics_msg if email_ics_msg else []

    # Log diagnostic info
    attachment_count = len(email_ics_msg) if email_ics_msg else 0
    logger.info(f"Preparing to send email: To={recipients}, Subject='{subject}', Attachments={attachment_count}")

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    # Retry logic with exponential backoff for throttling
    for attempt in range(max_retries):
        try:
            response = requests.post(graph_url, headers=headers, json=email_msg, timeout=30)

            if response.status_code == 202:
                logger.info(f"✅ Interview email sent successfully! Status: 202, Response: {response.text[:200] if response.text else 'empty'}")
                return True
            elif response.status_code == 429:
                # Throttled - check Retry-After header or use exponential backoff
                retry_after = response.headers.get('Retry-After')
                if retry_after:
                    wait_time = int(retry_after)
                else:
                    # Exponential backoff: 10s, 20s, 40s
                    wait_time = min(2 ** attempt * 10, 60)

                logger.warning(f"⚠️ Email throttled (429). Retrying in {wait_time}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            else:
                logger.error(f"Failed to send email: {response.status_code} - {response.text}")
                return False

        except Exception as e:
            logger.error(f"Exception while sending email: {e}")
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt * 5  # 5s, 10s, 20s
                logger.warning(f"Retrying in {wait_time}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            else:
                return False

    logger.error(f"❌ Failed to send email after {max_retries} retries due to persistent throttling")
    return False




def image_to_base64(path):
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode()
        return f"data:image/png;base64,{encoded}"




def send_interview_feedback_email(interview, DEPLOYMENT):
    # Load the HTML template
    with open("templates/email_interview_feedback.html", "r") as file:
        html_content = file.read()

    if DEPLOYMENT == "PRODUCTION":
        feedback_link = "https://connect.airahr.ai/feedback"
    else:
        feedback_link = "https://connect.airahr.ai/feedback"
    
    html_content = html_content.replace("{{CANDIDATE_NAME}}", interview['candidate_name'])
    html_content = html_content.replace("{{FEEDBACK_LINK}}", feedback_link)

    sender = "support@airahr.ai"
    subject = "We'd Love Your Feedback on AIRAHR (2 mins only)"
    send_email(subject, html_content, interview['candidate_email'], sender)

def sanitize_html_for_pdf(html_content):
    """
    Sanitize HTML email content for PDF generation with WeasyPrint.
    Removes problematic CSS while preserving all content and structure.
    """
    import re

    # Remove problematic CSS properties that WeasyPrint doesn't handle well
    # but keep the content structure intact
    pdf_html = html_content

    # Remove complex flexbox and grid CSS
    pdf_html = re.sub(r'display:\s*flex[^;]*;?', 'display: block;', pdf_html, flags=re.IGNORECASE)
    pdf_html = re.sub(r'display:\s*grid[^;]*;?', 'display: block;', pdf_html, flags=re.IGNORECASE)

    # Simplify complex CSS transforms and animations
    pdf_html = re.sub(r'transform:[^;]*;?', '', pdf_html, flags=re.IGNORECASE)
    pdf_html = re.sub(r'animation[^;]*;?', '', pdf_html, flags=re.IGNORECASE)
    pdf_html = re.sub(r'transition[^;]*;?', '', pdf_html, flags=re.IGNORECASE)

    # Remove problematic CSS3 properties
    pdf_html = re.sub(r'box-shadow:[^;]*;?', '', pdf_html, flags=re.IGNORECASE)
    pdf_html = re.sub(r'text-shadow:[^;]*;?', '', pdf_html, flags=re.IGNORECASE)

    # Ensure basic styling for readability
    basic_css = """
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; color: #333; line-height: 1.4; }
        h1, h2, h3 { color: #2d5aa0; margin: 20px 0 10px 0; }
        h1 { font-size: 24px; border-bottom: 2px solid #2d5aa0; padding-bottom: 10px; }
        h2 { font-size: 20px; }
        h3 { font-size: 16px; }
        .section { margin: 15px 0; padding: 15px; border: 1px solid #ddd; border-radius: 5px; }
        .pass { background-color: #e6f8f1; }
        .fail { background-color: #ffeaea; }
        table { width: 100%; border-collapse: collapse; margin: 10px 0; }
        td, th { padding: 8px; text-align: left; border-bottom: 1px solid #ddd; }
        ul { margin: 10px 0; padding-left: 20px; }
        li { margin: 5px 0; }
        p { margin: 10px 0; }
        .metric-bar { background: #f0f0f0; height: 20px; border-radius: 10px; overflow: hidden; margin: 5px 0; }
        .metric-fill { height: 100%; background: #4CAF50; }
        .footer { margin-top: 30px; padding: 15px; background: #f0f0f0; text-align: center; font-size: 12px; }
    </style>
    """

    # Insert our basic CSS after the <head> tag or create one
    if '<head>' in pdf_html:
        pdf_html = pdf_html.replace('<head>', f'<head>{basic_css}')
    elif '<html>' in pdf_html:
        pdf_html = pdf_html.replace('<html>', f'<html><head>{basic_css}</head>')
    else:
        pdf_html = f'<html><head>{basic_css}</head><body>{pdf_html}</body></html>'

    return pdf_html


def create_pdf_compatible_html(template_data):
    """Create a simplified HTML version for PDF generation without problematic CSS"""

    candidate = template_data.get('candidate', {})
    client = template_data.get('client', {})
    interview = template_data.get('interview', {})
    verdict = template_data.get('verdict', {})
    overview = template_data.get('overview', {})
    questions = template_data.get('questions', {}).get('list', [])

    # Simple CSS that works with WeasyPrint
    simple_css = """
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; color: #333; }
        h1 { color: #4a90e2; border-bottom: 2px solid #4a90e2; padding-bottom: 10px; }
        h2 { color: #2d7a4b; margin-top: 30px; }
        h3 { color: #555; }
        .section { margin: 20px 0; padding: 15px; border: 1px solid #ddd; border-radius: 8px; }
        .pass { background-color: #e6f8f1; }
        .fail { background-color: #ffeaea; }
        .metric { margin: 10px 0; padding: 8px; background: #f5f5f5; border-radius: 4px; }
        .question { margin: 15px 0; padding: 12px; background: #f9f9f9; border-left: 4px solid #4a90e2; }
        .footer { margin-top: 30px; padding: 15px; background: #f0f0f0; text-align: center; font-size: 12px; }
        table { width: 100%; border-collapse: collapse; margin: 10px 0; }
        td, th { padding: 8px; text-align: left; border-bottom: 1px solid #ddd; }
    </style>
    """

    # Build simple HTML
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>AIRA Interview Report</title>
        {simple_css}
    </head>
    <body>
        <h1>AIRA - Interview Report</h1>
       
        <div class="section">
            <h2>Candidate Information</h2>
            <table>
                <tr><td><strong>Name:</strong></td><td>{candidate.get('name', '')}</td></tr>
                <tr><td><strong>Email:</strong></td><td>{candidate.get('email', '')}</td></tr>
                <tr><td><strong>Phone:</strong></td><td>{candidate.get('phone', '')}</td></tr>
                <tr><td><strong>Position:</strong></td><td>{client.get('position', '')}</td></tr>
                <tr><td><strong>Company:</strong></td><td>{client.get('company', '')}</td></tr>
                <tr><td><strong>Interview Date:</strong></td><td>{interview.get('date', '')} at {interview.get('time', '')}</td></tr>
            </table>
        </div>
       
        <div class="section {'pass' if verdict.get('passed') else 'fail'}">
            <h2>Interview Result: {verdict.get('status', 'Unknown')}</h2>
            <p><strong>Overall Assessment:</strong> {verdict.get('overall_assessment', '')}</p>
            <p><strong>Recommendation:</strong> {verdict.get('subtitle', '')}</p>
        </div>
       
        <div class="section">
            <h2>Performance Overview</h2>
            <p>{overview.get('summary', '')}</p>
    """

    # Add metrics
    for metric in overview.get('metrics', []):
        html += f'<div class="metric"><strong>{metric.get("name", "")}:</strong> {metric.get("score", 0)}%</div>'

    html += """
        </div>
       
        <div class="section">
            <h2>Strengths & Development Areas</h2>
    """

    # Add strengths
    strengths = verdict.get('strengths', {}).get('items', [])
    if strengths:
        html += "<h3>Key Strengths:</h3><ul>"
        for strength in strengths:
            html += f"<li>{strength}</li>"
        html += "</ul>"

    # Add development areas
    areas = verdict.get('areas', {}).get('items', [])
    if areas:
        html += "<h3>Areas for Development:</h3><ul>"
        for area in areas:
            html += f"<li>{area}</li>"
        html += "</ul>"

    html += "</div>"

    # Add individual questions section only if questions exist
    # Add individual questions section only if questions exist
    if questions:
        html += """
       
        <div class="section">
            <h2>Question-by-Question Analysis</h2>
        """

        # Add questions with detailed analysis
        for q in questions:
            html += f"""
            <div class="question">
                <h3>Question {q.get('id', '')}: {q.get('question', '')}</h3>
                <p><strong>Answer:</strong> {q.get('answer', '')}</p>
                <p><strong>Analysis:</strong> {q.get('analysis', '')}</p>
                <p><strong>Overall Score:</strong> {q.get('overall_score', 0)}/100</p>
            """

            # Add detailed metrics breakdown
            metrics = q.get('metrics', [])
            if metrics:
                html += "<h4>Performance Breakdown:</h4>"
                for metric in metrics:
                    html += f'<div class="metric"><strong>{metric.get("name", "")}:</strong> {metric.get("score", 0)}/100</div>'

            # Add strengths
            strengths = q.get('strengths', [])
            if strengths:
                html += "<h4>✅ Strengths:</h4><ul>"
                for strength in strengths:
                    html += f"<li>{strength}</li>"
                html += "</ul>"

            # Add areas for improvement
            weaknesses = q.get('weaknesses', [])
            if weaknesses:
                html += "<h4>⚠️ Areas for Improvement:</h4><ul>"
                for weakness in weaknesses:
                    html += f"<li>{weakness}</li>"
                html += "</ul>"

            html += "</div>"

        # Close the questions section
        html += "</div>"

    # Close HTML
    html += f"""
        <div class="footer">
            <p>{template_data.get('footer', {}).get('text', 'Generated by AIRA')}</p>
        </div>
    </body>
    </html>
    """

    return html

def send_interview_summary_email(interview, json_final_analysis, analysis_blocks,process_id,room_id):
    """Enhanced email function using Jinja2 template for rich interview reports"""
    html_data = {}
    try:
        logger.info(f"[process id: {process_id}][room id: {room_id}] Starting interview summary email generation")

        # STEP 1: Process and extract best face image FIRST (before any template generation)
        try:
            if FACE_EXTRACTOR_AVAILABLE and face_extractor:
                candidate_id = interview.get('id')
                logger.info(f"[process id: {process_id}][room id: {room_id}] Processing face extraction for interview {candidate_id}")
                best_face_path = face_extractor.select_best_face(
                    source_folder_name=candidate_id,
                    source_file_name_prefix=f"zoom_tile_{candidate_id}",
                    face_folder="best_faces"
                )
                if best_face_path:
                    logger.info(f"[process id: {process_id}][room id: {room_id}] ✅ Best face extracted and saved: {best_face_path}")
                else:
                    logger.warning(f"[process id: {process_id}][room id: {room_id}] ⚠️ No suitable face found for interview {candidate_id}")
            else:
                logger.warning(f"[process id: {process_id}][room id: {room_id}] Face extraction disabled - OpenCV not available")
        except Exception as e:
            logger.warning(f"[process id: {process_id}][room id: {room_id}] Face extraction failed for interview {interview.get('id')}: {e}")

        # STEP 2: Initialize template handler and generate BOTH reports (now that faces are ready)
        template_handler = InterviewReportTemplate()

        # Generate candidate ID and timestamp for file naming
        candidate_id = interview.get('candidate_jd_id', 'unknown')
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Generate BOTH Summary and Detailed reports
        all_files = template_handler.generate_both_reports(
            interview, json_final_analysis, analysis_blocks, candidate_id, timestamp,process_id,room_id
        )
        send_sentinel_reports="sentinel_html" in all_files
        if send_sentinel_reports:
            summary_html= all_files.get("summary_html")
            summary_path= all_files.get("summary_path")
            detailed_html= all_files.get("detailed_html")
            detailed_path= all_files.get("detailed_path")
            sentinel_html= all_files.get("sentinel_html")
            sentinel_path= all_files.get("sentinel_path")

            html_data['sentinel_html'] = sentinel_html
        else:
            summary_html= all_files.get("summary_html")
            summary_path= all_files.get("summary_path")
            detailed_html= all_files.get("detailed_html")
            detailed_path= all_files.get("detailed_path")
        html_data['summary_html'] = summary_html
        html_data['detailed_html'] = detailed_html
        # Pass structured report data through for DB storage (replaces HTML bytea storage)
        html_data['report_data_dict'] = all_files.get("report_data_dict", {})
        # Create interview_reports directory if it doesn't exist    
        interview_reports_dir = "interview_reports"
        os.makedirs(interview_reports_dir, exist_ok=True)

        # Define the custom file paths with room_id naming
        custom_summary_path = os.path.join(interview_reports_dir, f"{room_id}_summary_report.html")
        custom_detailed_path = os.path.join(interview_reports_dir, f"{room_id}_detailed_report.html")
        if send_sentinel_reports:
            custom_sentinel_path = os.path.join(interview_reports_dir, f"{room_id}_sentinel_report.html")
            with open(custom_sentinel_path, 'w', encoding='utf-8') as f:
                f.write(sentinel_html)

        # Save the HTML files with custom names
        with open(custom_summary_path, 'w', encoding='utf-8') as f:
            f.write(summary_html)
            
        with open(custom_detailed_path, 'w', encoding='utf-8') as f:
            f.write(detailed_html)
            

        logger.info(f"[process id: {process_id}][room id: {room_id}] Saved HTML reports:")
        logger.info(f"  - Summary: {custom_summary_path}")
        logger.info(f"  - Detailed: {custom_detailed_path}")
        if send_sentinel_reports:
            logger.info(f"  - Sentinel: {custom_sentinel_path}")

        # After generating the reports, add code to save them with your desired naming:
        logger.info(f"[process id: {process_id}][room id: {room_id}] Generated Summary report: {summary_path}")
        logger.info(f"[process id: {process_id}][room id: {room_id}] Generated Detailed report: {detailed_path}")
        if send_sentinel_reports:
            logger.info(f"[process id: {process_id}][room id: {room_id}] Generated Sentinel report: {sentinel_path}")

        # Generate PDFs from both HTML reports
        safe_name = sanitize_filename(interview['candidate_name'])
        summary_pdf_filename = f"{safe_name}_Summary_Report_{timestamp}.pdf"
        detailed_pdf_filename = f"{safe_name}_Detailed_Report_{timestamp}.pdf"
        if send_sentinel_reports:
            sentinel_pdf_filename= f"{safe_name}_Sentinel_Report_{timestamp}.pdf"
        # Generate PDFs from both HTML reports
        try:
            # Use browser-based PDF conversion for perfect content preservation
            logger.info(f"[process id: {process_id}][room id: {room_id}] Generating PDFs using browser-based conversion...")

            # Generate Summary PDF
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_summary_pdf:
                summary_pdf_path = convert_interview_html_to_pdf(
                    summary_html,
                    tmp_summary_pdf.name,
                    candidate_id,
                    process_id,
                    room_id
                )

                with open(summary_pdf_path, "rb") as f:
                    summary_pdf_bytes = f.read()
                encoded_summary_pdf = base64.b64encode(summary_pdf_bytes).decode('utf-8')

            # Generate Detailed PDF
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_detailed_pdf:
                detailed_pdf_path = convert_interview_html_to_pdf(
                    detailed_html,
                    tmp_detailed_pdf.name,
                    candidate_id,
                    process_id,
                    room_id
                )

                with open(detailed_pdf_path, "rb") as f:
                    detailed_pdf_bytes = f.read()
                encoded_detailed_pdf = base64.b64encode(detailed_pdf_bytes).decode('utf-8')

            # Generate Sentinel PDF
            if send_sentinel_reports:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_sentinel_pdf:
                    sentinel_pdf_path = convert_interview_html_to_pdf(
                        sentinel_html,
                        tmp_sentinel_pdf.name,
                        candidate_id,
                        process_id,
                        room_id
                    )

                    with open(sentinel_pdf_path, "rb") as f:
                        sentinel_pdf_bytes = f.read()
                    encoded_sentinel_pdf = base64.b64encode(sentinel_pdf_bytes).decode('utf-8')

            # Create attachments for BOTH reports (PDF only - removed HTML duplicates to reduce size)
            attachments = [
                # Summary Report - PDF
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": summary_pdf_filename,
                    "contentType": "application/pdf",
                    "contentBytes": encoded_summary_pdf
                },
                # Detailed Report - PDF
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": detailed_pdf_filename,
                    "contentType": "application/pdf",
                    "contentBytes": encoded_detailed_pdf
                }
                    # Sentinel Report - PDF
                    
                ]
            if send_sentinel_reports:
                attachments.append({
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": sentinel_pdf_filename,
                    "contentType": "application/pdf",
                    "contentBytes": encoded_sentinel_pdf
                })
            # Cleanup temporary PDF files
            os.unlink(tmp_summary_pdf.name)
            os.unlink(tmp_detailed_pdf.name)
            if send_sentinel_reports:
                os.unlink(tmp_sentinel_pdf.name)
                logger.info(f"[process id: {process_id}][room id: {room_id}] Successfully generated Summary, Detailed and Sentinel PDFs using browser rendering")
            else:
                logger.info(f"[process id: {process_id}][room id: {room_id}] Successfully generated Summary, Detailed PDFs using browser rendering")

        except Exception as e:
            logger.error(f"Browser-based PDF generation failed: {e}")
            logger.info(f"[process id: {process_id}][room id: {room_id}] Falling back to HTML-only attachments")
            attachments = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": f"{safe_name}_Summary_Report_{timestamp}.html",
                    "contentType": "text/html",
                    "contentBytes": base64.b64encode(summary_html.encode('utf-8')).decode('utf-8')
                },
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": f"{safe_name}_Detailed_Report_{timestamp}.html",
                    "contentType": "text/html",
                    "contentBytes": base64.b64encode(detailed_html.encode('utf-8')).decode('utf-8')
                }
            ]
            if send_sentinel_reports and sentinel_html:
                attachments.append({
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": f"{safe_name}_sentinel_Report_{timestamp}.html",
                    "contentType": "text/html",
                    "contentBytes": base64.b64encode(sentinel_html.encode('utf-8')).decode('utf-8')
                })

        # Send email with BOTH report attachments (use summary HTML as email body)
        sender = "support@airahr.ai"
        subject = f"[AIRA] Interview Report – {interview['candidate_name']} for {interview['job_title']}"
        send_email(subject, summary_html, _resolve_email_recipients(interview), sender, email_ics_msg=attachments)

        logger.info(f"[process id: {process_id}][room id: {room_id}] Enhanced interview reports sent successfully for {interview['candidate_name']}")
        logger.info(f"[process id: {process_id}][room id: {room_id}] Summary report file saved at: {summary_path}")
        logger.info(f"[process id: {process_id}][room id: {room_id}] Detailed report file saved at: {detailed_path}")

        # Final cleanup: Rename folders with timestamp (LAST STEP after everything is complete)
        try:
            if FACE_EXTRACTOR_AVAILABLE and face_extractor:
                face_extractor.rename_folder_with_timestamp("best_faces/" + str(interview.get('id')))
        except Exception as e:
            logger.warning(f"[process id: {process_id}][room id: {room_id}] Could not rename best_faces folder for interview {interview.get('id')}: {e}")

        try:
            if FACE_EXTRACTOR_AVAILABLE and face_extractor:
                face_extractor.rename_folder_with_timestamp("captured_tiles/" + str(interview.get('id')))
        except Exception as e:
            logger.warning(f"[process id: {process_id}][room id: {room_id}] Could not rename captured_tiles folder for interview {interview.get('id')}: {e}")

        logger.info(f"[process id: {process_id}][room id: {room_id}] ✅ Final cleanup completed - folders renamed with timestamp")
        return html_data
    except Exception as e:
        logger.error(f"[process id: {process_id}][room id: {room_id}] Error sending enhanced interview summary email: {e}")
        # Fallback to basic email if template fails
        try:
            basic_subject = f"[AIRA] Interview Report – {interview['candidate_name']} for {interview['job_title']}"
            # Read pass threshold from DB interview_config, fallback to static config
            import json as _json
            _raw_ic = interview.get("interview_config")
            _ic = _json.loads(_raw_ic) if isinstance(_raw_ic, str) else (_raw_ic or {})
            _pass_threshold = int(_ic.get("pass_threshold_pct", INTERVIEW_PASS_THRESHOLD_PERCENTAGE))
            basic_content = f"""
            <h2>Interview Report</h2>
            <p><strong>Candidate:</strong> {interview['candidate_name']}</p>
            <p><strong>Position:</strong> {interview['job_title']}</p>
            <p><strong>Score:</strong> {json_final_analysis.get('final_score', 0):.0f}%</p>
            <p><strong>Verdict:</strong> {'PASS' if json_final_analysis.get('final_score', 0) >= _pass_threshold else 'FAIL'}</p>
            <p><strong>Analysis:</strong> {json_final_analysis.get('final_analysis', 'Analysis not available')}</p>
            """
            send_email(basic_subject, basic_content, _resolve_email_recipients(interview), "support@airahr.ai")
            logger.info(f"[process id: {process_id}][room id: {room_id}] Sent basic fallback email")
        except Exception as fallback_error:
            logger.error(f"Failed to send fallback email: {fallback_error}")
            return html_data


def send_interview_terminates_as_candidate_not_joied(interview):
    # Load the HTML template
    with open("templates/email_candidate_did_not_join_interview.html", "r") as file:
        html_content = file.read()

    # Replace placeholders
    html_content = html_content.replace("{{CANDIDATE_NAME}}", interview['candidate_name'])
    html_content = html_content.replace("{{CANDIDATE_EMAIL}}", interview['candidate_email'])
    html_content = html_content.replace("{{JOB_TITLE}}", interview['job_title'])
    html_content = html_content.replace("{{INTERVIEW_DATETIME}}",
                                        interview['interview_time'].strftime("%Y-%m-%d %H:%M:%S %p"))
    html_content = html_content.replace("{{INTERVIEW_TIMEZONE}}", interview['interview_timezone'])
    # html_content = html_content.replace("{{interview_datetime}}", interview_datetime or "")
    # html_content = html_content.replace("{{interview_timezone}}", interview_timezone or "")

    sender = "support@airahr.ai"
    subject = f"[AIRA]Interview terminated - Candidate {interview['candidate_name']} did not join for job - {interview['job_title']}"
    send_email(subject, html_content, interview['candidate_email'], sender, None, _resolve_email_recipients(interview), None)


def sanitize_filename(name):
    name = name.strip().lower()
    name = re.sub(r'\s+', '_', name)  # Replace spaces with underscores
    name = re.sub(r'[^a-z0-9_]', '', name)  # Remove anything that's not alphanumeric or underscore
    return name

# # Example usage:

# if __name__ == "__main__":
#     interview = {
#         "id": 87,
#         "candidate_name": "John Doe",
#         "candidate_email": "john.doe@example.com",
#         "interview_time": datetime(2025, 7, 6, 15, 30),
#         "job_title": "Senior DevOps Engineer",
#         "company_name": "AiraHr Inc.",
#         "resume_match_score": 87,
#         "resume_job_analysis": "The candidate's resume matches 87% of the job description. Strong experience in CI/CD, AWS, and container orchestration. Slight gaps in GCP-related experience.",
#         "recruiter_email": "recruiter@Airahr.ai"
#     }
#     json_final_analysis = {
#         "final_analysis": "The candidate's responses were consistently vague, lacking relevance, and failed to address the questions effectively. There was a significant disconnect between the questions asked and the answers provided, indicating a lack of understanding or preparation. Overall, the candidate's performance was poor.",
#         "final_score": 2.0
#     }

#     analysis_blocks = [
#         {"question": "To begin, could you provide a brief summary of your professional background and key qualifications?",
#         "answer": "I think which will be, your shift programmers.",
#         "analysis": "The answer provided is not relevant to the question asked and lacks clarity. It seems to be a random response unrelated to the professional background and qualifications requested. There is a significant disconnect between the question and the answer.",
#         "score": 2.0},
#         {"question": "Can you explain your experience with configuring, designing, developing, administering, and maintaining IBM WebSphere MQ environments?",
#         "answer": "Yeah. So were nothing much about it. So",
#         "analysis": "The answer provided is extremely vague and lacks any meaningful information related to the question. It does not address the various aspects of configuring, designing, developing, administering, and maintaining IBM WebSphere MQ environments. A score of 2 is given for acknowledging some familiarity with the topic, but the response is highly insufficient.",
#         "score": 2.0}
#     ]

#     send_interview_summary_email(interview, json_final_analysis, analysis_blocks)        







def send_candidate_absence_notification(interview):
    candidate_name     = interview.get("candidate_name")
    candidate_email    = interview.get("candidate_email")
    recruiter_email    = interview.get("recruiter_email")
    job_title          = interview.get("job_title")
    interview_datetime = interview.get("interview_time", "N/A")
    interview_timezone = interview.get("zone", "")
    company_name       = interview.get("company_name", "AIRA")

    subject = f"Interview Terminated: {job_title}"

    if isinstance(interview_datetime, datetime):
        interview_datetime_str = interview_datetime.strftime("%Y-%m-%d %I:%M %p")
    else:
        interview_datetime_str = str(interview_datetime)

    try:
        with open("templates/email_candidate_did_not_join_interview.html", "r", encoding="utf-8") as f:
            html_template = f.read()
    except FileNotFoundError:
        logger.error("❌ email_candidate_did_not_join_interview.html template not found.")
        return

    html_body = (html_template
        .replace("{{CANDIDATE_NAME}}", candidate_name or "")
        .replace("{{CANDIDATE_EMAIL}}", candidate_email or "")
        .replace("{{JOB_TITLE}}", job_title or "")
        .replace("{{INTERVIEW_DATETIME}}", interview_datetime_str)
        .replace("{{INTERVIEW_TIMEZONE}}", interview_timezone or "")
        .replace("{{COMPANY_NAME}}", company_name or "AIRA")
    )

    try:
        send_email(
            subject=subject,
            body=html_body,
            recipients=[candidate_email],
            sender="support@airahr.ai",
            cc=_resolve_email_recipients(interview)
        )
        logger.info(f"✅ Candidate absence email sent (To={candidate_email}, Cc={recruiter_email})")
    except Exception as e:
        logger.exception(f"❌ Failed to send candidate absence email via Microsoft Graph: {e}")









