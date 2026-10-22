import * as XLSX from 'xlsx';
import type { TrackChange, ExcelRow, ExcelValidationMap, ExcelMetaMap } from './types';

/**
 * Parse an Excel file and extract "Track changes" + "is_valid" columns.
 * Looks for columns by name (case-insensitive, trimmed).
 */
export async function parseExcelFile(file: File): Promise<ExcelRow[]> {
  const arrayBuffer = await file.arrayBuffer();
  const workbook = XLSX.read(arrayBuffer, { type: 'array' });

  // Use the first sheet
  const sheetName = workbook.SheetNames[0];
  const sheet = workbook.Sheets[sheetName];

  // Convert to JSON (array of objects with header keys)
  // defval ensures cells with FALSE / empty are still included as keys
  const rawRows = XLSX.utils.sheet_to_json<Record<string, unknown>>(sheet, {
    raw: true,
    defval: null,
  });

  if (rawRows.length === 0) {
    throw new Error('Excel file is empty or has no data rows');
  }

  // Find the column names (case-insensitive match)
  const firstRow = rawRows[0];
  const keys = Object.keys(firstRow);

  const trackChangeCol = keys.find(
    (k) => k.toLowerCase().replace(/[_\s]/g, '').includes('trackchange')
  );
  const isValidCol = keys.find(
    (k) => k.toLowerCase().replace(/[_\s]/g, '').includes('isvalid') ||
           k.toLowerCase().replace(/[_\s]/g, '') === 'valid'
  );

  if (!trackChangeCol) {
    throw new Error(
      `Could not find "Track changes" column. Found columns: ${keys.join(', ')}`
    );
  }
  if (!isValidCol) {
    throw new Error(
      `Could not find "is_valid" column. Found columns: ${keys.join(', ')}`
    );
  }

  // Find optional reason, suggested_fix, and previous_context columns
  const reasonCol = keys.find(
    (k) => k.toLowerCase().replace(/[_\s]/g, '').includes('reason')
  );
  const suggestedFixCol = keys.find(
    (k) => k.toLowerCase().replace(/[_\s]/g, '').includes('suggestedfix') ||
           k.toLowerCase().replace(/[_\s]/g, '').includes('suggested_fix') ||
           k.toLowerCase().replace(/[_\s]/g, '').includes('fix')
  );
  const previousContextCol = keys.find(
    (k) => {
      const norm = k.toLowerCase().replace(/[_\s]/g, '');
      return norm.includes('previouscontent') ||
             norm.includes('previouscontext') ||
             norm.includes('prevcontent') ||
             norm.includes('prevcontext');
    }
  );

  const rows: ExcelRow[] = rawRows.map((row) => {
    const trackChangeText = String(row[trackChangeCol] || '').trim();
    const rawValid = row[isValidCol];

    // Parse is_valid: handle TRUE/FALSE strings, 1/0, boolean, null/undefined
    let isValid = false;
    if (rawValid === null || rawValid === undefined || rawValid === '') {
      isValid = false;
    } else if (typeof rawValid === 'boolean') {
      isValid = rawValid;
    } else if (typeof rawValid === 'number') {
      isValid = rawValid !== 0;
    } else if (typeof rawValid === 'string') {
      const normalized = rawValid.toLowerCase().trim();
      isValid = normalized === 'true' || normalized === '1' || normalized === 'yes';
    }

    const reason = reasonCol ? String(row[reasonCol] || '').trim() : '';
    const suggestedFix = suggestedFixCol ? String(row[suggestedFixCol] || '').trim() : '';
    const previousContext = previousContextCol ? String(row[previousContextCol] || '').trim() : '';

    return { trackChangeText, isValid, reason, suggestedFix, previousContext };
  });

  return rows;
}

