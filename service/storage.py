"""Cloudflare R2 (S3-compatible) object storage helper.

Uploads finished decks to R2 and returns their public URL (served via the
configured custom domain). Keys are namespaced under ``<prefix>/<job_id>/`` so
this service never collides with — or deletes — objects written by other apps
that share the same bucket.
"""

from __future__ import annotations

from pathlib import Path

import boto3
from botocore.config import Config

from .config import settings


def _client():
    if not settings.r2_enabled:
        raise RuntimeError(
            "R2 is not configured. Set R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / "
            "R2_SECRET_ACCESS_KEY / R2_BUCKET_NAME / R2_PUBLIC_DOMAIN in .env."
        )
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=Config(signature_version="s3v4", region_name="auto"),
    )


def object_key(job_id: str, filename: str) -> str:
    safe_name = Path(filename).name
    prefix = settings.r2_key_prefix
    return f"{prefix}/{job_id}/{safe_name}" if prefix else f"{job_id}/{safe_name}"


def upload_file(job_id: str, path: Path, content_type: str) -> tuple[str, str]:
    """Upload an arbitrary job artifact under this job's prefix, verify its size,
    and return ``(key, public_url)``. Raises on failure."""
    key = object_key(job_id, path.name)
    client = _client()
    with path.open("rb") as fh:
        client.put_object(Bucket=settings.r2_bucket, Key=key, Body=fh, ContentType=content_type)
    head = client.head_object(Bucket=settings.r2_bucket, Key=key)
    local_size = path.stat().st_size
    if head.get("ContentLength") != local_size:
        raise RuntimeError(
            f"R2 upload size mismatch for {key}: "
            f"local={local_size} remote={head.get('ContentLength')}"
        )
    return key, settings.r2_public_url(key)


def delete_prefix(job_id: str) -> int:
    """Delete every object this service wrote for a job (under its own prefix
    only — never touches other apps' files in a shared bucket). Returns count.
    Best-effort: returns 0 if R2 is not configured."""
    if not settings.r2_enabled:
        return 0
    prefix = settings.r2_key_prefix
    key_prefix = f"{prefix}/{job_id}/" if prefix else f"{job_id}/"
    client = _client()
    deleted = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.r2_bucket, Prefix=key_prefix):
        objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=settings.r2_bucket, Delete={"Objects": objects})
            deleted += len(objects)
    return deleted
