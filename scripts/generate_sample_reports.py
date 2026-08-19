#!/usr/bin/env python3
"""
Generate sample AIRA reports (both summary and detailed) for pass and fail scenarios
"""

from jinja2 import Template
from datetime import datetime

# Read the templates
with open('aira_summary_report_template.html.j2', 'r') as f:
    summary_template = Template(f.read())

with open('aira_detailed_report_template.html.j2', 'r') as f:
    detailed_template = Template(f.read())

# Sample data for PASS scenario
pass_data = {
    'header': {
        'title': 'Senior Python Developer Interview - John Smith'
    },
    'candidate': {
        'name': 'John Smith',
        'email': 'john.smith@email.com',
        'phone': '+1-555-0123',
        'photo_url': ''
    },
    'client': {
        'company': 'TechCorp Inc.',
        'position': 'Senior Python Developer'
    },
    'interview': {
        'date': '2025-12-03',
        'time': '10:00 AM',
        'timezone': 'PST',
        'duration': '45 minutes',
        'questions_count': '8',
        'mode': 'Video',
        'room_id': 'ROOM-2025-1203-001'
    },
    'resume_analysis': {
        'score': 4.2,
        'recommendation': 'Strong technical background with 7+ years of Python experience. Resume demonstrates expertise in Django, Flask, and data engineering.'
    },
    'interview_analysis': {
        'score': 4.4,
        'recommendation': 'Excellent performance across technical and behavioral questions. Demonstrated strong problem-solving skills and clear communication.'
    },
    'verdict': {
        'passed': True,
        'icon': '✓',
        'status': 'RECOMMENDED',
        'subtitle': 'Strong candidate for the position',
        'strengths': {
            'title': 'Key Strengths',
            'items': [
                'Demonstrated deep understanding of Python design patterns and best practices',
                'Excellent problem-solving approach with clear articulation of thought process',
                'Strong experience with RESTful API design and microservices architecture',
                'Good understanding of database optimization and query performance'
            ]
        },
        'areas': {
            'title': 'Growth Opportunities',
            'items': [
                'Could benefit from more hands-on experience with Kubernetes in production',
                'Limited exposure to serverless architectures (AWS Lambda, Azure Functions)',
                'Could strengthen understanding of advanced async/await patterns'
            ]
        }
    },
    'overview': {
        'summary': 'John demonstrated excellent technical competency and communication skills throughout the interview. His responses showed depth of knowledge in Python development, system design, and problem-solving. He would be a strong addition to the team.',
        'metrics': [
            {'name': 'Communication', 'score': 4.2},
            {'name': 'Experience', 'score': 4.5},
            {'name': 'Confidence', 'score': 4.0},
            {'name': 'Domain Knowledge', 'score': 4.3},
            {'name': 'Resume Justification', 'score': 4.1},
            {'name': 'Skills', 'score': 4.4}
        ]
    },
    'mandatory': {
        'mandatory_skills': [
            {
                'skill_name': 'Python',
                'score': 4.5,
                'proficiency_level': 'Advanced',
                'evidence': 'Candidate stated: "I\'ve been working with Python for 7 years, primarily using Django and Flask frameworks. I\'ve built multiple microservices handling millions of requests daily."',
                'assessment': 'The candidate demonstrated expert-level knowledge of Python, including advanced concepts like decorators, context managers, and metaclasses. They provided detailed examples from their professional experience.',
                'demonstrated': True
            },
            {
                'skill_name': 'Django/Flask',
                'score': 4.4,
                'proficiency_level': 'Advanced',
                'evidence': 'Candidate explained: "I\'ve architected several Django applications with custom middleware, REST framework integrations, and optimized ORM queries."',
                'assessment': 'Strong understanding of both Django and Flask frameworks. The candidate showed expertise in building scalable web applications with proper architecture patterns.',
                'demonstrated': True
            },
            {
                'skill_name': 'REST APIs',
                'score': 4.2,
                'proficiency_level': 'Advanced',
                'evidence': 'Candidate described: "I follow RESTful principles strictly, implementing proper HTTP methods, status codes, and versioning strategies."',
                'assessment': 'Excellent grasp of RESTful API design principles. Demonstrated knowledge of best practices including pagination, filtering, and error handling.',
                'demonstrated': True
            },
            {
                'skill_name': 'SQL',
                'score': 4.1,
                'proficiency_level': 'Advanced',
                'evidence': 'Candidate stated: "I write complex SQL queries regularly and optimize them using EXPLAIN ANALYZE. I understand indexing strategies."',
                'assessment': 'Good SQL knowledge with practical experience in query optimization. Could deepen understanding of advanced database concepts like partitioning.',
                'demonstrated': True
            },
            {
                'skill_name': 'Docker',
                'score': 4.0,
                'proficiency_level': 'Intermediate',
                'evidence': 'Candidate mentioned: "All our services are containerized. I write Dockerfiles and use docker-compose for local development."',
                'assessment': 'Solid practical experience with Docker. Understands containerization concepts and has used it in production environments.',
                'demonstrated': True
            },
            {
                'skill_name': 'Git',
                'score': 4.4,
                'proficiency_level': 'Advanced',
                'evidence': 'Candidate explained: "I use Git daily with feature branch workflow, rebasing, and cherry-picking when needed."',
                'assessment': 'Excellent command of Git version control. Understands advanced concepts and follows best practices for collaboration.',
                'demonstrated': True
            },
            {
                'skill_name': 'Testing (pytest)',
                'score': 3.9,
                'proficiency_level': 'Intermediate',
                'evidence': 'Candidate said: "I write unit tests using pytest with fixtures and mocking. We maintain 80%+ code coverage."',
                'assessment': 'Good understanding of testing practices. Has practical experience with pytest but could expand knowledge of integration testing strategies.',
                'demonstrated': True
            },
            {
                'skill_name': 'CI/CD',
                'score': 3.8,
                'proficiency_level': 'Intermediate',
                'evidence': 'Candidate described: "We use GitHub Actions for CI/CD. I\'ve set up pipelines for testing, building, and deployment."',
                'assessment': 'Practical experience with CI/CD pipelines. Has set up basic automation but could deepen knowledge of advanced deployment strategies.',
                'demonstrated': True
            }
        ],
        'overall_mandatory_score': 4.2,
        'skills_met': 8,
        'total_skills': 8,
        'skills_gap_summary': 'The candidate successfully demonstrated all mandatory skills required for the Senior Python Developer role. They showed advanced proficiency in Python, web frameworks, and API design. While some areas like Docker and CI/CD are at intermediate level, their overall skill set is strong and well-aligned with the position requirements.'
    },
    'questions': {
        'list': [
            {
                'question': 'Can you explain the difference between a list and a tuple in Python, and when would you use each?',
                'answer': 'Lists are mutable sequences, meaning you can modify them after creation by adding, removing, or changing elements. Tuples are immutable - once created, they cannot be changed. I use lists when I need a collection that will be modified, like accumulating results. I use tuples for data that shouldn\'t change, like coordinates or database records, and as dictionary keys since they\'re hashable.',
                'analysis': 'Excellent answer demonstrating deep understanding. The candidate correctly identified the key difference (mutability) and provided practical use cases. They also mentioned an advanced concept (hashability) showing expert-level knowledge.',
                'overall_score': 4.5,
                'metrics': [
                    {'name': 'Communication', 'score': 4.5},
                    {'name': 'Experience', 'score': 4.5},
                    {'name': 'Confidence', 'score': 4.3},
                    {'name': 'Domain Knowledge', 'score': 4.7},
                    {'name': 'Skills', 'score': 4.5}
                ],
                'strengths': [
                    'Clear explanation of mutability',
                    'Excellent practical examples',
                    'Mentioned advanced hashability concept'
                ],
                'weaknesses': [],
                'flag': False
            },
            {
                'question': 'How would you optimize a slow database query in a Django application?',
                'answer': 'First, I\'d use Django Debug Toolbar to identify the problematic query. Then I\'d check for N+1 query issues and use select_related or prefetch_related. I\'d look at the query plan using EXPLAIN to see if indexes are being used. If needed, I\'d add database indexes on frequently filtered fields. For complex queries, I might use raw SQL or database views. Caching frequently accessed data with Redis is also an option.',
                'analysis': 'Outstanding comprehensive answer showing production experience. The candidate demonstrated a systematic debugging approach and mentioned multiple optimization techniques including ORM optimization, database indexing, and caching strategies.',
                'overall_score': 4.7,
                'metrics': [
                    {'name': 'Communication', 'score': 4.5},
                    {'name': 'Experience', 'score': 5.0},
                    {'name': 'Confidence', 'score': 4.5},
                    {'name': 'Domain Knowledge', 'score': 4.8},
                    {'name': 'Skills', 'score': 4.7}
                ],
                'strengths': [
                    'Systematic debugging approach',
                    'Multiple optimization techniques',
                    'Production-ready solutions',
                    'Mentioned specific Django tools'
                ],
                'weaknesses': [],
                'flag': False
            },
            {
                'question': 'Describe a challenging bug you encountered recently and how you solved it.',
                'answer': 'We had a race condition in our payment processing service causing duplicate charges. It only happened under high load. I reproduced it locally using locust for load testing. The issue was that we were checking payment status and creating charges in separate transactions. I fixed it by adding a unique constraint on the payment ID at the database level and wrapping the operations in a transaction with select_for_update to lock the row.',
                'analysis': 'Exceptional answer demonstrating senior-level problem-solving. The candidate described a production issue, explained the debugging process, identified the root cause (race condition), and implemented a robust solution using database constraints and row locking.',
                'overall_score': 4.8,
                'metrics': [
                    {'name': 'Communication', 'score': 4.7},
                    {'name': 'Experience', 'score': 5.0},
                    {'name': 'Confidence', 'score': 4.8},
                    {'name': 'Domain Knowledge', 'score': 4.9},
                    {'name': 'Skills', 'score': 4.8}
                ],
                'strengths': [
                    'Real production experience',
                    'Excellent debugging methodology',
                    'Advanced concurrency handling',
                    'Database-level solution'
                ],
                'weaknesses': [],
                'flag': False
            }
        ],
        'conversation_timeline': []
    },
    'json_final_analysis': {
        'final_score': 4.4
    },
    'footer': {
        'text': '| Generated by AIRA - AI Recruiting Agent | Confidential Report |'
    }
}

