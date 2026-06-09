import pdfplumber

pdf_path = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\canada_testing_missed_files\CDN_HP_DBC\CDN_HP_DBC 9 - March 2026.pdf"

with pdfplumber.open(pdf_path) as pdf:
    for i, page in enumerate(pdf.pages[50:52]):
        print(f"\n{'='*60}")
        print(f"PAGE {i+1}")
        print(f"{'='*60}")
        print(page.extract_text())
