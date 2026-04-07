from __future__ import annotations

import boto3
from botocore.client import Config as BotoConfig
from urllib.parse import urlparse

from ..config import settings


def _configured_direct_url() -> str:
    return settings.effective_site_agent_download_url


def _configured_bucket_download() -> bool:
    return all(
        [
            settings.effective_site_agent_download_bucket,
            settings.effective_site_agent_download_object_key,
            settings.effective_site_agent_download_s3_endpoint,
            settings.effective_site_agent_download_access_key_id,
            settings.effective_site_agent_download_secret_access_key,
        ]
    )


def _filename_from_download_target(url: str, object_key: str) -> str:
    if url:
        return str(urlparse(url).path or "").rstrip("/").rsplit("/", 1)[-1]
    return str(object_key or "").rstrip("/").rsplit("/", 1)[-1]


def site_agent_download_state() -> dict[str, object]:
    url = _configured_direct_url()
    version = settings.effective_site_agent_download_version
    object_key = settings.effective_site_agent_download_object_key
    filename = _filename_from_download_target(url, object_key)
    return {
        "available": bool(url) or _configured_bucket_download(),
        "url": url,
        "version": version,
        "filename": filename,
        "source": "direct_url" if url else ("presigned_bucket" if _configured_bucket_download() else ""),
    }


def resolve_site_agent_download_url() -> str:
    direct_url = _configured_direct_url()
    if direct_url:
        return direct_url

    if not _configured_bucket_download():
        return ""

    session = boto3.session.Session()
    client = session.client(
        "s3",
        endpoint_url=settings.effective_site_agent_download_s3_endpoint,
        region_name=settings.effective_site_agent_download_s3_region or None,
        aws_access_key_id=settings.effective_site_agent_download_access_key_id,
        aws_secret_access_key=settings.effective_site_agent_download_secret_access_key,
        config=BotoConfig(signature_version="s3v4"),
    )
    return str(
        client.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": settings.effective_site_agent_download_bucket,
                "Key": settings.effective_site_agent_download_object_key,
            },
            ExpiresIn=settings.effective_site_agent_download_presign_ttl_seconds,
        )
    )
