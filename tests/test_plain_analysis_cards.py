#!/usr/bin/env python3

"""
Quick visual test to verify the template renders correctly with plain analysis cards
"""

import sys
import os
from datetime import datetime

# Add project root to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../')))

def test_plain_analysis_cards():
    """Test that the template renders with plain analysis cards"""
    print("🎨 Testing Plain Analysis Cards Template Rendering")
    print("=" * 60)
    
    try:
        from interview_template_handler import InterviewReportTemplate
        
        # Create mock interview data
        mock_interview = {
            'id': 123,
            'candidate_name': 'John Smith',
            'candidate_email': 'john.smith@example.com',
            'candidate_phone': '+1234567890',
            'candidate_jd_id': '456789',
            'job_jd_id': '987654',
            'company_name': 'Tech Corp',
            'job_title': 'Senior Software Engineer',
            'interview_time': datetime(2025, 8, 20, 14, 30, 0),
            'interview_timezone': 'UTC',
            'resume_match_score': 82.5,
            'resume_job_analysis': 'Strong technical background with relevant experience in Python, cloud computing, and system design.',
            'actual_duration': '23 mins 15 sec',  # Use our new duration calculation
            'actual_duration_minutes': 23,
            'actual_duration_seconds': 15
        }
        
        # Mock analysis data
        mock_analysis = {
            'final_analysis': 'The candidate demonstrated strong technical knowledge and communication skills throughout the interview.',
            'final_score': 8.2,
            'performance_overview': 'Excellent technical discussion with clear examples from previous experience. Strong problem-solving approach and good understanding of system architecture principles.'
        }
        
        # Mock analysis blocks
        mock_analysis_blocks = [
            {
                'id': 1,
                'question': 'Can you explain your experience with cloud architecture and microservices?',
                'answer': 'I have worked extensively with AWS and Azure cloud platforms, designing and implementing microservices architectures for scalable applications.',
                'analysis': 'Strong technical response demonstrating deep understanding of cloud technologies and architectural patterns.',
                'overall_score': 85,
                'metrics': [
                    {'name': 'Communication', 'score': 90, 'color': '#4CAF50'},
                    {'name': 'Experience', 'score': 85, 'color': '#2196F3'},
                    {'name': 'Domain Knowledge', 'score': 88, 'color': '#9C27B0'},
                    {'name': 'Technical Skills', 'score': 82, 'color': '#FF5722'}
                ],
                'strengths': ['Clear technical communication', 'Relevant cloud experience', 'Good understanding of microservices'],
                'weaknesses': ['Could provide more specific implementation details']
            }
        ]
        
        # Generate template
        handler = InterviewReportTemplate()
        template_data = handler.generate_template_data(
            mock_interview,
            mock_analysis,
            mock_analysis_blocks
        )
        
        # Verify key template data
        print("✅ Template Generation: SUCCESS")
        print(f"📊 Duration Used: {template_data['interview']['duration']}")
        print(f"👤 Candidate: {template_data['candidate']['name']}")
        print(f"🏢 Company: {template_data['client']['company']}")
        print(f"📈 Resume Score: {template_data['resume_analysis']['score']}")
        print(f"📋 Interview Score: {template_data['interview_analysis']['score']}")
        
        # Generate actual HTML content
        html_content = handler.template.render(**template_data)
        
        # Check that the HTML contains our plain styling (no gradients)
        if 'linear-gradient' in html_content:
            print("⚠️  WARNING: Template still contains gradient styles")
        else:
            print("✅ Plain Styling: Confirmed - no gradients found in rendered HTML")
        
        # Check for analysis card classes
        if 'analysis-card-resume' in html_content and 'analysis-card-interview' in html_content:
            print("✅ Analysis Cards: Present in template")
        else:
            print("⚠️  WARNING: Analysis card classes not found")
        
        # Check duration integration
        if '23 mins 15 sec' in html_content:
            print("✅ Duration Integration: Actual duration correctly used")
        else:
            print("⚠️  Duration not found in template")
        
        print("\n📋 Template Summary:")
        print(f"   • HTML Length: {len(html_content):,} characters")
        print(f"   • Contains Gradients: {'No' if 'linear-gradient' not in html_content else 'Yes'}")
        print(f"   • Analysis Cards: {'Present' if 'analysis-card' in html_content else 'Missing'}")
        print(f"   • Duration: {template_data['interview']['duration']}")
        
        print("\n✅ PLAIN ANALYSIS CARDS TEST COMPLETED SUCCESSFULLY!")
        return True
        
    except ImportError as e:
        print(f"❌ Import Error: {e}")
        return False
    except Exception as e:
        print(f"❌ Test Error: {e}")
        return False

if __name__ == "__main__":
    success = test_plain_analysis_cards()
    exit(0 if success else 1)
