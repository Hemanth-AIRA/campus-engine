# AIRA Interview Report Template System

A dynamic HTML template system for generating AIRA interview reports from JSON data using Jinja2.

## 🚀 Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements_template.txt
```

### 2. Generate a Report

```bash
python generate_report.py aira_report_template.html.j2 sample_data.json output_report.html
```

### 3. View the Report

Open `output_report.html` in your browser to see the generated report.

## 📁 Files Structure

```
├── aira_report_template.html.j2    # Jinja2 HTML template
├── sample_data.json               # Sample JSON data structure
├── generate_report.py             # Python script to generate reports
├── requirements_template.txt      # Python dependencies
└── README_template.md            # This documentation
```

## 📊 JSON Data Structure

The template expects a JSON object with the following structure:

### Header Section

```json
{
  "header": {
    "title": "AIRA",
    "subtitle": "Your AI Hiring Agent<br>Autonomous Interview & Resume Analysis",
    "logo_url": "https://via.placeholder.com/80x80.png?text=AIRA"
  }
}
```

### Candidate Information

```json
{
  "candidate": {
    "name": "John Doe",
    "email": "john.doe@example.com",
    "phone": "+1234567890",
    "photo_url": "https://via.placeholder.com/80"
  }
}
```

### Client Information

```json
{
  "client": {
    "company": "Tech Corp Inc.",
    "position": "Senior Developer"
  }
}
```

### Interview Details

```json
{
  "interview": {
    "date": "2025-08-15",
    "time": "2:00 PM",
    "timezone": "UTC",
    "duration": "45 minutes",
    "questions_count": 10,
    "mode": "Video Call"
  }
}
```

### Analysis Scores

```json
{
  "resume_analysis": {
    "score": 85,
    "recommendation": "Strong technical background..."
  },
  "interview_analysis": {
    "score": 78,
    "recommendation": "Good communication skills..."
  }
}
```

### Interview Verdict

```json
{
  "verdict": {
    "passed": true,
    "icon": "✓",
    "status": "PASSED INTERVIEW ROUND",
    "subtitle": "Candidate cleared this round",
    "strengths": {
      "title": "✓ Key Strengths",
      "items": ["Strong technical skills", "Good communication"]
    },
    "areas": {
      "title": "⚠ Areas for Development",
      "items": ["Could improve in X", "Needs work on Y"]
    },
    "overall_assessment": "Detailed assessment text..."
  }
}
```

### Performance Overview

```json
{
  "overview": {
    "metrics": [
      { "name": "Communication", "score": 81, "color": "#76c7c0" },
      { "name": "Experience", "score": 84, "color": "#67b26f" }
    ],
    "analysis": [
      {
        "name": "Communication",
        "score": 81,
        "color": "#76c7c0",
        "description": "Clear articulation with good vocabulary"
      }
    ]
  }
}
```

### Questions and Responses

```json
{
  "questions": {
    "header": {
      "title": "Detailed Analysis Report",
      "subtitle": "Question-by-Question Assessment",
      "description": "Comprehensive analysis of responses"
    },
    "list": [
      {
        "id": 1,
        "question": "Explain your approach to...",
        "answer": "My approach would be...",
        "analysis": "Good structured response...",
        "overall_score": 85,
        "metrics": [
          { "name": "Communication", "score": 86, "color": "#76c7c0" }
        ],
        "strengths": ["Clear explanation", "Good examples"],
        "weaknesses": ["Could be more specific"]
      }
    ]
  }
}
```

## 🎨 Dynamic Verdict Styling

The template automatically applies different styling based on the `verdict.passed` boolean:

### For Passed Interviews (`"passed": true`)

- Green checkmark icon (✓)
- Green status text color
- Green-tinted analysis background
- "Key Strengths" and "Areas for Development" sections

### For Failed Interviews (`"passed": false`)

- Red X icon (✗)
- Red status text color
- Red-tinted analysis background
- "Limited Strengths" and "Critical Areas of Concern" sections

## 🔧 Customization

### Adding New Sections

1. Add the data structure to your JSON
2. Add the corresponding template code in the `.html.j2` file
3. Update the CSS if needed

### Changing Colors/Styling

- Modify the CSS variables in the `:root` section
- Add new CSS classes for custom styling
- Use conditional Jinja2 classes for dynamic styling

### Custom Metrics

Add new metrics to the `overview.metrics` and `questions.list[].metrics` arrays:

```json
{
  "name": "New Metric",
  "score": 75,
  "color": "#ff6b6b"
}
```

## 📱 PDF Generation (Optional)

To generate PDF reports, you can use tools like:

- **WeasyPrint**: `pip install weasyprint`

  ```python
  from weasyprint import HTML
  HTML(filename='output_report.html').write_pdf('report.pdf')
  ```

- **Puppeteer** (Node.js): For more advanced PDF features

## 🚨 Error Handling

The generator script includes error handling for:

- Missing template or data files
- Invalid JSON format
- Template rendering errors
- File permission issues

## 🔄 Integration Example

```python
import json
from generate_report import generate_report

# Your data from database/API
interview_data = {
    "candidate": get_candidate_data(candidate_id),
    "verdict": analyze_interview(interview_id),
    # ... other data
}

# Save to temporary JSON file
with open('temp_data.json', 'w') as f:
    json.dump(interview_data, f)

# Generate report
generate_report(
    'aira_report_template.html.j2',
    'temp_data.json',
    f'report_{interview_id}.html'
)
```

## 📝 Notes

- All styling is embedded in the HTML for easy PDF conversion
- Template uses Jinja2 loops and conditionals for dynamic content
- Responsive design works on desktop and mobile
- Colors and metrics are customizable through the JSON data
- Template is self-contained with no external dependencies

## 🆘 Support

If you encounter issues:

1. Check that all required fields are present in your JSON
2. Validate your JSON syntax
3. Ensure template file paths are correct
4. Check Python error messages for specific issues
