"""R2/S3 image storage backend for memory images.

This module handles uploading, retrieving, and deleting images from
Cloudflare R2 (S3-compatible) storage. Images are stored separately
from the SQLite database but in the same bucket.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import time
from typing import Optional

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    boto3 = None
    ClientError = None
    NoCredentialsError = None

logger = logging.getLogger(__name__)


class R2ImageStorageError(Exception):
    """Raised when R2 image operations fail."""
    pass


class R2ImageStorage:
    """Handles image upload, retrieval, and deletion for R2/S3 storage.

    Images are stored at: images/{memory_id}/{timestamp}_{index}_{hash}.{ext}
    """

    def __init__(
        self,
        bucket: str,
        endpoint_url: Optional[str] = None,
        public_domain: Optional[str] = None,
    ):
        """Initialize R2 image storage client.

        Args:
            bucket: S3/R2 bucket name
            endpoint_url: S3-compatible endpoint URL (e.g., R2 endpoint)
            public_domain: Public URL base for images (e.g., https://pub-xxx.r2.dev)
                          If not set, will generate signed URLs
        """
        if boto3 is None:
            raise ImportError(
                "boto3 is required for R2 image storage. "
                "Install with: pip install boto3"
            )

        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.public_domain = public_domain.rstrip('/') if public_domain else None

        # Initialize S3 client
        client_kwargs = {}
        if endpoint_url:
            client_kwargs['endpoint_url'] = endpoint_url

        self.s3_client = boto3.client('s3', **client_kwargs)

        logger.info(f"Initialized R2ImageStorage: bucket={bucket}, endpoint={endpoint_url}")

    def _generate_key(
        self,
        memory_id: int,
        image_index: int,
        content_hash: str,
        extension: str,
    ) -> str:
        """Generate unique object key for image.

        Format: images/{memory_id}/{timestamp}_{index}_{hash[:8]}.{ext}
        """
        timestamp = int(time.time())
        short_hash = content_hash[:8]
        return f"images/{memory_id}/{timestamp}_{image_index}_{short_hash}.{extension}"

    def _compute_hash(self, data: bytes) -> str:
        """Compute SHA256 hash of image data."""
        return hashlib.sha256(data).hexdigest()

    def upload_image(
        self,
        image_data: bytes,
        content_type: str,
        memory_id: int,
        image_index: int = 0,
    ) -> str:
        """Upload image to R2 and return the storage key.

        Args:
            image_data: Raw image bytes
            content_type: MIME type (e.g., 'image/jpeg')
            memory_id: ID of the memory this image belongs to
            image_index: Index of image within the memory (for multiple images)

        Returns:
            Storage key (relative path) for the image, prefixed with 'r2://'

        Raises:
            R2ImageStorageError: If upload fails
        """
        # Determine extension from content type
        ext_map = {
            'image/jpeg': 'jpg',
            'image/png': 'png',
            'image/gif': 'gif',
            'image/webp': 'webp',
        }
        extension = ext_map.get(content_type, 'jpg')

        # Generate unique key
        content_hash = self._compute_hash(image_data)
        key = self._generate_key(memory_id, image_index, content_hash, extension)

        try:
            self.s3_client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=image_data,
                ContentType=content_type,
            )
            logger.debug(f"Uploaded image to R2: {key}")
            # Return key with r2:// prefix to identify as R2 storage reference
            return f"r2://{key}"

        except (ClientError, NoCredentialsError) as e:
            logger.error(f"Failed to upload image to R2: {e}")
            raise R2ImageStorageError(f"Failed to upload image: {e}") from e

    def get_url(self, key: str) -> str:
        """Get URL for an image.

        Returns public URL if public_domain is configured,
        otherwise generates a signed URL.
        """
        if self.public_domain:
            return f"{self.public_domain}/{key}"
        else:
            # Generate signed URL (valid for 1 hour)
            try:
                url = self.s3_client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': self.bucket, 'Key': key},
                    ExpiresIn=3600,
                )
                return url
            except ClientError as e:
                logger.error(f"Failed to generate signed URL: {e}")
                raise R2ImageStorageError(f"Failed to generate URL: {e}") from e

    def delete_image(self, key: str) -> bool:
        """Delete a single image from R2.

        Returns:
            True if deleted, False if not found
        """
        try:
            self.s3_client.delete_object(Bucket=self.bucket, Key=key)
            logger.debug(f"Deleted image from R2: {key}")
            return True
        except ClientError as e:
            logger.error(f"Failed to delete image from R2: {e}")
            return False

    def delete_memory_images(self, memory_id: int) -> int:
        """Delete all images for a memory.

        Uses prefix-based listing to find and delete all images
        associated with a memory.

        Returns:
            Number of images deleted
        """
        prefix = f"images/{memory_id}/"
        deleted_count = 0

        try:
            # List all objects with the memory's prefix
            paginator = self.s3_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                objects = page.get('Contents', [])
                if not objects:
                    continue

                # Delete objects in batch
                delete_keys = [{'Key': obj['Key']} for obj in objects]
                self.s3_client.delete_objects(
                    Bucket=self.bucket,
                    Delete={'Objects': delete_keys}
                )
                deleted_count += len(delete_keys)

            if deleted_count > 0:
                logger.info(f"Deleted {deleted_count} images for memory {memory_id}")
            return deleted_count

        except ClientError as e:
            logger.error(f"Failed to delete images for memory {memory_id}: {e}")
            return deleted_count


def expand_r2_url(src: str, use_proxy: bool = True) -> str:
    """Expand an r2:// reference to a full URL.

    Args:
        src: Image source (may be r2://key, http(s)://, or data:)
        use_proxy: If True, return local proxy URL (/r2/...) instead of R2 URL

    Returns:
        Proxy URL, full R2 URL, or src unchanged
    """
    if not src.startswith('r2://'):
        return src

    key = src[5:]  # Remove r2:// prefix

    # Use local proxy URL for graph visualization (avoids CORS/auth issues)
    if use_proxy:
        return f"/r2/{key}"

    # Fall back to direct R2 URL (signed or public)
    image_storage = get_image_storage_instance()
    if not image_storage:
        return src

    return image_storage.get_url(key)


def parse_data_uri(data_uri: str) -> tuple[bytes, str]:
    """Parse a data URI into bytes and content type.

    Args:
        data_uri: Data URI string (e.g., 'data:image/jpeg;base64,...')

    Returns:
        Tuple of (image_bytes, content_type)

    Raises:
        ValueError: If data URI format is invalid
    """
    # Match data URI format: data:mime/type;base64,DATA
    match = re.match(r'^data:([^;]+);base64,(.+)$', data_uri)
    if not match:
        raise ValueError("Invalid data URI format")

    content_type = match.group(1)
    b64_data = match.group(2)

    try:
        image_bytes = base64.b64decode(b64_data)
        return image_bytes, content_type
    except Exception as e:
        raise ValueError(f"Failed to decode base64 data: {e}") from e


def get_image_storage() -> Optional[R2ImageStorage]:
    """Get the global R2 image storage instance if configured.

    Uses MEMORA_STORAGE_URI to determine bucket, and AWS_ENDPOINT_URL
    for the R2 endpoint.

    Returns:
        R2ImageStorage instance if configured, None otherwise
    """
    storage_uri = os.getenv('MEMORA_STORAGE_URI', '')

    # Only initialize if using cloud storage (s3://)
    if not storage_uri.startswith('s3://'):
        return None

    # Parse bucket from s3://bucket/path/to/file
    parts = storage_uri[5:].split('/', 1)
    if not parts:
        return None
    bucket = parts[0]

    endpoint_url = os.getenv('AWS_ENDPOINT_URL')
    public_domain = os.getenv('R2_PUBLIC_DOMAIN')

    try:
        return R2ImageStorage(
            bucket=bucket,
            endpoint_url=endpoint_url,
            public_domain=public_domain,
        )
    except Exception as e:
        logger.warning(f"Failed to initialize R2 image storage: {e}")
        return None


# Global instance (lazy initialization)
_image_storage: Optional[R2ImageStorage] = None
_image_storage_initialized = False


def get_image_storage_instance() -> Optional[R2ImageStorage]:
    """Get or create the global R2ImageStorage instance."""
    global _image_storage, _image_storage_initialized

    if not _image_storage_initialized:
        _image_storage = get_image_storage()
        _image_storage_initialized = True

    return _image_storage
