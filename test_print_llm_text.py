"""
Debug test: PRINT the FULL TEXT that gets sent to the LLM during PDF
superscript extraction.

Your console log only shows a truncated preview like:
    text_before: '1. Under earlier British Columbia legislation,1 ...'
    Match 1: score=0.870, text_end='...', after='...'

This test rebuilds the EXACT same prompt string that
`_llm_pick_superscript` sends to the model and prints it in full — so you can
see everything the LLM receives. It does NOT modify any of your existing code.

It monkeypatches `_llm_pick_superscript` ONLY inside this test process (your
source file is untouched) to print the prompt, then calls the real function so
the pipeline still works normally and the Excel is still saved.

Usage:
    python test_print_llm_text.py <docx_path> <pdf_path> [output_excel_path]

Example:
    python test_print_llm_text.py "C:\\path\\to\\file.docx" "C:\\path\\to\\file.pdf"
"""

import sys
import os

# Add project root to path so `canada_pipeline` imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import canada_pipeline.superscript_extraction_from_pdf as sup


def _rebuild_prompt(search_text, top_matches):
    """
    Rebuild the EXACT prompt string used by `_llm_pick_superscript`.
    Kept identical to the source so what you see here is what the LLM sees.
    """
    candidates_text = ""
    for i, match in enumerate(top_matches, 1):
        matched_tail = match['text_matched'][-80:]
        candidates_text += (
            f"\nMatch {i} (similarity: {match['score']:.3f}):\n"
            f"  Text ending at match: \"...{matched_tail}\"\n"
            f"  +20 chars after match: \"{match['text_after_20']}\"\n"
        )

    prompt = f"""You are identifying footnote superscript numbers from a PDF.

The DOCX track changes show that a footnote was inserted after this text:
"{search_text[-200:]}"

I searched for this text in the PDF and found {len(top_matches)} matching locations.
For each match, I show the end of the matched text and the 20 characters that come AFTER it in the PDF.
The footnote superscript number should appear in those 20 characters after the match.

{candidates_text}

Look at the "+20 chars after match" for each candidate. The superscript footnote number is a digit (e.g., 1, 2, 23) that appears right after the matching text.

Rules:
1. Pick the match whose text best corresponds to the DOCX text above.
2. From that match's "+20 chars after", extract the footnote number (the first digit/number you see).
3. If none match or no digit is found, reply "NONE".

Reply with ONLY the superscript number (e.g., "1", "5", "23") or "NONE".
"""
    return prompt


def run(docx_path, pdf_path, output_excel_path=None):
    if not os.path.exists(docx_path):
        print(f"❌ DOCX not found: {docx_path}")
        return
    if not os.path.exists(pdf_path):
        print(f"❌ PDF not found: {pdf_path}")
        return

    if output_excel_path is None:
        base = os.path.splitext(os.path.basename(docx_path))[0]
        output_dir = os.path.dirname(os.path.abspath(docx_path))
        output_excel_path = os.path.join(output_dir, f"{base}_superscript_debug.xlsx")

    counter = {"n": 0}
    original_pick = sup._llm_pick_superscript

    def printing_pick(search_text, top_matches, client, model_name=sup.MODEL_SONNET):
        counter["n"] += 1
        n = counter["n"]
        prompt = _rebuild_prompt(search_text, top_matches)

        print("\n" + "#" * 100)
        print(f"# FULL TEXT SENT TO LLM  —  footnote prompt #{n}")
        print(f"# (search_text = {len(search_text)} chars, {len(top_matches)} candidate matches)")
        print("#" * 100)
        print(prompt)
        print("-" * 100)

        # Call the real function so the pipeline behaves exactly as before
        answer = original_pick(search_text, top_matches, client, model_name)
        print(f">>> LLM ANSWER: {answer!r}")
        print("#" * 100 + "\n")
        return answer

    # Patch ONLY in this test process — your source file is not modified
    sup._llm_pick_superscript = printing_pick
    try:
        sup.process_excel(
            docx_path=docx_path,
            pdf_path=pdf_path,
            output_excel_path=output_excel_path,
        )
    finally:
        sup._llm_pick_superscript = original_pick

    print(f"\n✅ Printed {counter['n']} full LLM prompt(s) above.")
    print(f"📊 Excel saved → {output_excel_path}")


if __name__ == "__main__":
    # ── Option A: hardcode your paths here, then just run `python test_print_llm_text.py`
    DOCX_PATH = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test 17 (July 14, 2026)\CDN_OO_OW Ch 3\CR-Done_CDN_OO_OW Chapter 3.doc"   # e.g. r"C:\Users\C302473\Downloads\CDN_HW_CLC 1.docx"
    PDF_PATH  = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test 17 (July 14, 2026)\CDN_OO_OW Ch 3 - Copy\CR-Done_CDN_OO_OW Chapter 3.pdf"   # e.g. r"C:\Users\C302473\Downloads\CDN_HW_CLC 1.pdf"

    # ── Option B: pass them on the command line (overrides the hardcoded values):
    #     python test_print_llm_text.py "<docx>" "<pdf>" [output.xlsx]
    docx_path = sys.argv[1] if len(sys.argv) > 1 else DOCX_PATH
    pdf_path = sys.argv[2] if len(sys.argv) > 2 else PDF_PATH
    output_excel_path = sys.argv[3] if len(sys.argv) > 3 else None

    if not docx_path or not pdf_path:
        print("Provide a DOCX and PDF path.\n"
              "  - Either edit DOCX_PATH / PDF_PATH at the bottom of this file, or\n"
              "  - Run: python test_print_llm_text.py \"<docx>\" \"<pdf>\" [output.xlsx]")
        sys.exit(1)

    run(docx_path, pdf_path, output_excel_path)
