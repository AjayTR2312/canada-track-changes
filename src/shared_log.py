"""A small keyed record log stored in S3 -- one JSON object per record.

Why one object per record (instead of one growing CSV): several API pods write to the
same log at the same time.  A CSV would have to be downloaded, edited and re-uploaded,
so two pods would silently overwrite each other's rows.  With one object per record an
upsert is a single atomic PUT and a removal a single DELETE; nothing is ever lost and
nothing is kept in a pod's memory or disk.

Records are keyed by an identity tuple (never appended blindly), so repeating an action
updates the row instead of duplicating it.  `read_all()` returns every team member's rows.

Layout:  feedback/<region id>/<kind>/[<partition>/]<sha1(identity)>.json
Used by the false-positive log, the correction (chatbot) log and the resolver log.
"""
from __future__ import annotations

import hashlib
import logging

import s3_utils
from canada_pipeline.region import get_region

logger = logging.getLogger(__name__)


class SharedLog:
    def __init__(self, kind: str, headers: list, identity_fields: tuple, partition_field: str | None = None):
        self.kind = kind
        self.headers = list(headers)
        self.identity_fields = tuple(identity_fields)
        # When set, records are grouped under feedback/<region>/<kind>/<value of this field>/ so a
        # per-session read (e.g. the flags of ONE validation session) lists only that folder.
        self.partition_field = partition_field
        self.prefix = f"{s3_utils.S3_PREFIX_FEEDBACK}/{s3_utils.safe_id(get_region().id)}/{kind}/"

    # ---- identity / keys -------------------------------------------------------
    def identity(self, row: dict) -> tuple:
        return tuple(str(row.get(f, "")) for f in self.identity_fields)

    def _key_for_identity(self, ident: tuple) -> str:
        digest = hashlib.sha1("\x1f".join(ident).encode("utf-8")).hexdigest()[:24]
        if self.partition_field:
            part = s3_utils.safe_id(ident[self.identity_fields.index(self.partition_field)], "none")
            return f"{self.prefix}{part}/{digest}.json"
        return f"{self.prefix}{digest}.json"

    def _clean(self, row: dict) -> dict:
        return {h: ("" if row.get(h) is None else str(row.get(h))) for h in self.headers}

    # ---- writes ----------------------------------------------------------------
    def upsert(self, row: dict) -> dict:
        row = self._clean(row)
        s3_utils.put_json(self._key_for_identity(self.identity(row)), row)
        return row

    def remove(self, identity: tuple) -> bool:
        return s3_utils.delete_object(self._key_for_identity(tuple(str(i) for i in identity)))

    # ---- reads -----------------------------------------------------------------
    def get(self, identity: tuple) -> dict | None:
        return s3_utils.get_json(self._key_for_identity(tuple(str(i) for i in identity)))

    def _read_prefix(self, prefix: str) -> list:
        keys = [o["key"] for o in s3_utils.list_objects(prefix) if o["key"].endswith(".json")]
        return s3_utils.read_many_json(keys)

    def read_partition(self, value: str) -> list:
        """Rows of one partition (needs partition_field)."""
        if not self.partition_field:
            return self.read_all()
        return self._read_prefix(f"{self.prefix}{s3_utils.safe_id(value, 'none')}/")

    def read_all(self) -> list:
        """Every team member's rows."""
        return self._read_prefix(self.prefix)

    # kept for callers written against the old file-based log
    read_local = read_all


# Backwards-compatible name (the old implementation kept CSV files).
SharedCsvLog = SharedLog
