#!/usr/bin/env python3
"""
Convert HTML to PDF using wkhtmltopdf (browser-based rendering)
This provides much better CSS and layout support than WeasyPrint
"""

import os
import sys
import subprocess
import tempfile

def check_wkhtmltopdf():
    """Check if wkhtmltopdf is installed"""
    try:
        result = subprocess.run(['wkhtmltopdf', '--version'], 
                              capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ wkhtmltopdf found: {result.stdout.strip()}")
            return True
    except FileNotFoundError:
        pass
    
    print("❌ wkhtmltopdf not found")
    print("📦 Installing wkhtmltopdf...")
    
    # Try to install via homebrew on macOS
    try:
        subprocess.run(['brew', 'install', 'wkhtmltopdf'], check=True)
        print("✅ wkhtmltopdf installed successfully")
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("❌ Failed to install wkhtmltopdf via brew")
        print("💡 Please install manually:")
        print("   macOS: brew install wkhtmltopdf")
        print("   Ubuntu: sudo apt-get install wkhtmltopdf")
        print("   Or download from: https://wkhtmltopdf.org/downloads.html")
        return False

def convert_html_to_pdf_wkhtmltopdf(html_file_path, output_pdf_path=None, options=None):
    """
    Convert HTML to PDF using wkhtmltopdf
    
    Args:
        html_file_path (str): Path to HTML file
        output_pdf_path (str): Output PDF path
        options (dict): Additional options for wkhtmltopdf
    
    Returns:
        str: Path to generated PDF
    """
    
    if not os.path.exists(html_file_path):
        raise FileNotFoundError(f"HTML file not found: {html_file_path}")
    
    if not check_wkhtmltopdf():
        raise RuntimeError("wkhtmltopdf is required but not available")
    
    if output_pdf_path is None:
        base_name = os.path.splitext(html_file_path)[0]
        output_pdf_path = f"{base_name}_wkhtml.pdf"
    
    print(f"🌐 Converting HTML to PDF using wkhtmltopdf...")
    print(f"📥 Input:  {html_file_path}")
    print(f"📤 Output: {output_pdf_path}")
    
    # Default options for good quality PDF
    cmd = [
        'wkhtmltopdf',
        '--page-size', 'A4',
        '--margin-top', '0.5in',
        '--margin-right', '0.5in',
        '--margin-bottom', '0.5in',
        '--margin-left', '0.5in',
        '--encoding', 'UTF-8',
        '--print-media-type',
        '--enable-local-file-access',
        '--javascript-delay', '2000',  # Wait for JS to load
        '--no-stop-slow-scripts',
        '--debug-javascript',
        '--disable-smart-shrinking'
    ]
    
    # Add custom options if provided
    if options:
        for key, value in options.items():
            if isinstance(value, bool) and value:
                cmd.append(f'--{key}')
            elif value is not None:
                cmd.extend([f'--{key}', str(value)])
    
    # Add input and output files
    cmd.extend([os.path.abspath(html_file_path), os.path.abspath(output_pdf_path)])
    
    print(f"🔧 Command: {' '.join(cmd)}")
    
    try:
        # Run wkhtmltopdf
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        if result.returncode == 0:
            print("✅ PDF generated successfully")
            
            # Check file size
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
        else:
            print(f"❌ wkhtmltopdf failed with return code: {result.returncode}")
            print(f"STDOUT: {result.stdout}")
            print(f"STDERR: {result.stderr}")
            raise RuntimeError(f"wkhtmltopdf conversion failed")
            
    except subprocess.TimeoutExpired:
        print("❌ Conversion timed out")
        raise RuntimeError("Conversion timed out after 60 seconds")

def main():
    """Main function for command line usage"""
    
    if len(sys.argv) < 2:
        print("Usage: python html_to_pdf_wkhtml.py <html_file> [output_pdf] [options]")
        print("Example: python html_to_pdf_wkhtml.py report.html")
        print("Example: python html_to_pdf_wkhtml.py report.html --landscape")
        print("\nSupported options:")
        print("  --landscape          : Use landscape orientation")
        print("  --grayscale          : Generate grayscale PDF")
        print("  --low-quality        : Reduce file size")
        print("  --disable-javascript : Disable JavaScript")
        sys.exit(1)
    
    html_file = sys.argv[1]
    output_pdf = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith('--') else None
    
    # Parse additional options
    options = {}
    args = sys.argv[2:] if output_pdf else sys.argv[2:]
    
    if '--landscape' in args:
        options['orientation'] = 'Landscape'
        print("📐 Using landscape orientation")
    
    if '--grayscale' in args:
        options['grayscale'] = True
        print("🎨 Using grayscale")
    
    if '--low-quality' in args:
        options['lowquality'] = True
        print("📉 Using low quality for smaller file size")
    
    if '--disable-javascript' in args:
        options['disable-javascript'] = True
        print("🚫 JavaScript disabled")
    
    try:
        pdf_path = convert_html_to_pdf_wkhtmltopdf(html_file, output_pdf, options)
        print(f"\n🎉 Conversion completed successfully!")
        print(f"📁 PDF saved as: {pdf_path}")
        print(f"📂 Full path: {os.path.abspath(pdf_path)}")
        
    except Exception as e:
        print(f"\n💥 Conversion failed: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
