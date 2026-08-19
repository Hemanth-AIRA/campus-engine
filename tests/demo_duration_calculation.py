#!/usr/bin/env python3

"""
Demonstration Script for Interview Duration Calculation
Shows how the duration calculation works in the actual interview flow
"""

import sys
import os
from datetime import datetime, timedelta

# Add project root to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../')))

def simulate_duration_calculation(start_time, end_time):
    """
    Simulate the duration calculation logic from zoom_interview.py
    
    Args:
        start_time (datetime): Interview start time
        end_time (datetime): Interview end time
    
    Returns:
        tuple: (duration_str, minutes, seconds)
    """
    # Calculate duration (as done in zoom_interview.py)
    interview_duration_seconds = (end_time - start_time).total_seconds()
    interview_duration_minutes = int(interview_duration_seconds // 60)
    interview_duration_remainder_seconds = int(interview_duration_seconds % 60)
    
    # Format duration string (as done in zoom_interview.py)
    if interview_duration_minutes > 0:
        if interview_duration_minutes == 1:
            duration_str = f"1 min{' ' + str(interview_duration_remainder_seconds) + ' sec' if interview_duration_remainder_seconds > 0 else ''}"
        else:
            duration_str = f"{interview_duration_minutes} mins{' ' + str(interview_duration_remainder_seconds) + ' sec' if interview_duration_remainder_seconds > 0 else ''}"
    else:
        duration_str = f"{interview_duration_remainder_seconds} secs"
    
    return duration_str, interview_duration_minutes, interview_duration_remainder_seconds

def demonstrate_template_integration(duration_str):
    """
    Demonstrate how the duration is used in template generation
    
    Args:
        duration_str (str): Formatted duration string
    
    Returns:
        dict: Sample template data showing duration integration
    """
    try:
        from interview_template_handler import InterviewReportTemplate
        
        # Mock interview data with actual duration
        mock_interview = {
            'actual_duration': duration_str,
            'candidate_name': 'Demo Candidate',
            'interview_time': datetime.now(),
            'interview_timezone': 'UTC',
            'company_name': 'Demo Company',
            'job_title': 'Software Engineer'
        }
        
        # Mock analysis data
        mock_analysis = {
            'final_analysis': 'Demo analysis for duration calculation',
            'final_score': 7.5
        }
        
        mock_blocks = []
        
        # Generate template data
        handler = InterviewReportTemplate()
        template_data = handler.generate_template_data(
            mock_interview, 
            mock_analysis, 
            mock_blocks
        )
        
        return {
            'duration_used': template_data['interview']['duration'],
            'candidate_name': template_data['candidate']['name'],
            'company': template_data['client']['company']
        }
        
    except ImportError:
        return {
            'duration_used': duration_str,
            'note': 'Template handler not available, showing duration only'
        }

def main():
    """Main demonstration function"""
    print("🎯 Interview Duration Calculation Demonstration")
    print("=" * 60)
    
    # Test scenarios with realistic interview durations
    scenarios = [
        {
            'name': 'Quick Screening Call',
            'duration': timedelta(minutes=3, seconds=45),
            'description': 'Short initial screening'
        },
        {
            'name': 'Technical Phone Interview',
            'duration': timedelta(minutes=15, seconds=30),
            'description': 'Medium technical discussion'
        },
        {
            'name': 'Full Technical Interview',
            'duration': timedelta(minutes=25, seconds=0),
            'description': 'Complete technical assessment'
        },
        {
            'name': 'Extended Panel Interview',
            'duration': timedelta(minutes=45, seconds=15),
            'description': 'Comprehensive panel discussion'
        },
        {
            'name': 'Brief Interruption',
            'duration': timedelta(seconds=30),
            'description': 'Interview ended early'
        }
    ]
    
    for i, scenario in enumerate(scenarios, 1):
        print(f"\n{i}. {scenario['name']}")
        print(f"   Description: {scenario['description']}")
        
        # Simulate interview timing
        start_time = datetime(2025, 8, 20, 14, 30, 0)
        end_time = start_time + scenario['duration']
        
        print(f"   Start Time: {start_time.strftime('%H:%M:%S')}")
        print(f"   End Time: {end_time.strftime('%H:%M:%S')}")
        
        # Calculate duration using our logic
        duration_str, minutes, seconds = simulate_duration_calculation(start_time, end_time)
        
        print(f"   📊 Calculated Duration: {duration_str}")
        print(f"   🔢 Raw Data: {minutes} minutes, {seconds} seconds")
        
        # Demonstrate template integration
        template_result = demonstrate_template_integration(duration_str)
        print(f"   📄 Template Integration: {template_result}")
    
    print("\n" + "=" * 60)
    print("✅ Demonstration completed successfully!")
    print("\n📋 Summary:")
    print("   - Duration is calculated from ask_questions_in_zoom function start to end")
    print("   - Formatting handles seconds, minutes, and combinations intelligently")
    print("   - Template handler uses actual_duration with fallback to '25 mins'")
    print("   - Integration is seamless and maintains backward compatibility")

if __name__ == "__main__":
    main()
