"""
Standalone validation test runner.

Runs the track-change validation independently (no GUI / no full pipeline) and
produces a validation Excel report whose ``xml_element_ids`` column is populated
from mapper.json — so the React DOCX viewer can navigate revisions purely by
WordProcessing ``w:id``.

How to run (from the workspace ROOT, so the package imports resolve):

    cd "c:\\projectdata\\xmlproject\\canadatrackchanges_react_fastapi - adding_image_cue for insertion_newvalidation"
    python -m test_validation_standalone

Edit the CONFIG block below to point at your own DOCX / images ZIP / final XML.
"""

import os
import re
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd

from canada_pipeline.exe_track_changes_1 import (
    run_validation_for_gui,
    _load_mapper_track_change_lookup,
    _load_mapper_xml_element_ids,
    _format_validation_excel_sheet,
)
from canada_pipeline.config import MODEL_OPUS


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — edit these paths for your test
# ─────────────────────────────────────────────────────────────────────────────
DOCX_PATH = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test 17 (July 14, 2026)\CDN_MC_LCCIC_CH 8\CDN_MA_LCCIC 8 2026-1 update.docx"

IMAGES_ZIP_PATH = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test 17 (July 14, 2026)\CDN_MC_LCCIC_CH 8\CDN_MA_LCCIC 8 2026-1 update_images.zip"

FINAL_XML_PATH = r"C:\projectdata\xmlproject\new_approach_working\testing_exe_ouputs\Test 17 (July 14, 2026)\CDN_MC_LCCIC_CH 8\Output\CDN_MA_LCCIC 8 (chapter and all children)_20260714_122113_final.pxml"

# Prompts folder (must contain prompt.txt and prompt_validation.txt)
PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "validation_prompts")

# Where to write outputs (validation_temp/, Excel report, extracted images)
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "standalone_validation_output")

# Anthropic model + parallelism
CLAUDE_MODEL = MODEL_OPUS
MAX_WORKERS = 5

# If True, delete an existing mapper.json so it is regenerated with fresh
# xml_element_ids (recommended after code changes to the id logic).
FORCE_REGENERATE_MAPPER = True
# ─────────────────────────────────────────────────────────────────────────────


def _extract_images_zip(zip_path: str, dest_dir: str) -> str:
    """Extract the images ZIP (images + PDF) into ``dest_dir`` and return it."""
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)
    # If the ZIP nested everything under a single top folder, flatten to that.
    entries = [os.path.join(dest_dir, e) for e in os.listdir(dest_dir)]
    subdirs = [e for e in entries if os.path.isdir(e)]
    files = [e for e in entries if os.path.isfile(e)]
    if len(subdirs) == 1 and not files:
        return subdirs[0]
    return dest_dir


