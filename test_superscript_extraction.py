"""
Test file for superscript extraction from DOCX + PDF.
Runs track changes extraction + footnote anchor enrichment + PDF superscript extraction
and saves result to Excel.

Usage:
    python test_superscript_extraction.py <docx_path> <pdf_path> [output_excel_path]

Example:
    python test_superscript_extraction.py "C:\path\to\file.docx" "C:\path\to\file.pdf"
    python test_superscript_extraction.py "C:\path\to\file.docx" "C:\path\to\file.pdf" "C:\path\to\output.xlsx"
"""

import sys
import os
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from canada_pipeline.track_changes_df import process_docx
from canada_pipeline.superscript_extraction_from_pdf import process_excel as process_superscripts
import pandas as pd


def run_superscript_extraction(docx_path, pdf_path, output_excel_path=None):
    """
    Extract track changes from DOCX, enrich footnote anchors,
    run PDF superscript extraction, and save debug Excel.
    """
    if not os.path.exists(docx_path):
        print(f"❌ File not found: {docx_path}")
        return None

    if not os.path.exists(pdf_path):
        print(f"❌ PDF not found: {pdf_path}")
        return None

    if output_excel_path is None:
        base = os.path.splitext(os.path.basename(docx_path))[0]
        output_dir = os.path.dirname(docx_path)
        output_excel_path = os.path.join(output_dir, f"{base}_superscript_debug.xlsx")

    print(f"\n{'='*80}")
    print(f"  Superscript Extraction Test (DOCX + PDF)")
    print(f"{'='*80}")
    print(f"  DOCX: {docx_path}")
    print(f"  PDF:  {pdf_path}")
    print(f"  Output: {output_excel_path}")
    print(f"{'='*80}\n")

    # Step 1: Extract track changes
    start = time.time()
    print("📄 Step 1: Extracting track changes from DOCX...")
    results = process_docx(docx_path)
    extractor_obj = results['extractor']
    print(f"   ✅ Done in {time.time() - start:.1f}s")

    # Step 2: Build before/after DataFrame
    start = time.time()
    print("\n📋 Step 2: Building before/after DataFrame...")
    result_with_ba = extractor_obj.build_before_after_df()
    print(f"   ✅ Done in {time.time() - start:.1f}s — {len(result_with_ba)} rows")

    # Step 3: Enrich footnote anchors (this is where text_before_superscript is populated)
    start = time.time()
    print("\n🔍 Step 3: Enriching footnote anchors (text_before_superscript)...")
    try:
        result_with_ba = extractor_obj.enrich_footnote_anchors()
        print(f"   ✅ Done in {time.time() - start:.1f}s")
    except RuntimeError as e:
        print(f"   ⚠️  Skipped: {e}")

    # Step 4: PDF superscript extraction
    start = time.time()
    print("\n📄 Step 4: Running PDF superscript extraction...")
    superscript_excel = output_excel_path.replace('.xlsx', '_filtered.xlsx')
    try:
        filtered_df, result_with_ba = process_superscripts(
            docx_path=docx_path,
            pdf_path=pdf_path,
            output_excel_path=superscript_excel,
            result_with_ba=result_with_ba
        )
        print(f"   ✅ Done in {time.time() - start:.1f}s")
        print(f"   Filtered footnotes Excel: {superscript_excel}")
    except Exception as e:
        print(f"   ❌ PDF superscript extraction failed: {e}")
        import traceback
        traceback.print_exc()
        filtered_df = None

    # Step 5: Show results summary
    fn_col = 'is_footnote_x' if 'is_footnote_x' in result_with_ba.columns else 'is_footnote'
    new_fn_rows = result_with_ba[
        (result_with_ba[fn_col] == True) &
        (result_with_ba.get('is_new_footnote', pd.Series(dtype=float)) == 1)
    ].drop_duplicates(subset=['superscript'], keep='first')

    print(f"\n📊 Summary of NEW footnotes:")
    print(f"   {'sup':<5} {'pdf_sup':<8} {'method':<20} {'last 60 of text_before_superscript'}")
    print(f"   {'-'*5} {'-'*8} {'-'*20} {'-'*50}")
    for _, row in new_fn_rows.iterrows():
        sup = int(row.get('superscript', 0))
        pdf_sup = row.get('pdf_superscript', '?')
        method = str(row.get('extraction_method', ''))[:20]
        tbs = str(row.get('text_before_superscript', ''))
        last_60 = tbs[-60:] if len(tbs) > 60 else tbs
        print(f"   {sup:<5} {str(pdf_sup):<8} {method:<20} ...{last_60}")

    # Step 6: Save full result to Excel
    print(f"\n💾 Step 6: Saving full result to Excel...")
    result_with_ba.to_excel(output_excel_path, index=False)
    print(f"   ✅ Saved: {output_excel_path}")

    return result_with_ba


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    docx_path = sys.argv[1]
    pdf_path = sys.argv[2]
    output_path = sys.argv[3] if len(sys.argv) > 3 else None

    run_superscript_extraction(docx_path, pdf_path, output_path)
