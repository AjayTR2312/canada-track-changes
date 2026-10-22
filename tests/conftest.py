"""Shared fixtures: a fake S3 (moto) so the storage layer runs against real boto3 code paths."""
import os
import sys

# Must be set BEFORE the application modules are imported (they read the bucket name at import).
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["AWS_REGION"] = "us-east-1"
os.environ["S3_BUCKET_NAME"] = "test-bucket"
os.environ.pop("S3_LOCAL_DIR", None)
os.environ.pop("S3_KEY_PREFIX", None)
os.environ["REGION_ID"] = "canada"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest
from moto import mock_aws


@pytest.fixture()
def s3():
    """A fresh empty bucket for every test."""
    import boto3
    import s3_utils
    with mock_aws():
        s3_utils._clients.clear()          # a client created outside the mock would talk to real AWS
        s3_utils._health_cache["at"] = 0.0
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket")
        yield s3_utils
        s3_utils._clients.clear()