# Sample data for FAIL scenario
fail_data = {
    'header': {
        'title': 'Junior Machine Learning Engineer Interview - Sarah Johnson'
    },
    'candidate': {
        'name': 'Sarah Johnson',
        'email': 'sarah.j@email.com',
        'phone': '+1-555-0456',
        'photo_url': ''
    },
    'client': {
        'company': 'AI Innovations Ltd.',
        'position': 'Junior Machine Learning Engineer'
    },
    'interview': {
        'date': '2025-12-03',
        'time': '2:30 PM',
        'timezone': 'EST',
        'duration': '22 minutes',
        'questions_count': '6',
        'mode': 'Video',
        'room_id': 'ROOM-2025-1203-045'
    },
    'resume_analysis': {
        'score': 2.2,
        'recommendation': 'Resume shows limited practical experience. Most projects appear to be academic exercises with minimal production experience.'
    },
    'interview_analysis': {
        'score': 1.6,
        'recommendation': 'Candidate struggled with fundamental ML concepts and could not articulate practical experience. Interview ended early due to candidate withdrawing.'
    },
    'verdict': {
        'passed': False,
        'icon': '✗',
        'status': 'NOT RECOMMENDED',
        'subtitle': 'Does not meet minimum requirements',
        'strengths': {
            'title': 'Limited Strengths',
            'items': [
                'Showed enthusiasm for learning machine learning',
                'Has completed online courses in data science fundamentals',
                'Demonstrated basic understanding of statistical concepts from coursework'
            ]
        },
        'areas': {
            'title': 'Critical Gaps',
            'items': [
                'No practical experience with Python in professional or project settings',
                'Unable to explain basic ML algorithms or their applications',
                'No hands-on experience with essential ML libraries (scikit-learn, TensorFlow)',
                'Lack of understanding of data preprocessing and feature engineering',
                'No experience with version control or collaborative development',
                'Candidate terminated interview prematurely, indicating lack of preparation'
            ]
        }
    },
    'overview': {
        'summary': 'Sarah showed significant gaps in fundamental skills required for the Junior ML Engineer position. She was unable to answer basic questions about Python programming or machine learning concepts. The candidate abruptly ended the interview after 22 minutes, stating she was not prepared. This position requires hands-on technical skills that were not demonstrated.',
        'metrics': [
            {'name': 'Communication', 'score': 2.2},
            {'name': 'Experience', 'score': 0.8},
            {'name': 'Confidence', 'score': 1.5},
            {'name': 'Domain Knowledge', 'score': 1.0},
            {'name': 'Resume Justification', 'score': 1.8},
            {'name': 'Skills', 'score': 0.7}
        ]
    },
    'mandatory': {
        'mandatory_skills': [
            {
                'skill_name': 'Python',
                'score': 0.5,
                'proficiency_level': 'None',
                'evidence': 'Candidate stated: "I never did any projects in Python and Java."',
                'assessment': 'The candidate explicitly admitted to having no practical experience with Python, which is a critical requirement for the role. Their knowledge appears theoretical at best, and they failed to provide any relevant examples or applications of Python in a machine learning context.',
                'demonstrated': False
            },
            {
                'skill_name': 'pandas',
                'score': 0.0,
                'proficiency_level': 'None',
                'evidence': 'No mention of pandas or related data manipulation techniques.',
                'assessment': 'The candidate did not discuss pandas at all during the interview, indicating a complete lack of knowledge and experience with this essential library for data manipulation in Python.',
                'demonstrated': False
            },
            {
                'skill_name': 'NumPy',
                'score': 0.0,
                'proficiency_level': 'None',
                'evidence': 'No mention of NumPy or its applications.',
                'assessment': 'The candidate did not address NumPy during the interview, demonstrating no familiarity with this fundamental library for numerical computations in Python.',
                'demonstrated': False
            },
            {
                'skill_name': 'scikit-learn',
                'score': 0.0,
                'proficiency_level': 'None',
                'evidence': 'Candidate did not answer the question about implementing a baseline model using scikit-learn.',
                'assessment': 'The candidate\'s abrupt exit from the interview and lack of response to questions about scikit-learn indicate a total absence of knowledge and experience with this critical machine learning library.',
                'demonstrated': False
            },
            {
                'skill_name': 'TensorFlow/PyTorch',
                'score': 0.0,
                'proficiency_level': 'None',
                'evidence': 'No mention of TensorFlow, PyTorch, or related deep learning frameworks.',
                'assessment': 'The candidate did not discuss any deep learning frameworks, showing a complete lack of understanding and experience with tools essential for building ML models.',
                'demonstrated': False
            },
            {
                'skill_name': 'SQL',
                'score': 0.0,
                'proficiency_level': 'None',
                'evidence': 'No mention of SQL or database management.',
                'assessment': 'The candidate did not address SQL during the interview, indicating no familiarity with database querying, which is essential for handling data in machine learning projects.',
                'demonstrated': False
            },
            {
                'skill_name': 'Git',
                'score': 0.0,
                'proficiency_level': 'None',
                'evidence': 'No mention of Git or version control practices.',
                'assessment': 'The candidate did not discuss Git at all, indicating a lack of knowledge regarding version control, which is crucial for collaborative software development.',
                'demonstrated': False
            },
            {
                'skill_name': 'Linux',
                'score': 0.0,
                'proficiency_level': 'None',
                'evidence': 'No mention of Linux or command line usage.',
                'assessment': 'The candidate did not address Linux during the interview, showing no familiarity with the operating system commonly used in machine learning environments.',
                'demonstrated': False
            }
        ],
        'overall_mandatory_score': 0.1,
        'skills_met': 0,
        'total_skills': 8,
        'skills_gap_summary': 'The candidate demonstrated significant gaps across all mandatory skills required for the Junior Machine Learning Engineer role. They lack practical experience and knowledge in Python, data manipulation libraries, machine learning frameworks, SQL, Git, and Linux, which are essential for success in this position. The early termination of the interview further indicates a lack of preparation and readiness for this role.'
    },
    'questions': {
        'list': [
            {
                'question': 'Can you explain what a NumPy array is and how it differs from a Python list?',
                'answer': 'Um... I think NumPy is like... for numbers? And arrays are... like lists but different? I\'m not really sure.',
                'analysis': 'Very weak answer showing no understanding of NumPy. The candidate could not articulate the basic purpose or functionality of NumPy arrays, which is fundamental knowledge for any ML engineer role.',
                'overall_score': 0.5,
                'metrics': [
                    {'name': 'Communication', 'score': 1.5},
                    {'name': 'Experience', 'score': 0.0},
                    {'name': 'Confidence', 'score': 0.8},
                    {'name': 'Domain Knowledge', 'score': 0.3},
                    {'name': 'Skills', 'score': 0.0}
                ],
                'strengths': [],
                'weaknesses': [
                    'No understanding of NumPy basics',
                    'Unable to articulate differences from lists',
                    'Lack of confidence',
                    'No practical knowledge'
                ],
                'flag': False
            },
            {
                'question': 'Have you worked with any machine learning algorithms? Can you explain one?',
                'answer': 'I heard about them in my course. Linear regression, I think? It\'s like... drawing a line through points?',
                'analysis': 'Extremely superficial answer demonstrating only textbook awareness without any real understanding. The candidate cannot explain the algorithm, its purpose, or applications.',
                'overall_score': 0.8,
                'metrics': [
                    {'name': 'Communication', 'score': 1.8},
                    {'name': 'Experience', 'score': 0.5},
                    {'name': 'Confidence', 'score': 1.0},
                    {'name': 'Domain Knowledge', 'score': 0.7},
                    {'name': 'Skills', 'score': 0.5}
                ],
                'strengths': [],
                'weaknesses': [
                    'Only theoretical knowledge',
                    'Cannot explain algorithm properly',
                    'No practical experience',
                    'Vague understanding'
                ],
                'flag': False
            },
            {
                'question': 'Tell me about a machine learning project you\'ve worked on.',
                'answer': 'I... actually I haven\'t done any real projects. Just some homework assignments. I don\'t think I\'m ready for this interview. Can we stop?',
                'analysis': 'Candidate admitted to having no project experience and terminated the interview. This shows a critical lack of preparation and the absence of required practical experience for even a junior role.',
                'overall_score': 0.2,
                'metrics': [
                    {'name': 'Communication', 'score': 2.0},
                    {'name': 'Experience', 'score': 0.0},
                    {'name': 'Confidence', 'score': 0.0},
                    {'name': 'Domain Knowledge', 'score': 0.0},
                    {'name': 'Skills', 'score': 0.0}
                ],
                'strengths': [
                    'Honest about limitations'
                ],
                'weaknesses': [
                    'No project experience',
                    'Interview terminated early',
                    'Not prepared for role',
                    'Lack of confidence'
                ],
                'flag': False
            }
        ],
        'conversation_timeline': []
    },
    'json_final_analysis': {
        'final_score': 1.6
    },
    'footer': {
        'text': '| Generated by AIRA - AI Recruiting Agent | Confidential Report |'
    }
}

