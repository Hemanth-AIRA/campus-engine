#!/usr/bin/env python3

"""
Specific test to verify that analysis cards no longer have gradient backgrounds
"""

import sys
import os
import re
from datetime import datetime

# Add project root to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../')))

def test_analysis_card_styling():
    """Test that analysis cards have plain white backgrounds, not gradients"""
    print("🎨 Testing Analysis Card Styling Changes")
    print("=" * 50)
    
    try:
        from interview_template_handler import InterviewReportTemplate
        
        # Create simple mock data
        mock_interview = {
            'candidate_name': 'Test User',
            'company_name': 'Test Company',
            'job_title': 'Test Position',
            'interview_time': datetime.now(),
            'resume_match_score': 75,
            'resume_job_analysis': 'Test analysis',
            'actual_duration': '15 mins 30 sec'
        }
        
        mock_analysis = {
            'final_analysis': 'Test final analysis',
            'final_score': 7.5
        }
        
        # Generate template
        handler = InterviewReportTemplate()
        template_data = handler.generate_template_data(mock_interview, mock_analysis, [])
        html_content = handler.template.render(**template_data)
        
        # Check CSS styles in the template
        print("🔍 Analyzing CSS styles for analysis cards...")
        
        # Extract all CSS within <style> tags
        style_matches = re.findall(r'<style>(.*?)</style>', html_content, re.DOTALL)
        
        analysis_card_styles_found = False
        has_gradient_in_analysis_cards = False
        
        for style_content in style_matches:
            # Look for analysis card styles
            if '.analysis-card-resume' in style_content or '.analysis-card-interview' in style_content:
                analysis_card_styles_found = True
                print("✅ Found analysis card styles in template")
                
                # Check specifically for gradients in analysis card sections
                # Extract the analysis card CSS blocks
                resume_card_match = re.search(r'\.analysis-card-resume\s*{([^}]+)}', style_content)
                interview_card_match = re.search(r'\.analysis-card-interview\s*{([^}]+)}', style_content)
                
                if resume_card_match:
                    resume_styles = resume_card_match.group(1)
                    print(f"📋 Resume card styles: {resume_styles.strip()}")
                    if 'linear-gradient' in resume_styles:
                        has_gradient_in_analysis_cards = True
                        print("❌ Resume card still has gradient!")
                    else:
                        print("✅ Resume card has plain background")
                
                if interview_card_match:
                    interview_styles = interview_card_match.group(1)
                    print(f"📋 Interview card styles: {interview_styles.strip()}")
                    if 'linear-gradient' in interview_styles:
                        has_gradient_in_analysis_cards = True
                        print("❌ Interview card still has gradient!")
                    else:
                        print("✅ Interview card has plain background")
        
        # Summary
        print(f"\n📊 Analysis Results:")
        print(f"   • Analysis card styles found: {'Yes' if analysis_card_styles_found else 'No'}")
        print(f"   • Gradients in analysis cards: {'Yes' if has_gradient_in_analysis_cards else 'No'}")
        print(f"   • Duration integration: {template_data['interview']['duration']}")
        
        if analysis_card_styles_found and not has_gradient_in_analysis_cards:
            print("\n✅ SUCCESS: Analysis cards have plain backgrounds!")
            return True
        else:
            print("\n❌ ISSUE: Analysis cards may still have gradients")
            return False
            
    except ImportError as e:
        print(f"❌ Import Error: {e}")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

if __name__ == "__main__":
    success = test_analysis_card_styling()
    exit(0 if success else 1)