// Normalize text for fuzzy comparison (lowercase, strip entities/punctuation, collapse spaces).
function normText(s: string): string {
  return (s || '')
    .toLowerCase()
    .replace(/&[a-z]+;/g, ' ')
    .replace(/[^a-z0-9 ]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

// Token-overlap similarity in [0,1]. Robust to minor wording/whitespace differences.
function similarity(a: string, b: string): number {
  const A = new Set(normText(a).split(' ').filter(Boolean));
  const B = new Set(normText(b).split(' ').filter(Boolean));
  if (A.size === 0 || B.size === 0) return 0;
  let inter = 0;
  A.forEach((t) => { if (B.has(t)) inter++; });
  return inter / Math.max(A.size, B.size);
}

/**
 * Match Excel validation rows to TrackChanges.
 *
 * The validation report has one row per *validated* revision (insertions / deletions /
 * replacements) in document order — it does NOT include formatting changes. The DOCX
 * track-change list DOES include formatting changes, so a naive row-i↔change-i mapping
 * drifts by one every time a formatting change appears, landing each verdict on the
 * WRONG revision. To fix that we:
 *   1) align only against non-formatting changes (the ones the validator actually saw), and
 *   2) refine with a forward-windowed text/context match so a missing/extra row can't
 *      cascade misalignment across the rest of the document.
 * Positional order is preserved (monotonic cursor); text matching only nudges within a
 * small window and falls back to position when the change text is too short to disambiguate.
 */
export function matchExcelToTrackChanges(
  excelRows: ExcelRow[],
  trackChanges: TrackChange[]
): { validationMap: ExcelValidationMap; metaMap: ExcelMetaMap; matchDetails: MatchDetail[]; unmappedRevisions: TrackChange[] } {
  const validationMap: ExcelValidationMap = {};
  const metaMap: ExcelMetaMap = {};
  const matchDetails: MatchDetail[] = [];

  // Only non-formatting changes were validated → align against those.
  const candidates = trackChanges.filter((t) => t.type !== 'formatChange');
  const used = new Array(candidates.length).fill(false);
  const WINDOW = 8;          // how far ahead we allow the text match to look
  const TEXT_THRESHOLD = 0.34; // min combined score to trust text over position

  let cursor = 0;
  for (let i = 0; i < excelRows.length; i++) {
    const excelRow = excelRows[i];

    // Look for the best text/context match in a forward window from the cursor.
    let bestIdx = -1;
    let bestScore = -1;
    for (let j = cursor; j < Math.min(candidates.length, cursor + WINDOW + 1); j++) {
      if (used[j]) continue;
      const c = candidates[j];
      const score =
        similarity(excelRow.trackChangeText, c.text) +
        0.3 * similarity(excelRow.previousContext, c.prevContext || c.paragraphText);
      if (score > bestScore) { bestScore = score; bestIdx = j; }
    }

    let tc: TrackChange | null = null;
    let matchIdx = -1;
    if (bestIdx >= 0 && bestScore >= TEXT_THRESHOLD) {
      tc = candidates[bestIdx];
      matchIdx = bestIdx;
    } else if (cursor < candidates.length) {
      // Not confident on text → keep positional alignment against non-format changes.
      tc = candidates[cursor];
      matchIdx = cursor;
    }

    if (tc && matchIdx >= 0) {
      used[matchIdx] = true;
      cursor = matchIdx + 1;
      validationMap[tc.id] = excelRow.isValid;
      metaMap[tc.id] = { reason: excelRow.reason, suggestedFix: excelRow.suggestedFix };
      matchDetails.push({
        rowIndex: i,
        excelText: excelRow.trackChangeText,
        trackChangeId: tc.id,
        trackChangeText: tc.text,
        isValid: excelRow.isValid,
        matched: true,
      });
    } else {
      // More Excel rows than revisions to map them to.
      matchDetails.push({
        rowIndex: i,
        excelText: excelRow.trackChangeText,
        trackChangeId: null,
        trackChangeText: null,
        isValid: excelRow.isValid,
        matched: false,
      });
    }
  }

  // Non-formatting revisions that never received an Excel row (not validated).
  const unmappedRevisions = candidates.filter((_, j) => !used[j]);

  return { validationMap, metaMap, matchDetails, unmappedRevisions };
}

export interface MatchDetail {
  rowIndex: number;
  excelText: string;
  trackChangeId: string | null;
  trackChangeText: string | null;
  isValid: boolean;
  matched: boolean;
};


