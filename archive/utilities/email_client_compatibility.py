def sanitize_html_for_email_clients(html_content):
    """
    Sanitize HTML content for email client compatibility.
    Removes modern CSS that email clients don't support and replaces with basic CSS.
    """
    import re
    
    # Remove problematic CSS properties that email clients don't support
    email_html = html_content
    
    # Replace modern display properties with email-safe alternatives
    email_html = re.sub(r'display:\s*flex[^;]*;?', 'display: block;', email_html, flags=re.IGNORECASE)
    email_html = re.sub(r'display:\s*grid[^;]*;?', 'display: block;', email_html, flags=re.IGNORECASE)
    email_html = re.sub(r'display:\s*table[^;]*;?', 'display: block;', email_html, flags=re.IGNORECASE)
    email_html = re.sub(r'display:\s*table-cell[^;]*;?', 'display: inline-block; vertical-align: top;', email_html, flags=re.IGNORECASE)
    
    # Remove problematic CSS3 properties
    email_html = re.sub(r'box-shadow:[^;]*;?', '', email_html, flags=re.IGNORECASE)
    email_html = re.sub(r'text-shadow:[^;]*;?', '', email_html, flags=re.IGNORECASE)
    email_html = re.sub(r'transform:[^;]*;?', '', email_html, flags=re.IGNORECASE)
    email_html = re.sub(r'animation[^;]*;?', '', email_html, flags=re.IGNORECASE)
    email_html = re.sub(r'transition[^;]*;?', '', email_html, flags=re.IGNORECASE)
    email_html = re.sub(r'border-radius:[^;]*;?', '', email_html, flags=re.IGNORECASE)
    
    # Remove modern layout properties
    email_html = re.sub(r'justify-content:[^;]*;?', '', email_html, flags=re.IGNORECASE)
    email_html = re.sub(r'align-items:[^;]*;?', '', email_html, flags=re.IGNORECASE)
    email_html = re.sub(r'gap:[^;]*;?', '', email_html, flags=re.IGNORECASE)
    
    # Add email-client-compatible CSS
    email_compatible_css = """
    <style>
        /* Email client compatible CSS - no modern properties */
        body { 
            font-family: Arial, Helvetica, sans-serif !important; 
            margin: 0 !important; 
            padding: 20px !important; 
            color: #333333 !important; 
            line-height: 1.5 !important;
            font-size: 14px !important;
        }
        
        table { 
            width: 100% !important; 
            border-collapse: collapse !important; 
            margin: 10px 0 !important; 
        }
        
        td, th { 
            padding: 10px !important; 
            text-align: left !important; 
            border-bottom: 1px solid #dddddd !important; 
            vertical-align: top !important;
        }
        
        h1, h2, h3 { 
            color: #2d5aa0 !important; 
            margin: 20px 0 10px 0 !important; 
            font-weight: bold !important;
        }
        
        h1 { 
            font-size: 24px !important; 
            border-bottom: 2px solid #2d5aa0 !important; 
            padding-bottom: 10px !important; 
        }
        
        h2 { font-size: 20px !important; }
        h3 { font-size: 16px !important; }
        
        p { 
            margin: 10px 0 !important; 
            font-size: 14px !important;
        }
        
        ul { 
            margin: 10px 0 !important; 
            padding-left: 20px !important; 
        }
        
        li { 
            margin: 5px 0 !important; 
            font-size: 14px !important;
        }
        
        .container {
            max-width: 800px !important;
            margin: 0 auto !important;
            padding: 20px !important;
            border: 1px solid #dfe6ec !important;
        }
        
        .header {
            background-color: #0078d7 !important;
            color: #ffffff !important;
            padding: 20px !important;
            text-align: center !important;
        }
        
        .section {
            margin: 15px 0 !important;
            padding: 15px !important;
            border: 1px solid #dddddd !important;
            background-color: #ffffff !important;
        }
        
        .pass {
            background-color: #e6f8f1 !important;
            border-color: #4CAF50 !important;
        }
        
        .fail {
            background-color: #ffeaea !important;
            border-color: #f44336 !important;
        }
        
        .metric-item {
            margin: 10px 0 !important;
            padding: 5px !important;
            border-bottom: 1px solid #eeeeee !important;
        }
        
        .metric-name {
            font-weight: bold !important;
            color: #333333 !important;
            font-size: 14px !important;
        }
        
        .metric-score {
            color: #666666 !important;
            font-size: 14px !important;
            margin-left: 10px !important;
        }
        
        .footer {
            margin-top: 30px !important;
            padding: 15px !important;
            background-color: #f0f0f0 !important;
            text-align: center !important;
            font-size: 12px !important;
            color: #666666 !important;
        }
        
        /* Force all divs to display as block */
        div {
            display: block !important;
        }
        
        /* Remove any remaining modern CSS */
        * {
            box-sizing: border-box !important;
        }
    </style>
    """
    
    # Replace the existing style tag or add new one
    if '<style>' in email_html:
        # Replace existing style with email-compatible version
        email_html = re.sub(r'<style>.*?</style>', email_compatible_css, email_html, flags=re.DOTALL | re.IGNORECASE)
    elif '<head>' in email_html:
        email_html = email_html.replace('<head>', f'<head>{email_compatible_css}')
    elif '<html>' in email_html:
        email_html = email_html.replace('<html>', f'<html><head>{email_compatible_css}</head>')
    else:
        email_html = f'<html><head>{email_compatible_css}</head><body>{email_html}</body></html>'
    
    return email_html

# Test the function
if __name__ == "__main__":
    # Test with sample HTML
    test_html = """
    <div style="display: flex; box-shadow: 0 2px 4px rgba(0,0,0,0.1); border-radius: 5px;">
        <div style="display: table-cell; transform: scale(1.1);">Test content</div>
    </div>
    """
    
    result = sanitize_html_for_email_clients(test_html)
    print("Original HTML:")
    print(test_html)
    print("\nEmail-compatible HTML:")
    print(result)
