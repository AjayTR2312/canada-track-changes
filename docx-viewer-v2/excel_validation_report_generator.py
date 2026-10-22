from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

try:
    import pandas as pd
except Exception:
    pd = None


NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DEFAULT_CLAUDE_MODEL = "claude-opus-4-6"


def _load_config_runtime() -> tuple[str, Any]:
    from config import MODEL_OPUS, build_anthropic_client

    return MODEL_OPUS, build_anthropic_client


def _get_default_model_from_config() -> str:
    try:
        model_name, _ = _load_config_runtime()
        return model_name or DEFAULT_CLAUDE_MODEL
    except Exception:
        return DEFAULT_CLAUDE_MODEL


def _load_validation_runtime() -> tuple[Any, Any, Any]:
    from exe_track_changes_1 import GPTProcessor, _format_validation_excel_sheet, run_outp_validation

    return GPTProcessor, _format_validation_excel_sheet, run_outp_validation


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.lower() == "nan":
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _column_letters_to_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return max(index - 1, 0)


def _read_xlsx_records_fallback(excel_path: str) -> list[dict[str, Any]]:
    with zipfile.ZipFile(excel_path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in shared_root.findall(f"{{{NS_MAIN}}}si"):
                shared_strings.append("".join(node.text or "" for node in item.iter(f"{{{NS_MAIN}}}t")))

        sheets_node = workbook.find(f"{{{NS_MAIN}}}sheets")
        if sheets_node is None or not list(sheets_node):
            return []

        first_sheet = list(sheets_node)[0]
        rel_id = first_sheet.attrib.get(f"{{{NS_REL}}}id")
        target = rel_map[rel_id]
        if not target.startswith("xl/"):
            target = f"xl/{target}"

        sheet_root = ET.fromstring(archive.read(target))
        parsed_rows: list[dict[int, str]] = []
        max_col = -1

        for row in sheet_root.iter(f"{{{NS_MAIN}}}row"):
            row_values: dict[int, str] = {}
            for cell in row.findall(f"{{{NS_MAIN}}}c"):
                cell_ref = cell.attrib.get("r", "A1")
                col_idx = _column_letters_to_index(cell_ref)
                cell_type = cell.attrib.get("t")
                value_node = cell.find(f"{{{NS_MAIN}}}v")
                inline_node = cell.find(f"{{{NS_MAIN}}}is")
                cell_value = ""

                if cell_type == "s" and value_node is not None and value_node.text is not None:
                    shared_index = int(value_node.text)
                    if 0 <= shared_index < len(shared_strings):
                        cell_value = shared_strings[shared_index]
                elif cell_type == "inlineStr" and inline_node is not None:
                    cell_value = "".join(node.text or "" for node in inline_node.iter(f"{{{NS_MAIN}}}t"))
                elif cell_type == "b" and value_node is not None:
                    cell_value = "TRUE" if value_node.text == "1" else "FALSE"
                elif value_node is not None and value_node.text is not None:
                    cell_value = value_node.text

                row_values[col_idx] = cell_value
                max_col = max(max_col, col_idx)

            if row_values:
                parsed_rows.append(row_values)

        if not parsed_rows:
            return []

        header_row = parsed_rows[0]
        headers = [_clean_text(header_row.get(idx, "")) for idx in range(max_col + 1)]
        records: list[dict[str, Any]] = []

        for row_values in parsed_rows[1:]:
            record: dict[str, Any] = {}
            for idx, header in enumerate(headers):
                if header:
                    record[header] = row_values.get(idx, "")
            if any(_clean_text(value) for value in record.values()):
                records.append(record)

        return records


def read_excel_records(excel_path: str) -> list[dict[str, Any]]:
    if pd is not None:
        dataframe = pd.read_excel(excel_path)
        dataframe = dataframe.where(dataframe.notna(), "")
        return dataframe.to_dict(orient="records")
    return _read_xlsx_records_fallback(excel_path)


def _strip_xml_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def _extract_section_anchor_from_xml(xml_text: str, section_value: str) -> str:
    section_value = _clean_text(section_value)
    if not xml_text or not section_value:
        return ""

    escaped = re.escape(section_value)
    patterns = [
        rf"<section\b[^>]*>.*?<designator[^>]*>\s*{escaped}\s*</designator>.*?</section>",
        rf"<editorial\.commentary\.section\b[^>]*>.*?<designator[^>]*>\s*{escaped}\s*</designator>.*?</editorial\.commentary\.section>",
    ]
    for pattern in patterns:
        match = re.search(pattern, xml_text, flags=re.DOTALL | re.IGNORECASE)
        if match:
            return _strip_xml_tags(match.group(0))[:1500]
    return ""


def _canonical_type(source_type: str) -> str:
    lowered = _clean_text(source_type).lower()
    if "deletion" in lowered:
        return "deletion"
    if "insertion" in lowered:
        return "insertion"
    if "replacement" in lowered:
        return "replacement"
    return ""


def _build_anchor_text(row: dict[str, Any], prevendor_xml_text: str = "") -> str:
    paragraph_text = _clean_text(row.get("paragraph_text"))
    prev_context = _clean_text(row.get("prev_context"))
    post_context = _clean_text(row.get("post_context"))
    previous_para = _clean_text(row.get("previous_para"))
    post_para = _clean_text(row.get("post_para"))
    section_value = _clean_text(row.get("section"))

    for candidate in (
        paragraph_text,
        " ".join(part for part in (prev_context, post_context) if part),
        " ".join(part for part in (previous_para, post_para) if part),
        previous_para,
        post_para,
    ):
        candidate = _clean_text(candidate)
        if candidate:
            return candidate

    return _extract_section_anchor_from_xml(prevendor_xml_text, section_value)


def _base_mapper_entry(row: dict[str, Any], row_number: int, segment_id: str, entry_type: str, anchor_text: str) -> dict[str, Any]:
    track_change = _clean_text(row.get("track_change"))
    return {
        "Track Change Segment": segment_id,
        "type": entry_type,
        "Track changes": "",
        "Deleted Content": "",
        "Previous Content": anchor_text,
        "section": _clean_text(row.get("section")),
        "paragraph_index": _clean_text(row.get("paragraph_index")),
        "track_change": track_change,
        "source_excel_type": _clean_text(row.get("type")),
        "deleted_text": _clean_text(row.get("deleted_text")),
        "inserted_text": _clean_text(row.get("inserted_text")),
        "prev_context": _clean_text(row.get("prev_context")),
        "post_context": _clean_text(row.get("post_context")),
        "paragraph_text": _clean_text(row.get("paragraph_text")),
        "previous_para": _clean_text(row.get("previous_para")),
        "post_para": _clean_text(row.get("post_para")),
        "author": _clean_text(row.get("author")),
        "date": _clean_text(row.get("date")),
        "has_bullet_point": _clean_text(row.get("has_bullet_point")),
        "idx": _clean_text(row.get("idx")),
        "revision_order": _clean_text(row.get("revision_order")) or str(row_number),
        "source_row_number": row_number,
    }


def convert_excel_rows_to_mapper(rows: list[dict[str, Any]], prevendor_xml_text: str = "") -> list[dict[str, Any]]:
    mapper_entries: list[dict[str, Any]] = []

    for row_number, row in enumerate(rows, start=2):
        entry_type = _canonical_type(row.get("type", ""))
        if not entry_type:
            continue

        base_segment = _clean_text(row.get("revision_order")) or _clean_text(row.get("idx")) or str(row_number)
        anchor_text = _build_anchor_text(row, prevendor_xml_text=prevendor_xml_text)
        deleted_text = _clean_text(row.get("deleted_text")) or _clean_text(row.get("track_change"))
        inserted_text = _clean_text(row.get("inserted_text")) or _clean_text(row.get("track_change"))

        if entry_type == "replacement":
            if deleted_text:
                deletion_entry = _base_mapper_entry(row, row_number, f"{base_segment}-del", "deletion", anchor_text)
                deletion_entry["Deleted Content"] = deleted_text
                mapper_entries.append(deletion_entry)
            if inserted_text:
                insertion_entry = _base_mapper_entry(row, row_number, f"{base_segment}-ins", "insertion", anchor_text)
                insertion_entry["Track changes"] = inserted_text
                mapper_entries.append(insertion_entry)
            continue

        mapper_entry = _base_mapper_entry(row, row_number, base_segment, entry_type, anchor_text)
        if entry_type == "deletion":
            mapper_entry["Deleted Content"] = deleted_text
        else:
            mapper_entry["Track changes"] = inserted_text
        mapper_entries.append(mapper_entry)

    return mapper_entries


def _build_enriched_anchor_text(row: dict[str, Any]) -> str:
    """Build a much richer anchor text from enriched dataframe rows.

    The enriched dataframe from dataframe_enricher guarantees populated fields
    (paragraph_text, previous_para, post_para, prev_context, post_context).
    We combine multiple fields for a stronger, more unique fingerprint that
    helps locate the correct page and PXML chunk.
    """
    paragraph_text = _clean_text(row.get("paragraph_text"))
    prev_context = _clean_text(row.get("prev_context"))
    post_context = _clean_text(row.get("post_context"))
    previous_para = _clean_text(row.get("previous_para"))
    post_para = _clean_text(row.get("post_para"))

    # Strategy: combine as much context as possible for a unique anchor
    # Priority 1: full paragraph text (best single-field anchor)
    # Priority 2: paragraph + neighbors for uniqueness
    # Priority 3: context fragments

    parts = []

    # Always include paragraph text if available — it's the primary anchor
    if paragraph_text:
        parts.append(paragraph_text)

    # Add neighboring paragraphs for disambiguation (enricher guarantees these
    # are closest non-empty, non-footnote paragraphs)
    if previous_para:
        parts.append(previous_para)
    if post_para:
        parts.append(post_para)

    if parts:
        return " ".join(parts)

    # Fallback: use inline context fragments
    if prev_context or post_context:
        return " ".join(part for part in (prev_context, post_context) if part)

    return ""


def _canonical_enriched_type(source_type: str) -> str:
    """Map dataframe_enricher type names to validation mapper types."""
    lowered = _clean_text(source_type).lower()
    if "deletion" in lowered:
        return "deletion"
    if "insertion" in lowered or "addition" in lowered:
        return "insertion"
    if "replacement" in lowered:
        return "replacement"
    return ""


def convert_enriched_df_to_mapper(revisions_df, pxml_content: str = "") -> list[dict[str, Any]]:
    """Convert the enriched dataframe from dataframe_enricher to validation mapper entries.

    This produces the same mapper format as convert_excel_rows_to_mapper but
    leverages the richer, guaranteed-populated fields from the enriched dataframe.
    """
    if revisions_df is None or (hasattr(revisions_df, 'empty') and revisions_df.empty):
        return []

    # Convert dataframe to list of dicts if needed
    if hasattr(revisions_df, 'to_dict'):
        rows = revisions_df.to_dict(orient="records")
    else:
        rows = list(revisions_df)

    mapper_entries: list[dict[str, Any]] = []

    for row_number, row in enumerate(rows, start=1):
        entry_type = _canonical_enriched_type(str(row.get("type", "")))
        if not entry_type:
            continue

        base_segment = (
            _clean_text(row.get("revision_order"))
            or _clean_text(row.get("idx"))
            or str(row_number)
        )

        # Build a rich anchor using the enriched fields
        anchor_text = _build_enriched_anchor_text(row)

        # If anchor is still empty, try extracting from PXML by section
        if not anchor_text and pxml_content:
            section_value = _clean_text(row.get("section"))
            anchor_text = _extract_section_anchor_from_xml(pxml_content, section_value)

        deleted_text = _clean_text(row.get("deleted_text")) or _clean_text(row.get("track_change"))
        inserted_text = _clean_text(row.get("inserted_text")) or _clean_text(row.get("track_change"))

        # Replacements were already split into deletion+insertion rows by dataframe_enricher
        # so we just handle each row as its canonical type
        mapper_entry = _base_mapper_entry(row, row_number, base_segment, entry_type, anchor_text)

        if entry_type == "deletion":
            mapper_entry["Deleted Content"] = deleted_text
        else:
            mapper_entry["Track changes"] = inserted_text

        mapper_entries.append(mapper_entry)

    return mapper_entries


def _resolve_api_key(claude_model: str, claude_api_key: str | None) -> str:
    if claude_api_key:
        return claude_api_key
    try:
        _, build_anthropic_client = _load_config_runtime()
        client = build_anthropic_client(claude_model)
        return client.api_key
    except Exception:
        return os.environ.get("ANTHROPIC_API_KEY", "")


def _prepare_images(images_dir: Path, images_folder: str) -> None:
    """Copy revision images (and PDF if present) from the uploaded folder into images_dir.

    Walks the folder recursively so that images inside nested subdirectories
    (common when a ZIP is extracted) are still found.
    No DOCX-to-images conversion is attempted — the caller must supply a folder
    that already contains PNGs (and optionally a PDF for page-text extraction).
    """
    if not images_folder or not os.path.isdir(images_folder):
        raise RuntimeError(
            "images_folder is required and must contain the revision page images (PNGs). "
            "Upload a ZIP with images or point to the folder containing them."
        )

    # Already populated from a previous run
    if any(images_dir.glob("*.png")):
        return

    valid_extensions = (".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".pdf")
    source_files: list[str] = []
    for dirpath, _, filenames in os.walk(images_folder):
        for fname in filenames:
            if fname.lower().endswith(valid_extensions):
                source_files.append(os.path.join(dirpath, fname))

    if not source_files:
        raise RuntimeError(
            f"No image files found in images_folder (recursive): {images_folder}. "
            "Ensure the ZIP/folder contains .png or .jpg page images."
        )

    for full_path in source_files:
        dest = images_dir / os.path.basename(full_path)
        if not dest.exists():
            shutil.copy2(full_path, dest)


def _find_pdf_path(images_dir: Path, output_dir: str, images_folder: str | None) -> str:
    """Search for a PDF recursively in images_dir, output_dir, and images_folder."""
    for search_root in (str(images_dir), output_dir, images_folder):
        if not search_root or not os.path.isdir(search_root):
            continue
        for dirpath, _, filenames in os.walk(search_root):
            for fname in filenames:
                if fname.lower().endswith(".pdf"):
                    return os.path.join(dirpath, fname)
    return ""


def _merge_results_with_mapper(results: list[dict[str, Any]], mapper_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapper_lookup = {
        str(item.get("Track Change Segment", "")): item
        for item in mapper_entries
    }
    merged_rows: list[dict[str, Any]] = []

    for result in results:
        segment_id = str(result.get("segment_id", ""))
        source = mapper_lookup.get(segment_id, {})
        merged_rows.append({
            "segment_id": segment_id,
            "type": result.get("type", source.get("type", "")),
            "section": source.get("section", ""),
            "paragraph_index": source.get("paragraph_index", ""),
            "track_change": source.get("track_change", ""),
            "Track changes": source.get("Track changes", ""),
            "Deleted Content": source.get("Deleted Content", ""),
            "Previous Content": source.get("Previous Content", ""),
            "prev_context": source.get("prev_context", ""),
            "post_context": source.get("post_context", ""),
            "paragraph_text": source.get("paragraph_text", ""),
            "previous_para": source.get("previous_para", ""),
            "post_para": source.get("post_para", ""),
            "author": source.get("author", ""),
            "date": source.get("date", ""),
            "has_bullet_point": source.get("has_bullet_point", ""),
            "source_excel_type": source.get("source_excel_type", ""),
            "revision_order": source.get("revision_order", ""),
            "source_row_number": source.get("source_row_number", ""),
            "pages": result.get("pages", []),
            "is_valid": result.get("is_valid"),
            "tagging_ok": result.get("tagging_ok"),
            "spacing_ok": result.get("spacing_ok"),
            "reason": result.get("reason", ""),
            "suggested_fix": result.get("suggested_fix", ""),
            "raw_response": result.get("raw_response", ""),
            "error": result.get("error", ""),
        })
    return merged_rows


def _write_report_excel(output_path: str, merged_rows: list[dict[str, Any]], mapper_entries: list[dict[str, Any]], content_drops_path: Path | None) -> str:
    if pd is None:
        raise RuntimeError("pandas is required to create the Excel validation report")

    _, _format_validation_excel_sheet, _ = _load_validation_runtime()

    validation_df = pd.DataFrame(merged_rows)
    mapper_df = pd.DataFrame(mapper_entries)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        validation_df.to_excel(writer, index=False, sheet_name="Validation Results")
        _format_validation_excel_sheet(writer.sheets["Validation Results"], validation_df)

        mapper_df.to_excel(writer, index=False, sheet_name="Excel Mapper")

        if content_drops_path and content_drops_path.exists():
            with open(content_drops_path, "r", encoding="utf-8") as handle:
                drops_data = json.load(handle)
            if drops_data:
                pd.DataFrame(drops_data).to_excel(writer, index=False, sheet_name="Content Drops")

    return output_path


def generate_validation_report_from_excel(
    excel_path: str,
    docx_path: str,
    postvendor_xml_path: str,
    output_dir: str,
    images_folder: str,
    prevendor_xml_path: str | None = None,
    claude_api_key: str | None = None,
    claude_model: str | None = None,
    validation_prompt_path: str | None = None,
    max_workers: int = 5,
) -> str:
    _, _, run_outp_validation = _load_validation_runtime()

    output_root = Path(output_dir)
    temp_dir = output_root / "validation_temp"
    images_dir = temp_dir / "document_images"
    temp_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    claude_model = claude_model or _get_default_model_from_config()

    prevendor_xml_text = ""
    if prevendor_xml_path and os.path.exists(prevendor_xml_path):
        prevendor_xml_text = Path(prevendor_xml_path).read_text(encoding="utf-8")

    rows = read_excel_records(excel_path)
    mapper_entries = convert_excel_rows_to_mapper(rows, prevendor_xml_text=prevendor_xml_text)
    if not mapper_entries:
        raise ValueError("No validation mapper entries could be derived from the Excel file")

    mapper_path = temp_dir / "excel_mapper.json"
    mapper_path.write_text(json.dumps(mapper_entries, indent=2, ensure_ascii=False), encoding="utf-8")

    if not images_folder:
        raise RuntimeError(
            "images_folder is required. Upload the revision images ZIP/folder that contains "
            "the page PNGs (and optionally a PDF for page-text extraction)."
        )

    _prepare_images(images_dir=images_dir, images_folder=images_folder)
    pdf_path = _find_pdf_path(images_dir=images_dir, output_dir=output_dir, images_folder=images_folder)
    if not pdf_path:
        raise RuntimeError(
            "No PDF found in the images folder. The revision PDF is required alongside the "
            "page images for page-text extraction. Include the PDF inside your images ZIP."
        )

    api_key = _resolve_api_key(claude_model=claude_model, claude_api_key=claude_api_key)
    if not api_key:
        raise RuntimeError("Claude API key not available")

    results = run_outp_validation(
        mapper_path=str(mapper_path),
        final_xml_path=postvendor_xml_path,
        images_folder_path=str(images_dir),
        pdf_path=pdf_path,
        output_dir=str(temp_dir),
        claude_api_key=api_key,
        claude_model=claude_model,
        validation_prompt_path=validation_prompt_path,
        max_workers=max_workers,
    )

    merged_rows = _merge_results_with_mapper(results, mapper_entries)
    merged_json_path = temp_dir / "track_change_validation_results_merged.json"
    merged_json_path.write_text(json.dumps(merged_rows, indent=2, ensure_ascii=False), encoding="utf-8")

    timestamp = datetime.now().strftime("%H-%M_%d_%m_%Y")
    excel_output_path = output_root / f"final_validation_report_excel_mapper_{timestamp}.xlsx"
    content_drops_path = temp_dir / "content_drops.json"

    return _write_report_excel(
        output_path=str(excel_output_path),
        merged_rows=merged_rows,
        mapper_entries=mapper_entries,
        content_drops_path=content_drops_path,
    )


def generate_validation_report_from_enriched(
    docx_path: str,
    prevendor_xml_path: str,
    postvendor_xml_path: str,
    output_dir: str,
    images_folder: str,
    claude_api_key: str | None = None,
    claude_model: str | None = None,
    validation_prompt_path: str | None = None,
    max_workers: int = 5,
    context_chars: int = 50,
    enriched_dict: dict | None = None,
) -> str:
    """Generate validation report using the dataframe_enricher for richer context.

    Instead of reading a pre-made Excel, this function runs dataframe_enricher
    directly on the DOCX + prevendor PXML to extract track changes with FULL
    paragraph text, accurate previous/post paragraphs, and LLM-filled section
    numbers. This gives the validator much better anchors for locating the
    correct page and PXML chunk.

    Parameters
    ----------
    docx_path : str
        Path to the revision DOCX file with tracked changes.
    prevendor_xml_path : str
        Path to the prevendor PXML file.
    postvendor_xml_path : str
        Path to the post-vendor (final output) XML/PXML file.
    output_dir : str
        Directory where validation outputs will be written.
    images_folder : str
        Folder containing revision page images (PNGs) and a PDF.
    claude_api_key : str, optional
        Claude API key; auto-resolved if None.
    claude_model : str, optional
        Claude model name; auto-resolved from config if None.
    validation_prompt_path : str, optional
        Path to prompt_validation.txt.
    max_workers : int
        Parallel validation worker count.
    context_chars : int
        Characters for prev_context/post_context (default 50 for richer context).
    enriched_dict : dict, optional
        Pre-computed enriched dict from build_enriched_dataframe. If None, it
        will be computed from docx_path + prevendor_xml_path.

    Returns
    -------
    str
        Path to the generated Excel validation report.
    """
    from dataframe_enricher import build_enriched_dataframe

    _, _, run_outp_validation = _load_validation_runtime()

    output_root = Path(output_dir)
    temp_dir = output_root / "validation_temp"
    images_dir = temp_dir / "document_images"
    temp_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    claude_model = claude_model or _get_default_model_from_config()

    # ─── Step 1: Run dataframe_enricher to get rich track-change data ─────────
    if enriched_dict is None:
        print("[Validation] Running dataframe_enricher for rich context extraction...")
        print("[Validation] Section number filling skipped (not needed for validation)")

        enriched_dict = build_enriched_dataframe(
            docx_path=docx_path,
            pxml_path=prevendor_xml_path,
            context_chars=context_chars,
            verbose=True,
            skip_section_filling=True,
        )

    revisions_df = enriched_dict.get("revisions_df")
    pxml_content = enriched_dict.get("pxml_content", "")

    if revisions_df is None or (hasattr(revisions_df, "empty") and revisions_df.empty):
        raise ValueError("No track changes found by dataframe_enricher in the DOCX file")

    print(f"[Validation] Enriched dataframe has {len(revisions_df)} track change rows")
    print(f"[Validation] Columns: {list(revisions_df.columns)}")

    # ─── Step 2: Convert enriched df to mapper (same format as Excel flow) ────
    mapper_entries = convert_enriched_df_to_mapper(revisions_df, pxml_content=pxml_content)
    if not mapper_entries:
        raise ValueError("No validation mapper entries could be derived from the enriched dataframe")

    print(f"[Validation] Created {len(mapper_entries)} mapper entries from enriched data")

    mapper_path = temp_dir / "enriched_mapper.json"
    mapper_path.write_text(json.dumps(mapper_entries, indent=2, ensure_ascii=False), encoding="utf-8")

    # Also save the enriched dataframe as Excel for reference
    if pd is not None:
        enriched_excel_path = temp_dir / "enriched_revisions.xlsx"
        revisions_df.to_excel(str(enriched_excel_path), index=False)

    # ─── Step 3: Prepare images and find PDF ──────────────────────────────────
    if not images_folder:
        raise RuntimeError(
            "images_folder is required. Provide the folder containing "
            "page PNGs and a PDF for page-text extraction."
        )

    _prepare_images(images_dir=images_dir, images_folder=images_folder)
    pdf_path = _find_pdf_path(images_dir=images_dir, output_dir=output_dir, images_folder=images_folder)
    if not pdf_path:
        raise RuntimeError(
            "No PDF found in the images folder. The revision PDF is required alongside the "
            "page images for page-text extraction. Include the PDF inside your images ZIP."
        )

    # ─── Step 4: Resolve API key ─────────────────────────────────────────────
    api_key = _resolve_api_key(claude_model=claude_model, claude_api_key=claude_api_key)
    if not api_key:
        raise RuntimeError("Claude API key not available")

    # ─── Step 5: Run validation (same engine as before) ───────────────────────
    print(f"[Validation] Running validation with {max_workers} workers...")
    results = run_outp_validation(
        mapper_path=str(mapper_path),
        final_xml_path=postvendor_xml_path,
        images_folder_path=str(images_dir),
        pdf_path=pdf_path,
        output_dir=str(temp_dir),
        claude_api_key=api_key,
        claude_model=claude_model,
        validation_prompt_path=validation_prompt_path,
        max_workers=max_workers,
    )

    # ─── Step 6: Merge results with mapper and write report ───────────────────
    merged_rows = _merge_results_with_mapper(results, mapper_entries)
    merged_json_path = temp_dir / "track_change_validation_results_merged.json"
    merged_json_path.write_text(json.dumps(merged_rows, indent=2, ensure_ascii=False), encoding="utf-8")

    timestamp = datetime.now().strftime("%H-%M_%d_%m_%Y")
    excel_output_path = output_root / f"final_validation_report_enriched_{timestamp}.xlsx"
    content_drops_path = temp_dir / "content_drops.json"

    report_path = _write_report_excel(
        output_path=str(excel_output_path),
        merged_rows=merged_rows,
        mapper_entries=mapper_entries,
        content_drops_path=content_drops_path,
    )

    print(f"[Validation] ✅ Report saved: {report_path}")
    return report_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate validation report from Excel + DOCX + post-vendor XML")
    parser.add_argument("--excel", help="Path to the source Excel file with track changes (legacy mode)")
    parser.add_argument("--docx", required=True, help="Path to the revision DOCX file")
    parser.add_argument("--postvendor-xml", required=True, help="Path to the post-vendor XML/PXML file")
    parser.add_argument("--output-dir", required=True, help="Directory where validation outputs will be written")
    parser.add_argument("--images-folder", required=True, help="Folder (or extracted ZIP) containing revision page images (PNGs) and optionally a PDF")
    parser.add_argument("--prevendor-xml", help="Prevendor XML/PXML (required for enriched mode, optional fallback for Excel mode)")
    parser.add_argument("--validation-prompt", help="Path to prompt_validation.txt")
    parser.add_argument("--claude-api-key", help="Claude API key; if omitted, it is fetched via config.py")
    parser.add_argument("--claude-model", help="Claude model name; if omitted, it is fetched from config.py")
    parser.add_argument("--max-workers", type=int, default=5, help="Parallel validation worker count")
    parser.add_argument("--use-enricher", action="store_true", default=False,
                        help="Use dataframe_enricher for richer context (requires --prevendor-xml). "
                             "If --excel is omitted, enricher mode is used automatically.")
    parser.add_argument("--context-chars", type=int, default=50,
                        help="Characters for prev_context/post_context in enricher mode (default 50)")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    # Decide mode: enricher (new) vs Excel (legacy)
    use_enricher = args.use_enricher or (args.excel is None)

    if use_enricher:
        if not args.prevendor_xml:
            parser.error("--prevendor-xml is required when using enricher mode (no --excel provided)")
        report_path = generate_validation_report_from_enriched(
            docx_path=args.docx,
            prevendor_xml_path=args.prevendor_xml,
            postvendor_xml_path=args.postvendor_xml,
            output_dir=args.output_dir,
            images_folder=args.images_folder,
            claude_api_key=args.claude_api_key,
            claude_model=args.claude_model,
            validation_prompt_path=args.validation_prompt,
            max_workers=args.max_workers,
            context_chars=args.context_chars,
        )
    else:
        report_path = generate_validation_report_from_excel(
            excel_path=args.excel,
            docx_path=args.docx,
            postvendor_xml_path=args.postvendor_xml,
            output_dir=args.output_dir,
            images_folder=args.images_folder,
            prevendor_xml_path=args.prevendor_xml,
            claude_api_key=args.claude_api_key,
            claude_model=args.claude_model,
            validation_prompt_path=args.validation_prompt,
            max_workers=args.max_workers,
        )

    print(report_path)


if __name__ == "__main__":
    main()
