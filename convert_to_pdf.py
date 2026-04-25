"""
Convert Markdown Report to PDF using fpdf2
Standalone converter script
"""
from fpdf import FPDF
import re
import os
import unicodedata

class MarkdownPDF(FPDF):
    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=True, margin=10)
        self.set_font("Helvetica", size=10)
        
    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 10, f"Page {self.page_no}", align="C")

def clean_text(text):
    """Remove or replace Unicode characters that aren't supported by Helvetica font"""
    # Replace Unicode arrows and symbols with text equivalents
    replacements = {
        '↓': 'v',
        '↑': '^',
        '→': '->',
        '←': '<-',
        '✓': '[OK]',
        '✗': '[X]',
        '•': '-',
        '●': 'o',
        '○': 'o',
        '◆': '[+]',
        '★': '*',
        '™': '(TM)',
        '©': '(C)',
        '®': '(R)',
    }
    
    for unicode_char, replacement in replacements.items():
        text = text.replace(unicode_char, replacement)
    
    # Remove any remaining non-ASCII characters
    text = unicodedata.normalize('NFKD', text)
    text = text.encode('ascii', 'ignore').decode('ascii')
    
    return text

md_file = 'ANTI_JAMMING_TECHNIQUES_REPORT.md'
pdf_file = 'ANTI_JAMMING_TECHNIQUES_REPORT.pdf'

print("[1/3] Reading markdown file...")
with open(md_file, 'r', encoding='utf-8') as f:
    content = f.read()

print("[2/3] Creating PDF...")
pdf = MarkdownPDF()
pdf.add_page()

lines = content.split('\n')

for line in lines:
    line = line.strip()
    
    if not line:
        pdf.ln(2)
        continue
    
    # Title
    if line.startswith('# ') and not line.startswith('##'):
        title = clean_text(line.replace('# ', '').strip())
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(0, 51, 102)
        pdf.cell(0, 12, title, ln=True, align='C')
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", size=10)
        pdf.ln(3)
        
    # Heading 1
    elif line.startswith('## ') and not line.startswith('###'):
        heading = clean_text(line.replace('## ', '').strip())
        if pdf.get_y() > 250:
            pdf.add_page()
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_text_color(0, 51, 102)
        pdf.cell(0, 10, heading, ln=True)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", size=10)
        pdf.ln(2)
        
    # Heading 2
    elif line.startswith('### '):
        heading = clean_text(line.replace('### ', '').strip())
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(0, 85, 128)
        pdf.cell(0, 9, heading, ln=True)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", size=10)
        pdf.ln(1)
        
    # Skip table lines
    elif line.startswith('|') or re.match(r'^-{3,}', line):
        continue
        
    # Regular text
    elif line and not line.startswith('```'):
        text = clean_text(line)
        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
        text = re.sub(r'__(.*?)__', r'\1', text)
        text = re.sub(r'`(.*?)`', r'\1', text)
        text = re.sub(r'\[(.*?)\]\([^\)]*\)', r'\1', text)
        
        if pdf.get_y() > 270:
            pdf.add_page()
        
        pdf.set_font("Helvetica", "", 10)
        if len(text) > 200:
            pdf.multi_cell(0, 4, text)
        else:
            pdf.cell(0, 5, text[:190], ln=True)

print("[3/3] Saving PDF...")
pdf.output(pdf_file)

file_size = os.path.getsize(pdf_file)
print(f"\n✓ SUCCESS! PDF created:")
print(f"  • Filename: {pdf_file}")
print(f"  • Size: {file_size / 1024:.1f} KB")
print(f"  • Pages: {pdf.page}")
print(f"  • Location: c:\\Users\\hp\\FYP-5G-Pipeline\\{pdf_file}")