# Generate PASS reports
print("Generating PASS scenario reports...")
pass_summary = summary_template.render(**pass_data)
with open('sample_summary_pass.html', 'w') as f:
    f.write(pass_summary)
print("✓ Generated sample_summary_pass.html")

pass_detailed = detailed_template.render(**pass_data)
with open('sample_detailed_pass.html', 'w') as f:
    f.write(pass_detailed)
print("✓ Generated sample_detailed_pass.html")

# Generate FAIL reports
print("\nGenerating FAIL scenario reports...")
fail_summary = summary_template.render(**fail_data)
with open('sample_summary_fail.html', 'w') as f:
    f.write(fail_summary)
print("✓ Generated sample_summary_fail.html")

fail_detailed = detailed_template.render(**fail_data)
with open('sample_detailed_fail.html', 'w') as f:
    f.write(fail_detailed)
print("✓ Generated sample_detailed_fail.html")

print("\n" + "="*60)
print("Sample reports generated successfully!")
print("="*60)
print("\nPASS Scenario:")
print("  - sample_summary_pass.html (Summary Report)")
print("  - sample_detailed_pass.html (Detailed Report)")
print("\nFAIL Scenario:")
print("  - sample_summary_fail.html (Summary Report)")
print("  - sample_detailed_fail.html (Detailed Report)")
print("\nNote: All scores now use 0-5 scale with 2.5 as pass threshold:")
print("  🔴 Red (Fail): < 2.0")
print("  🟠 Orange (Borderline): 2.0 - 2.4")
print("  🟢 Green (Pass): ≥ 2.5")
