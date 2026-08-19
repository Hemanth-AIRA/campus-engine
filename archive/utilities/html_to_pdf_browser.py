#!/usr/bin/env python3
"""
Convert HTML to PDF using browser printing approach
This uses Playwright to render HTML in a real browser and print to PDF
"""

import os
import sys
import asyncio
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Playwright not installed. Installing...")
    os.system("pip install playwright")
    os.system("playwright install chromium")
    from playwright.async_api import async_playwright

async def html_to_pdf_browser(html_file_path, output_pdf_path=None, options=None):
    """
    Convert HTML to PDF using browser printing
    
    Args:
        html_file_path (str): Path to HTML file
        output_pdf_path (str): Output PDF path
        options (dict): PDF generation options
    
    Returns:
        str: Path to generated PDF
    """
    
    if not os.path.exists(html_file_path):
        raise FileNotFoundError(f"HTML file not found: {html_file_path}")
    
    if output_pdf_path is None:
        base_name = os.path.splitext(html_file_path)[0]
        output_pdf_path = f"{base_name}_browser.pdf"
    
    # Default PDF options
    default_options = {
        'format': 'A4',
        'margin': {
            'top': '0.5in',
            'right': '0.5in',
            'bottom': '0.5in',
            'left': '0.5in'
        },
        'print_background': True,
        'prefer_css_page_size': False,
        'landscape': False
    }
    
    if options:
        default_options.update(options)
    
    print(f"🌐 Converting HTML to PDF using browser rendering...")
    print(f"📥 Input:  {html_file_path}")
    print(f"📤 Output: {output_pdf_path}")
    
    async with async_playwright() as p:
        # Launch browser
        print("🚀 Launching browser...")
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        # Get absolute path for file URL
        html_abs_path = os.path.abspath(html_file_path)
        file_url = f"file://{html_abs_path}"
        
        print(f"📄 Loading HTML: {file_url}")
        
        # Load the HTML file
        await page.goto(file_url, wait_until='networkidle')
        
        # Wait a bit for any dynamic content to load
        await page.wait_for_timeout(2000)
        
        print("🖨️  Generating PDF...")
        
        # Generate PDF
        pdf_bytes = await page.pdf(**default_options)
        
        # Save PDF
        with open(output_pdf_path, 'wb') as f:
            f.write(pdf_bytes)
        
        await browser.close()
    
    print(f"✅ PDF generated successfully: {output_pdf_path}")
    
    # Check file size and basic validation
    if os.path.exists(output_pdf_path):
        file_size = os.path.getsize(output_pdf_path)
        print(f"📄 PDF file size: {file_size:,} bytes ({file_size/1024:.1f} KB)")
        
        # Quick content validation
        try:
            import fitz
            doc = fitz.open(output_pdf_path)
            print(f"📑 PDF pages: {len(doc)}")
            if len(doc) > 0:
                text = doc[0].get_text()
                print(f"📝 Text content: {len(text):,} characters")
                if len(text) > 100:
                    print(f"📖 Sample: {text[:150]}...")
            doc.close()
        except ImportError:
            print("📝 PyMuPDF not available for content validation")
        except Exception as e:
            print(f"📝 Content validation error: {e}")
    
    return output_pdf_path

def convert_html_to_pdf_sync(html_file_path, output_pdf_path=None, options=None):
    """
    Synchronous wrapper for the async HTML to PDF conversion
    """
    return asyncio.run(html_to_pdf_browser(html_file_path, output_pdf_path, options))

def main():
    """Main function for command line usage"""
    
    if len(sys.argv) < 2:
        print("Usage: python html_to_pdf_browser.py <html_file> [output_pdf] [--landscape] [--a3]")
        print("Example: python html_to_pdf_browser.py report.html")
        print("Example: python html_to_pdf_browser.py report.html report.pdf --landscape")
        sys.exit(1)
    
    html_file = sys.argv[1]
    output_pdf = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith('--') else None
    
    # Parse options
    options = {}
    if '--landscape' in sys.argv:
        options['landscape'] = True
        print("📐 Using landscape orientation")
    
    if '--a3' in sys.argv:
        options['format'] = 'A3'
        print("📏 Using A3 page size")
    
    if '--no-background' in sys.argv:
        options['print_background'] = False
        print("🎨 Disabled background printing")
    
    try:
        pdf_path = convert_html_to_pdf_sync(html_file, output_pdf, options)
        print(f"\n🎉 Conversion completed successfully!")
        print(f"📁 PDF saved as: {pdf_path}")
        
        # Open PDF location
        print(f"📂 PDF location: {os.path.abspath(pdf_path)}")
        
    except Exception as e:
        print(f"\n💥 Conversion failed: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
