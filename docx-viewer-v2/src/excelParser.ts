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
  const xmlElementIdsCol = keys.find(
    (k) => {
      const norm = k.toLowerCase().replace(/[_\s]/g, '');
      return norm.includes('xmlelementids') || norm.includes('xml_element_ids');
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

    // Parse xml_element_ids: can be JSON array string like '["1","2"]' or comma-separated
    let xmlElementIds: string[] = [];
    if (xmlElementIdsCol) {
      const rawIds = row[xmlElementIdsCol];
      if (rawIds) {
        const idsStr = String(rawIds).trim();
        if (idsStr.startsWith('[')) {
          try { xmlElementIds = JSON.parse(idsStr); } catch { xmlElementIds = []; }
        } else {
          xmlElementIds = idsStr.split(',').map(s => s.trim()).filter(Boolean);
        }
      }
    }

    return { trackChangeText, isValid, reason, suggestedFix, previousContext, xmlElementIds };
  });

  return rows;
}

/**
 * Match Excel rows to TrackChanges using xml_element_ids when available,
 * falling back to index-based matching (row 0 → revision 0, etc.).
 * Returns a map of trackChangeId → is_valid plus per-row match details.
 */
export function matchExcelToTrackChanges(
  excelRows: ExcelRow[],
  trackChanges: TrackChange[]
): { validationMap: ExcelValidationMap; metaMap: ExcelMetaMap; matchDetails: MatchDetail[]; unmappedRevisions: TrackChange[] } {
  const validationMap: ExcelValidationMap = {};
  const metaMap: ExcelMetaMap = {};
  const matchDetails: MatchDetail[] = [];

  // Build a map of xmlElementId -> TrackChange for ID-based matching
  const xmlIdToTc = new Map<string, TrackChange>();
  for (const tc of trackChanges) {
    if (tc.xmlElementIds) {
      for (const xmlId of tc.xmlElementIds) {
        xmlIdToTc.set(xmlId, tc);
      }
    }
  }

  // Check if any Excel row has xml_element_ids — if so, prefer ID-based matching
  const hasXmlIds = excelRows.some(row => row.xmlElementIds.length > 0);
  const matchedTcIds = new Set<string>();

  for (let i = 0; i < excelRows.length; i++) {
    const excelRow = excelRows[i];
    let tc: TrackChange | undefined;

    if (hasXmlIds && excelRow.xmlElementIds.length > 0) {
      // Match by xml_element_ids: find the TrackChange that shares any ID
      for (const xmlId of excelRow.xmlElementIds) {
        const found = xmlIdToTc.get(xmlId);
        if (found && !matchedTcIds.has(found.id)) {
          tc = found;
          break;
        }
      }
    }

    // Fallback: index-based matching
    if (!tc) {
      tc = trackChanges[i];
    }

    if (tc) {
      matchedTcIds.add(tc.id);
      validationMap[tc.id] = excelRow.isValid;
      metaMap[tc.id] = {
        reason: excelRow.reason,
        suggestedFix: excelRow.suggestedFix,
      };
      matchDetails.push({
        rowIndex: i,
        excelText: excelRow.trackChangeText,
        trackChangeId: tc.id,
        trackChangeText: tc.text,
        isValid: excelRow.isValid,
        matched: true,
      });
    } else {
      // More Excel rows than track changes
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

  // Track changes beyond matched ones are unmapped
  const unmappedRevisions = trackChanges.filter(tc => !matchedTcIds.has(tc.id));

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


