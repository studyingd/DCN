"""MinIO / RustFS object storage service."""

import io
import logging
import threading

from minio import Minio
from minio.error import S3Error

from app.config import (
    MINIO_ACCESS_KEY,
    MINIO_BUCKET_RECORDINGS,
    MINIO_ENDPOINT,
    MINIO_SECRET_KEY,
    MINIO_SECURE,
)

logger = logging.getLogger(__name__)

_client: Minio | None = None
_init_lock = threading.Lock()


def get_minio_client() -> Minio:
    global _client
    if _client is None:
        with _init_lock:
            if _client is None:
                _client = Minio(
                    MINIO_ENDPOINT,
                    access_key=MINIO_ACCESS_KEY,
                    secret_key=MINIO_SECRET_KEY,
                    secure=MINIO_SECURE,
                )
                for bucket in [MINIO_BUCKET_RECORDINGS]:
                    if not _client.bucket_exists(bucket):
                        _client.make_bucket(bucket)
                        logger.info("Created MinIO bucket: %s", bucket)
    return _client


def upload_recording(session_id: str, data: bytes) -> str:
    """Upload a cast recording file. Returns the object path."""
    client = get_minio_client()
    path = f"{session_id}.cast"
    client.put_object(
        MINIO_BUCKET_RECORDINGS,
        path,
        io.BytesIO(data),
        length=len(data),
        content_type="application/octet-stream",
    )
    logger.info("Uploaded recording: %s (%d bytes)", path, len(data))
    return path


def upload_rdp_recording(session_id: str, data: bytes) -> str:
    """Upload an RDP Guacamole recording file. Returns the object path."""
    client = get_minio_client()
    path = f"{session_id}.guac"
    client.put_object(
        MINIO_BUCKET_RECORDINGS,
        path,
        io.BytesIO(data),
        length=len(data),
        content_type="application/octet-stream",
    )
    logger.info("Uploaded RDP recording: %s (%d bytes)", path, len(data))
    return path


def download_recording(session_id: str) -> bytes | None:
    """Download a recording file as bytes."""
    client = get_minio_client()
    path = f"{session_id}.cast"
    try:
        response = client.get_object(MINIO_BUCKET_RECORDINGS, path)
        data = response.read()
        response.close()
        response.release_conn()
        return data
    except S3Error:
        return None


def download_rdp_recording(session_id: str) -> bytes | None:
    """Download an RDP recording file as bytes."""
    client = get_minio_client()
    path = f"{session_id}.guac"
    try:
        response = client.get_object(MINIO_BUCKET_RECORDINGS, path)
        data = response.read()
        response.close()
        response.release_conn()
        return data
    except S3Error:
        return None


def upload_rdp_video(session_id: str, data: bytes) -> str:
    """Upload an RDP video recording file. Returns the object path."""
    client = get_minio_client()
    path = f"{session_id}.webm"
    client.put_object(
        MINIO_BUCKET_RECORDINGS,
        path,
        io.BytesIO(data),
        length=len(data),
        content_type="video/webm",
    )
    logger.info("Uploaded RDP video: %s (%d bytes)", path, len(data))
    return path


def download_rdp_video(session_id: str) -> bytes | None:
    """Download an RDP video recording file as bytes."""
    client = get_minio_client()
    path = f"{session_id}.webm"
    try:
        response = client.get_object(MINIO_BUCKET_RECORDINGS, path)
        data = response.read()
        response.close()
        response.release_conn()
        return data
    except S3Error:
        return None


def delete_recording(session_id: str) -> int:
    """Delete all recording objects for a session (cast/guac/webm).

    Best-effort: missing objects are not errors. Returns the number of objects
    actually removed. Called by the retention purge so MinIO objects do not
    become orphans when their DB rows are deleted.
    """
    client = get_minio_client()
    removed = 0
    for ext in (".cast", ".guac", ".webm"):
        path = f"{session_id}{ext}"
        try:
            client.remove_object(MINIO_BUCKET_RECORDINGS, path)
            removed += 1
        except S3Error as exc:
            # NoSuchKey is expected when only one format exists for the session.
            logger.debug("remove_object %s skipped: %s", path, exc)
    return removed
