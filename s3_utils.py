"""
S3 Utility Module — Upload/Download files to/from AWS S3 bucket.
Falls back to local temp directory if S3 is unavailable (no credentials or bucket not found).
"""
import os
import shutil
import tempfile
from pathlib import Path

# ─── Configuration (from environment variables) ───
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "a209309-trackchanges-topical")
S3_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_PREFIX_UPLOADS = "uploads"
S3_PREFIX_OUTPUTS = "outputs"

# Presigned URL expiry (seconds)
PRESIGNED_URL_EXPIRY = int(os.getenv("PRESIGNED_URL_EXPIRY", "3600"))

# Local fallback directory (used when S3 is unavailable)
LOCAL_STORAGE_DIR = os.path.join(tempfile.gettempdir(), "pxml_pipeline_s3_fallback")

# ─── S3 availability check ───
S3_AVAILABLE = False
try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError, BotoCoreError
    _test_client = boto3.client(
        "s3", region_name=S3_REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )
    _test_client.head_bucket(Bucket=S3_BUCKET_NAME)
    S3_AVAILABLE = True
    print(f"[S3] ✅ Connected to bucket: {S3_BUCKET_NAME}")
except Exception as e:
    print(f"[S3] ⚠️  S3 unavailable ({e}). Using local temp storage: {LOCAL_STORAGE_DIR}")
    os.makedirs(LOCAL_STORAGE_DIR, exist_ok=True)


def get_s3_client():
    """Create S3 client using environment credentials or IAM role."""
    import boto3
    return boto3.client(
        "s3",
        region_name=S3_REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )


def upload_file_to_s3(local_path: str, s3_key: str) -> str:
    """Upload a local file to S3, or copy to local fallback dir."""
    if S3_AVAILABLE:
        client = get_s3_client()
        client.upload_file(local_path, S3_BUCKET_NAME, s3_key)
        print(f"[S3] Uploaded: {local_path} → s3://{S3_BUCKET_NAME}/{s3_key}")
    else:
        dest = os.path.join(LOCAL_STORAGE_DIR, s3_key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(local_path, dest)
        print(f"[LOCAL] Saved: {local_path} → {dest}")
    return s3_key


def download_file_from_s3(s3_key: str, local_path: str) -> str:
    """Download a file from S3, or copy from local fallback dir."""
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    if S3_AVAILABLE:
        client = get_s3_client()
        client.download_file(S3_BUCKET_NAME, s3_key, local_path)
        print(f"[S3] Downloaded: s3://{S3_BUCKET_NAME}/{s3_key} → {local_path}")
    else:
        src = os.path.join(LOCAL_STORAGE_DIR, s3_key)
        if os.path.isfile(src):
            shutil.copy2(src, local_path)
            print(f"[LOCAL] Copied: {src} → {local_path}")
        else:
            raise FileNotFoundError(f"File not found locally: {src}")
    return local_path


def upload_directory_to_s3(local_dir: str, s3_prefix: str) -> list:
    """Upload an entire directory to S3 (or local fallback) recursively."""
    uploaded_keys = []
    for root, dirs, files in os.walk(local_dir):
        for filename in files:
            local_path = os.path.join(root, filename)
            relative_path = os.path.relpath(local_path, local_dir)
            s3_key = f"{s3_prefix}/{relative_path}".replace("\\", "/")
            upload_file_to_s3(local_path, s3_key)
            uploaded_keys.append(s3_key)
    return uploaded_keys


def download_directory_from_s3(s3_prefix: str, local_dir: str) -> list:
    """Download all files under a prefix from S3 or local fallback."""
    downloaded = []
    if S3_AVAILABLE:
        client = get_s3_client()
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=s3_prefix):
            for obj in page.get("Contents", []):
                s3_key = obj["Key"]
                relative = s3_key[len(s3_prefix):].lstrip("/")
                if not relative:
                    continue
                local_path = os.path.join(local_dir, relative)
                download_file_from_s3(s3_key, local_path)
                downloaded.append(local_path)
    else:
        local_prefix_dir = os.path.join(LOCAL_STORAGE_DIR, s3_prefix)
        if os.path.isdir(local_prefix_dir):
            for root, dirs, files in os.walk(local_prefix_dir):
                for fname in files:
                    src = os.path.join(root, fname)
                    relative = os.path.relpath(src, local_prefix_dir)
                    dest = os.path.join(local_dir, relative)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(src, dest)
                    downloaded.append(dest)
    return downloaded


def generate_presigned_url(s3_key: str, expiry: int = None) -> str:
    """Generate a presigned download URL, or return local file path."""
    if S3_AVAILABLE:
        client = get_s3_client()
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET_NAME, "Key": s3_key},
            ExpiresIn=expiry or PRESIGNED_URL_EXPIRY,
        )
        return url
    else:
        local_path = os.path.join(LOCAL_STORAGE_DIR, s3_key)
        return f"file:///{local_path.replace(os.sep, '/')}"


def upload_job_inputs(job_id: str, docx_path: str, pxml_path: str, images_dir: str = None) -> dict:
    """
    Upload all job input files to S3 under uploads/{job_id}/.
    Returns dict of S3 keys.
    """
    prefix = f"{S3_PREFIX_UPLOADS}/{job_id}"
    keys = {}
    keys["docx"] = upload_file_to_s3(docx_path, f"{prefix}/{os.path.basename(docx_path)}")
    keys["pxml"] = upload_file_to_s3(pxml_path, f"{prefix}/{os.path.basename(pxml_path)}")
    if images_dir and os.path.isdir(images_dir):
        keys["images"] = upload_directory_to_s3(images_dir, f"{prefix}/images")
    return keys


def upload_job_outputs(job_id: str, output_dir: str) -> list:
    """Upload all output files to S3 under outputs/{job_id}/."""
    prefix = f"{S3_PREFIX_OUTPUTS}/{job_id}"
    return upload_directory_to_s3(output_dir, prefix)


def get_output_download_url(job_id: str, filename: str) -> str:
    """Get presigned URL for a specific output file."""
    s3_key = f"{S3_PREFIX_OUTPUTS}/{job_id}/{filename}"
    return generate_presigned_url(s3_key)


def list_output_files(job_id: str) -> list:
    """List all output files for a job (S3 or local fallback)."""
    files = []
    if S3_AVAILABLE:
        client = get_s3_client()
        prefix = f"{S3_PREFIX_OUTPUTS}/{job_id}/"
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=prefix):
            for obj in page.get("Contents", []):
                relative = obj["Key"][len(prefix):]
                if relative:
                    files.append({"key": obj["Key"], "filename": relative, "size": obj["Size"]})
    else:
        local_dir = os.path.join(LOCAL_STORAGE_DIR, S3_PREFIX_OUTPUTS, job_id)
        if os.path.isdir(local_dir):
            for root, dirs, fnames in os.walk(local_dir):
                for fname in fnames:
                    fpath = os.path.join(root, fname)
                    relative = os.path.relpath(fpath, local_dir).replace("\\", "/")
                    files.append({"key": f"{S3_PREFIX_OUTPUTS}/{job_id}/{relative}", "filename": relative, "size": os.path.getsize(fpath)})
    return files
