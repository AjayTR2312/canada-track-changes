"""Read a publication's files from a folder in the S3 bucket so a whole publication can be
batch-processed without a manual upload (replaces the old SharePoint-folder source).

A user drops the chapter files (docx + xml/pxml + images zip per chapter) into a folder of the
bucket -- e.g. with the S3 console, the AWS CLI or another pipeline -- and gives the folder name
(or an s3://<bucket>/<folder> link) in the Batch tab.

Only the files a batch can use are fetched; anything else in the folder (working notes, prior
outputs, loose PDFs -- the chapter PDF comes inside the images zip) is ignored.
"""
from __future__ import annotations

import os
import re

import s3_utils

# Extensions a batch can use.
_WANTED_EXT = {".docx", ".doc", ".xml", ".pxml", ".zip"}


def normalize_folder(value: str) -> str:
    """User input -> a clean key prefix ending in '/'. Raises RuntimeError with a message meant to be
    shown to the user (wrong bucket, reserved/internal folder, empty)."""
    v = (value or "").strip()
    if not v:
        raise RuntimeError("Please provide an S3 folder (for example: incoming/Publication_6121007).")
    m = re.match(r"^s3://([^/]+)/?(.*)$", v, re.IGNORECASE)
    if m:
        if m.group(1) != s3_utils.S3_BUCKET_NAME:
            raise RuntimeError(f"That link points to bucket '{m.group(1)}'. This application reads from "
                               f"'{s3_utils.S3_BUCKET_NAME}' only.")
        v = m.group(2)
    v = v.replace("\\", "/").strip("/")
    parts = v.split("/")
    if not v or ".." in parts:
        raise RuntimeError("Please provide a folder inside the bucket (for example: incoming/Publication_6121007).")
    if parts[0] in s3_utils.RESERVED_PREFIXES:
        raise RuntimeError(f"'{parts[0]}/' is used by the application for its own data. Put the publication's "
                           f"files in a different folder (for example: incoming/Publication_6121007).")
    return v + "/"


def list_folder(prefix: str) -> list:
    """The usable files directly in or below the folder: [{key, name, size}]."""
    return [
        {"key": o["key"], "name": o["key"].rsplit("/", 1)[-1], "size": o["size"]}
        for o in s3_utils.list_objects(prefix)
        if os.path.splitext(o["key"])[1].lower() in _WANTED_EXT
    ]


def download_s3_folder(folder: str, dest_dir: str) -> list:
    """Download every usable file of an S3 folder into `dest_dir` (flat). Subfolders are walked,
    because publications are often organised one subfolder per chapter -- flattening is safe since
    the grouping step keys off filenames, not paths. Returns the local filenames written."""
    prefix = normalize_folder(folder)
    files = list_folder(prefix)
    os.makedirs(dest_dir, exist_ok=True)
    written = []
    for f in sorted(files, key=lambda f: f["key"]):
        # Flattened, so a name already taken by a different chapter's file must not be overwritten.
        target = f["name"]
        counter = 2
        while os.path.exists(os.path.join(dest_dir, target)):
            stem, ext = os.path.splitext(f["name"])
            target = f"{stem} ({counter}){ext}"
            counter += 1
        print(f"[S3-BATCH] Downloading '{f['name']}' ({f['size'] / 1024:.0f} KB)…")
        s3_utils.download_file_from_s3(f["key"], os.path.join(dest_dir, target))
        written.append(target)
    print(f"[S3-BATCH] Done — {len(written)} file(s) fetched from s3://{s3_utils.S3_BUCKET_NAME}/{prefix}")
    return written
