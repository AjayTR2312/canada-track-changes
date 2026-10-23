import { useEffect, useState } from 'react';
import { API_BASE } from './services/basePath';

// The active region pack (name, products, DTD families) comes from the backend
// (GET /api/region, backed by regions/<REGION_ID>/region.yaml) so no screen hard-codes a
// region. Shared by the host app and the isolated validator documents.

export interface RegionInfo {
  id: string;
  name: string;
  description: string;
  products: { id: string; label: string }[];
  dtd_families: { id: string; label: string; selectable: boolean }[];
  default_family: string;
}

// Shown until the first response (and if the API is unreachable) — deliberately neutral.
const PLACEHOLDER: RegionInfo = {
  id: '', name: '', description: '', products: [], dtd_families: [], default_family: '',
};

let cached: Promise<RegionInfo> | null = null;

export function loadRegion(): Promise<RegionInfo> {
  if (!cached) {
    const base = API_BASE;
    cached = fetch(`${base}/api/region`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .catch((err) => {
        cached = null; // retry on the next mount
        console.error('Could not load the region info:', err);
        return PLACEHOLDER;
      });
  }
  return cached;
}

export function useRegion(): RegionInfo {
  const [region, setRegion] = useState<RegionInfo>(PLACEHOLDER);
  useEffect(() => {
    let alive = true;
    loadRegion().then((r) => { if (alive) setRegion(r); });
    return () => { alive = false; };
  }, []);
  return region;
}

/** Browser tab title for the host app, e.g. "XML Track Changes - US Forms". */
export function useRegionTitle(prefix = 'XML Track Changes'): RegionInfo {
  const region = useRegion();
  useEffect(() => {
    document.title = region.name ? `${prefix} - ${region.name}` : prefix;
  }, [region, prefix]);
  return region;
}