def _rebuild_excel_with_ids(temp_dir: Path, output_dir: str) -> str:
    """
    Rebuild the validation Excel from the validation JSON + mapper.json, adding
    the ``Track changes`` (full revision text) and ``xml_element_ids`` columns.

    Returns the path to the written Excel file.
    """
    json_file_path = temp_dir / "track_change_validation_results.json"
    content_drops_path = temp_dir / "content_drops.json"
    mapper_path = str(temp_dir / "mapper.json")

    if not json_file_path.exists():
        raise FileNotFoundError(f"Validation results JSON not found at {json_file_path}")

    with open(json_file_path, "r", encoding="utf-8") as f:
        val_data = json.load(f)
    df = pd.DataFrame(val_data)

    # Populate 'Track changes' (full revision text) and 'xml_element_ids' from mapper.json
    mapper_lookup = _load_mapper_track_change_lookup(mapper_path)
    xml_ids_lookup = _load_mapper_xml_element_ids(mapper_path)

    if mapper_lookup and not df.empty:
        def _resolve_track_change(row):
            key = (str(row.get("type", "")).strip(), str(row.get("segment_id", "")).strip())
            return mapper_lookup.get(key, row.get("Track changes", ""))
        df["Track changes"] = df.apply(_resolve_track_change, axis=1)
        print(f"  Populated 'Track changes' from mapper.json ({len(mapper_lookup)} entries)")

    if xml_ids_lookup and not df.empty:
        def _resolve_xml_ids(row):
            key = (str(row.get("type", "")).strip(), str(row.get("segment_id", "")).strip())
            return json.dumps(xml_ids_lookup.get(key, []))
        df["xml_element_ids"] = df.apply(_resolve_xml_ids, axis=1)
        non_empty = sum(1 for v in df["xml_element_ids"] if v not in ("[]", "", None))
        print(f"  Populated 'xml_element_ids' from mapper.json ({non_empty}/{len(df)} rows have ids)")
    else:
        print("  [WARN] mapper.json produced NO xml_element_ids — navigation will fall back to text matching!")

    timestamp = datetime.now().strftime("%H-%M_%d_%m_%Y")
    excel_path = os.path.join(output_dir, f"final_validation_report_WITH_IDS_{timestamp}.xlsx")

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Validation Results")
        ws = writer.sheets["Validation Results"]
        try:
            _format_validation_excel_sheet(ws, df)
        except Exception as e:
            print(f"  [WARN] Sheet formatting skipped: {e}")

        if content_drops_path.exists():
            with open(content_drops_path, "r", encoding="utf-8") as f:
                drops_data = json.load(f)
            if drops_data:
                pd.DataFrame(drops_data).to_excel(writer, index=False, sheet_name="Content Drops")

    return excel_path


def main():
    print("=" * 70)
    print("STANDALONE VALIDATION TEST")
    print("=" * 70)

    # Basic sanity checks
    for label, path in [
        ("DOCX", DOCX_PATH),
        ("Images ZIP", IMAGES_ZIP_PATH),
        ("Final XML", FINAL_XML_PATH),
    ]:
        if not os.path.exists(path):
            raise FileNotFoundError(f"{label} not found: {path}")

    prompt_path = os.path.join(PROMPTS_DIR, "prompt.txt")
    validation_prompt_path = os.path.join(PROMPTS_DIR, "prompt_validation.txt")
    for label, path in [("prompt.txt", prompt_path), ("prompt_validation.txt", validation_prompt_path)]:
        if not os.path.exists(path):
            raise FileNotFoundError(f"{label} not found in {PROMPTS_DIR}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 1. Extract the images ZIP (contains images + PDF)
    images_extract_dir = os.path.join(OUTPUT_DIR, "images_from_zip")
    print(f"\n[1] Extracting images ZIP → {images_extract_dir}")
    images_folder = _extract_images_zip(IMAGES_ZIP_PATH, images_extract_dir)
    print(f"    Images folder: {images_folder}")

    # 2. Optionally force mapper.json regeneration (fresh xml_element_ids)
    temp_dir = Path(OUTPUT_DIR) / "validation_temp"
    mapper_json = temp_dir / "mapper.json"
    if FORCE_REGENERATE_MAPPER and mapper_json.exists():
        print(f"\n[2] Removing stale mapper.json to force regeneration: {mapper_json}")
        mapper_json.unlink()

    # 3. Run validation (generates mapper.json + validation JSON + a base Excel)
    print("\n[3] Running validation…")
    run_validation_for_gui(
        docx_path=DOCX_PATH,
        final_xml_path=FINAL_XML_PATH,
        output_dir=OUTPUT_DIR,
        prompt_path=prompt_path,
        validation_prompt_path=validation_prompt_path,
        claude_api_key=None,          # auto-resolved via TR token endpoint / env
        claude_model=CLAUDE_MODEL,
        max_workers=MAX_WORKERS,
        images_folder=images_folder,
    )

    # 4. Rebuild the Excel with the xml_element_ids column (for ID-based nav)
    print("\n[4] Rebuilding Excel with 'xml_element_ids' column…")
    excel_path = _rebuild_excel_with_ids(temp_dir, OUTPUT_DIR)

    print("\n" + "=" * 70)
    print(f"DONE. Upload this Excel to the React viewer:\n  {excel_path}")
    print(f"mapper.json (with ids): {mapper_json}")
    print("=" * 70)


if __name__ == "__main__":
    main()
