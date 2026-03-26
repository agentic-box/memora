"""Storage backend abstraction for pluggable cloud and local storage.

This module provides a backend system that allows memora to transparently
use different storage backends (local SQLite, cloud-synced SQLite, etc.) while
keeping the same API surface.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import filelock
except ImportError:
    filelock = None

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
except ImportError:
    boto3 = None
    ClientError = None
    NoCredentialsError = None
    EndpointConnectionError = None
    BotoConfig = None

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds
RETRY_MAX_DELAY = 30.0  # seconds
SYNC_CHECK_TTL = 30.0  # seconds - skip HEAD request if we checked recently

# Transient error codes that should trigger retry
TRANSIENT_ERROR_CODES = {
    "500", "502", "503", "504",  # Server errors
    "RequestTimeout", "RequestTimeoutException",
    "ThrottlingException", "Throttling",
    "SlowDown", "ServiceUnavailable",
    "InternalError",
}


def _is_transient_error(error: Exception) -> bool:
    """Check if an error is transient and should be retried."""
    if EndpointConnectionError and isinstance(error, EndpointConnectionError):
        return True
    if ClientError and isinstance(error, ClientError):
        error_code = error.response.get("Error", {}).get("Code", "")
        return error_code in TRANSIENT_ERROR_CODES
    # Also retry on connection errors
    if isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return True
    return False


def _get_user_friendly_error(error: Exception, operation: str) -> str:
    """Convert S3/R2 errors to user-friendly messages with actionable advice."""
    if NoCredentialsError and isinstance(error, NoCredentialsError):
        return (
            f"AWS/R2 credentials not found while {operation}.\n"
            "Please configure credentials using one of:\n"
            "  - Environment variables: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY\n"
            "  - AWS credentials file: ~/.aws/credentials\n"
            "  - For Cloudflare R2: Set AWS_ENDPOINT_URL to your R2 endpoint"
        )

    if ClientError and isinstance(error, ClientError):
        error_code = error.response.get("Error", {}).get("Code", "")
        error_msg = error.response.get("Error", {}).get("Message", str(error))

        if error_code == "AccessDenied" or error_code == "403":
            return (
                f"Access denied while {operation}.\n"
                f"Error: {error_msg}\n"
                "Please check:\n"
                "  - Your API token has the correct permissions (read/write)\n"
                "  - The bucket name is correct\n"
                "  - For R2: Token needs 'Object Read & Write' permission"
            )
        elif error_code == "InvalidAccessKeyId":
            return (
                f"Invalid access key while {operation}.\n"
                "Your AWS_ACCESS_KEY_ID appears to be incorrect.\n"
                "Please verify your credentials are correct."
            )
        elif error_code == "SignatureDoesNotMatch":
            return (
                f"Signature mismatch while {operation}.\n"
                "Your AWS_SECRET_ACCESS_KEY appears to be incorrect.\n"
                "Please verify your credentials are correct."
            )
        elif error_code == "NoSuchBucket":
            return (
                f"Bucket not found while {operation}.\n"
                f"Error: {error_msg}\n"
                "Please check that the bucket exists and the name is correct."
            )
        elif error_code in TRANSIENT_ERROR_CODES:
            return (
                f"Temporary service error while {operation} (will retry).\n"
                f"Error: {error_code} - {error_msg}"
            )
        else:
            return f"S3/R2 error while {operation}: {error_code} - {error_msg}"

    if EndpointConnectionError and isinstance(error, EndpointConnectionError):
        return (
            f"Cannot connect to cloud storage while {operation}.\n"
            "Please check:\n"
            "  - Your internet connection\n"
            "  - The AWS_ENDPOINT_URL is correct (for R2/MinIO)\n"
            f"Error: {error}"
        )

    return f"Error while {operation}: {error}"


def _retry_with_backoff(func, operation: str, max_retries: int = MAX_RETRIES):
    """Execute a function with exponential backoff retry for transient errors.

    Args:
        func: Callable to execute
        operation: Description of operation for error messages
        max_retries: Maximum number of retry attempts

    Returns:
        Result of func()

    Raises:
        Original exception if non-transient or retries exhausted
    """
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as e:
            last_error = e

            if not _is_transient_error(e):
                # Non-transient error, don't retry
                raise

            if attempt == max_retries:
                # Last attempt failed
                logger.error(
                    f"All {max_retries + 1} attempts failed for {operation}: {e}"
                )
                raise

            # Calculate delay with exponential backoff + jitter
            delay = min(
                RETRY_BASE_DELAY * (2 ** attempt) + (time.time() % 1),
                RETRY_MAX_DELAY
            )
            logger.warning(
                f"Transient error during {operation} (attempt {attempt + 1}/{max_retries + 1}), "
                f"retrying in {delay:.1f}s: {e}"
            )
            time.sleep(delay)

    # Should not reach here, but just in case
    raise last_error


class ConflictError(Exception):
    """Raised when a cloud sync conflict is detected (concurrent modification)."""
    pass


class StorageBackend(ABC):
    """Abstract base class for storage backends.

    Backends are responsible for:
    1. Providing a SQLite connection via connect()
    2. Syncing state before use (download from cloud, etc.)
    3. Syncing state after writes (upload to cloud, etc.)
    """

    @abstractmethod
    def connect(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        """Return a SQLite connection ready for use.

        For cloud backends, this may involve syncing from remote first.

        Args:
            check_same_thread: SQLite connection parameter

        Returns:
            sqlite3.Connection ready for queries
        """
        pass

    @abstractmethod
    def sync_before_use(self) -> None:
        """Sync state before using the database (e.g., download from cloud)."""
        pass

    @abstractmethod
    def sync_after_write(self) -> None:
        """Sync state after modifying the database (e.g., upload to cloud)."""
        pass

    @abstractmethod
    def get_info(self) -> dict:
        """Return diagnostic information about the backend."""
        pass

    @property
    def supports_sessions(self) -> bool:
        """Whether this backend supports session memory tables.

        Session memory requires local SQLite with transaction support,
        foreign keys, and no cloud sync on every write.
        """
        return False


class LocalSQLiteBackend(StorageBackend):
    """Local file-based SQLite backend (original behavior)."""

    def __init__(self, db_path: Path):
        """Initialize local SQLite backend.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = Path(db_path)
        self._ensure_parent_dir()

    def _ensure_parent_dir(self) -> None:
        """Ensure parent directory exists."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        """Return a connection to the local SQLite database."""
        conn = sqlite3.connect(self.db_path, check_same_thread=check_same_thread)
        conn.row_factory = sqlite3.Row
        return conn

    def sync_before_use(self) -> None:
        """No-op for local backend."""
        pass

    def sync_after_write(self) -> None:
        """No-op for local backend."""
        pass

    def get_info(self) -> dict:
        """Return backend information."""
        return {
            "backend_type": "local_sqlite",
            "db_path": str(self.db_path),
            "exists": self.db_path.exists(),
            "size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
        }

    @property
    def supports_sessions(self) -> bool:
        return True


class CloudSQLiteBackend(StorageBackend):
    """Cloud-backed SQLite using local cache with sync to/from S3-compatible storage.

    This backend:
    - Downloads the SQLite file from cloud storage to a local cache
    - Serves all queries from the local cache (fast)
    - Uploads changes back to cloud storage after writes
    - Uses file locking to prevent concurrent corruption
    - Tracks dirty state to avoid unnecessary uploads
    """

    def __init__(
        self,
        cloud_url: str,
        cache_dir: Optional[Path] = None,
        encrypt: bool = False,
        compress: bool = False,
        auto_sync: bool = True,
    ):
        """Initialize cloud SQLite backend.

        Args:
            cloud_url: S3 URL (e.g., s3://bucket/path/to/db.sqlite)
            cache_dir: Local cache directory (default: ~/.cache/memora)
            encrypt: Enable server-side encryption on upload
            compress: Compress database before upload
            auto_sync: Automatically sync before/after operations
        """
        if boto3 is None:
            raise ImportError(
                "boto3 is required for cloud storage. "
                "Install with: pip install boto3"
            )

        if filelock is None:
            raise ImportError(
                "filelock is required for cloud storage. "
                "Install with: pip install filelock"
            )

        self.cloud_url = cloud_url
        self.encrypt = encrypt
        self.compress = compress
        self.auto_sync = auto_sync

        # Parse S3 URL
        self.bucket, self.key = self._parse_s3_url(cloud_url)

        # Set up cache directory
        if cache_dir is None:
            cache_dir = Path.home() / ".cache" / "memora"
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Create a unique cache path based on bucket + key
        cache_key = hashlib.sha256(f"{self.bucket}/{self.key}".encode()).hexdigest()[:16]
        self.cache_path = self.cache_dir / cache_key / "memories.db"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        # Lock file to prevent concurrent access
        self.lock_path = self.cache_path.parent / "sync.lock"
        self.lock = filelock.FileLock(self.lock_path, timeout=30)

        # Metadata file to track sync state
        self.meta_path = self.cache_path.parent / "metadata.json"

        # S3 client
        self.s3_client = boto3.client("s3")

        # Dirty tracking
        self._is_dirty = False
        self._last_hash = None

        # TTL cache for sync checks (avoids redundant HEAD requests)
        self._last_sync_check: float = 0.0

        logger.info(f"Initialized CloudSQLiteBackend: {cloud_url} -> {self.cache_path}")

    def _parse_s3_url(self, url: str) -> tuple[str, str]:
        """Parse S3 URL into bucket and key.

        Args:
            url: S3 URL like s3://bucket/path/to/file.db

        Returns:
            (bucket, key) tuple
        """
        if not url.startswith("s3://"):
            raise ValueError(f"Cloud URL must start with s3://, got: {url}")

        parts = url[5:].split("/", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid S3 URL format: {url}")

        bucket, key = parts
        return bucket, key

    def _compute_hash(self) -> Optional[str]:
        """Compute hash of the local database file.

        Returns:
            SHA256 hash of the file, or None if file doesn't exist
        """
        if not self.cache_path.exists():
            return None

        sha256 = hashlib.sha256()
        with open(self.cache_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _load_metadata(self) -> dict:
        """Load sync metadata from cache."""
        if self.meta_path.exists():
            try:
                with open(self.meta_path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load metadata: {e}")
        return {}

    def _save_metadata(self, metadata: dict) -> None:
        """Save sync metadata to cache."""
        try:
            with open(self.meta_path, "w") as f:
                json.dump(metadata, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save metadata: {e}")

    def _create_and_upload_empty_database(self) -> None:
        """Create an empty database locally and upload it to cloud storage.

        This is called when the remote database doesn't exist yet (first-time setup).
        """
        logger.info(f"Creating empty database and uploading to {self.bucket}/{self.key}")

        # Create empty local database with schema
        # Import here to avoid circular imports
        from .storage import ensure_schema

        conn = sqlite3.connect(self.cache_path, check_same_thread=True)
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        conn.close()

        logger.info(f"Created empty database at {self.cache_path}")

        # Upload to cloud
        extra_args = {}
        if self.encrypt:
            extra_args["ServerSideEncryption"] = "AES256"

        self.s3_client.upload_file(
            str(self.cache_path),
            self.bucket,
            self.key,
            ExtraArgs=extra_args if extra_args else None
        )

        # Get the ETag of the uploaded file
        head_response = self.s3_client.head_object(
            Bucket=self.bucket,
            Key=self.key
        )

        # Save metadata
        metadata = {
            "etag": head_response.get("ETag", "").strip('"'),
            "last_sync": datetime.now().isoformat(),
            "remote_modified": head_response.get("LastModified").isoformat() if head_response.get("LastModified") else None,
        }
        self._save_metadata(metadata)

        # Update tracking
        self._last_hash = self._compute_hash()
        self._is_dirty = False

        logger.info(f"Uploaded empty database to {self.bucket}/{self.key}")

    def _create_local_database_only(self) -> None:
        """Create an empty database locally without uploading to cloud.

        This is used as a fallback when cloud upload fails, allowing
        the user to continue working in local-only mode.
        """
        logger.info(f"Creating local-only database at {self.cache_path}")

        # Create empty local database with schema
        from .storage import ensure_schema

        conn = sqlite3.connect(self.cache_path, check_same_thread=True)
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        conn.close()

        # Mark as dirty so next sync attempt will try to upload
        self._last_hash = self._compute_hash()
        self._is_dirty = True

        logger.warning(
            "Running in local-only mode. Changes will not sync to cloud until "
            "connectivity is restored. Run 'memora-server sync-push' to retry upload."
        )

    def sync_before_use(self) -> None:
        """Download database from S3 if needed."""
        if not self.auto_sync:
            return

        # Skip if we checked recently (within TTL)
        now = time.time()
        if now - self._last_sync_check < SYNC_CHECK_TTL and self.cache_path.exists():
            logger.debug(f"Skipping sync check (TTL: {SYNC_CHECK_TTL}s)")
            return

        with self.lock:
            try:
                # Check if remote object exists and get metadata
                try:
                    head_response = _retry_with_backoff(
                        lambda: self.s3_client.head_object(
                            Bucket=self.bucket,
                            Key=self.key
                        ),
                        "checking remote database"
                    )
                    remote_etag = head_response.get("ETag", "").strip('"')
                    remote_modified = head_response.get("LastModified")
                except ClientError as e:
                    error_code = e.response["Error"]["Code"]
                    # Handle both 404 (Not Found) and 403 (Forbidden/Access Denied)
                    # R2 and some S3 configurations return 403 for non-existent objects
                    # when the bucket policy doesn't allow ListBucket
                    if error_code in ("404", "403"):
                        logger.info(
                            f"Remote database not found (error {error_code}), "
                            f"creating empty database"
                        )
                        try:
                            self._create_and_upload_empty_database()
                        except Exception as upload_error:
                            # Graceful fallback: use local-only mode if upload fails
                            friendly_msg = _get_user_friendly_error(
                                upload_error, "uploading initial database"
                            )
                            logger.warning(
                                f"Failed to upload initial database, using local-only mode.\n"
                                f"{friendly_msg}"
                            )
                            # Create local database without uploading
                            self._create_local_database_only()
                        self._last_sync_check = time.time()
                        return
                    raise

                # Load local metadata
                metadata = self._load_metadata()
                local_etag = metadata.get("etag")

                # Skip download if local cache is up to date
                if local_etag == remote_etag and self.cache_path.exists():
                    logger.debug(f"Local cache is up to date (ETag: {remote_etag})")
                    self._last_hash = self._compute_hash()
                    self._last_sync_check = time.time()
                    return

                # Download from S3 with retry
                logger.info(f"Downloading {self.bucket}/{self.key} to {self.cache_path}")
                start_time = time.time()

                # Download to temporary file first
                temp_path = self.cache_path.parent / f"{self.cache_path.name}.tmp"

                _retry_with_backoff(
                    lambda: self.s3_client.download_file(
                        self.bucket, self.key, str(temp_path)
                    ),
                    "downloading database"
                )

                # Move to final location
                shutil.move(str(temp_path), str(self.cache_path))

                duration = time.time() - start_time
                size_mb = self.cache_path.stat().st_size / (1024 * 1024)
                logger.info(f"Downloaded {size_mb:.2f} MB in {duration:.2f}s")

                # Update metadata
                metadata["etag"] = remote_etag
                metadata["last_sync"] = datetime.now().isoformat()
                metadata["remote_modified"] = remote_modified.isoformat() if remote_modified else None
                self._save_metadata(metadata)

                # Update hash and sync check time
                self._last_hash = self._compute_hash()
                self._is_dirty = False
                self._last_sync_check = time.time()

            except NoCredentialsError as e:
                friendly_msg = _get_user_friendly_error(e, "syncing from cloud")
                logger.error(friendly_msg)
                raise RuntimeError(friendly_msg) from e
            except ClientError as e:
                friendly_msg = _get_user_friendly_error(e, "syncing from cloud")
                logger.error(friendly_msg)
                raise RuntimeError(friendly_msg) from e
            except Exception as e:
                friendly_msg = _get_user_friendly_error(e, "syncing from cloud")
                logger.error(friendly_msg)
                raise

    def sync_after_write(self) -> None:
        """Upload database to S3 if dirty."""
        if not self.auto_sync:
            return

        # Fast path: check dirty flag first (avoids expensive hashing)
        if not self._is_dirty:
            logger.debug("Database not dirty, skipping sync")
            return

        with self.lock:
            try:
                # Double-check dirty flag under lock
                if not self._is_dirty:
                    logger.debug("Database not dirty (checked under lock), skipping sync")
                    return

                if not self.cache_path.exists():
                    logger.warning("Cache file doesn't exist, nothing to upload")
                    return

                # Compute hash to detect changes (only when dirty flag is set)
                current_hash = self._compute_hash()
                if current_hash == self._last_hash:
                    # False positive - dirty flag was set but content unchanged
                    logger.debug("Database unchanged after hashing, skipping upload")
                    self._is_dirty = False
                    return

                # Check for conflicts before uploading
                # Load the last known remote ETag
                metadata = self._load_metadata()
                last_known_etag = metadata.get("etag")

                # Verify remote hasn't changed since our last sync
                if last_known_etag:
                    try:
                        current_remote = _retry_with_backoff(
                            lambda: self.s3_client.head_object(
                                Bucket=self.bucket,
                                Key=self.key
                            ),
                            "checking remote state before upload"
                        )
                        current_remote_etag = current_remote.get("ETag", "").strip('"')

                        if current_remote_etag != last_known_etag:
                            # Conflict detected: remote was modified by another writer
                            logger.error(
                                f"Conflict detected! Remote object changed since last sync. "
                                f"Expected ETag: {last_known_etag}, "
                                f"Current ETag: {current_remote_etag}"
                            )
                            raise ConflictError(
                                "Database was modified by another process. "
                                "Run 'memora-server sync-pull' to get latest changes."
                            )
                    except ClientError as e:
                        if e.response["Error"]["Code"] != "404":
                            raise

                # Upload to S3 with retry
                logger.info(f"Uploading {self.cache_path} to {self.bucket}/{self.key}")
                start_time = time.time()

                extra_args = {}
                if self.encrypt:
                    extra_args["ServerSideEncryption"] = "AES256"

                _retry_with_backoff(
                    lambda: self.s3_client.upload_file(
                        str(self.cache_path),
                        self.bucket,
                        self.key,
                        ExtraArgs=extra_args if extra_args else None
                    ),
                    "uploading database"
                )

                duration = time.time() - start_time
                size_mb = self.cache_path.stat().st_size / (1024 * 1024)
                logger.info(f"Uploaded {size_mb:.2f} MB in {duration:.2f}s")

                # Update metadata with new remote state
                head_response = _retry_with_backoff(
                    lambda: self.s3_client.head_object(
                        Bucket=self.bucket,
                        Key=self.key
                    ),
                    "verifying upload"
                )
                metadata = {
                    "etag": head_response.get("ETag", "").strip('"'),
                    "last_sync": datetime.now().isoformat(),
                    "remote_modified": head_response.get("LastModified").isoformat() if head_response.get("LastModified") else None,
                }
                self._save_metadata(metadata)

                # Update tracking
                self._last_hash = current_hash
                self._is_dirty = False

            except ConflictError:
                # Re-raise conflict errors without wrapping
                raise
            except NoCredentialsError as e:
                friendly_msg = _get_user_friendly_error(e, "uploading to cloud")
                logger.error(friendly_msg)
                # Keep dirty flag set so next attempt will try again
                raise RuntimeError(friendly_msg) from e
            except ClientError as e:
                friendly_msg = _get_user_friendly_error(e, "uploading to cloud")
                logger.error(friendly_msg)
                # Keep dirty flag set so next attempt will try again
                raise RuntimeError(friendly_msg) from e
            except Exception as e:
                friendly_msg = _get_user_friendly_error(e, "uploading to cloud")
                logger.error(friendly_msg)
                # Keep dirty flag set so next attempt will try again
                raise

    def connect(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        """Return a connection to the cached SQLite database.

        This will sync from cloud if needed before returning the connection.
        """
        # Sync from cloud before use
        self.sync_before_use()

        # Create connection to local cache
        conn = sqlite3.connect(self.cache_path, check_same_thread=check_same_thread)
        conn.row_factory = sqlite3.Row

        # Mark backend as dirty when connection commits
        # We use a wrapper class to intercept commits since in Python 3.13+
        # sqlite3.Connection methods are read-only
        class TrackedConnection:
            def __init__(self, conn, backend):
                self._conn = conn
                self._backend = backend

            def __getattr__(self, name):
                attr = getattr(self._conn, name)
                if name == 'commit':
                    def wrapped_commit(*args, **kwargs):
                        result = attr(*args, **kwargs)
                        self._backend._is_dirty = True
                        logger.debug("Database marked as dirty after commit")
                        return result
                    return wrapped_commit
                return attr

            def __enter__(self):
                return self._conn.__enter__()

            def __exit__(self, *args):
                return self._conn.__exit__(*args)

        return TrackedConnection(conn, self)

    def get_info(self) -> dict:
        """Return backend information."""
        metadata = self._load_metadata()
        return {
            "backend_type": "cloud_sqlite",
            "cloud_url": self.cloud_url,
            "bucket": self.bucket,
            "key": self.key,
            "cache_path": str(self.cache_path),
            "cache_exists": self.cache_path.exists(),
            "cache_size_bytes": self.cache_path.stat().st_size if self.cache_path.exists() else 0,
            "is_dirty": self._is_dirty,
            "last_etag": metadata.get("etag"),
            "last_sync": metadata.get("last_sync"),
            "auto_sync": self.auto_sync,
            "encrypt": self.encrypt,
        }

    def force_sync_pull(self) -> None:
        """Force download from cloud, ignoring local state."""
        with self.lock:
            logger.info("Forcing sync pull from cloud")
            # Clear metadata to force download
            if self.meta_path.exists():
                self.meta_path.unlink()
            # Reset TTL cache to ensure sync_before_use() actually downloads
            self._last_sync_check = 0.0
            self.sync_before_use()

    def force_sync_push(self) -> None:
        """Force upload to cloud, even if not dirty."""
        with self.lock:
            logger.info("Forcing sync push to cloud")
            self._is_dirty = True
            self._last_hash = None  # Force hash mismatch
            self.sync_after_write()


class D1Row:
    """A dict-like row that supports both index and key access (like sqlite3.Row)."""

    def __init__(self, data: dict, columns: list):
        self._data = data
        self._columns = columns

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[self._columns[key]]
        return self._data[key]

    def __iter__(self):
        return iter(self._data.values())

    def keys(self):
        return self._columns

    def values(self):
        return [self._data[c] for c in self._columns]

    def items(self):
        return [(c, self._data[c]) for c in self._columns]

    def __repr__(self):
        return f"D1Row({self._data})"


class D1Cursor:
    """A cursor-like object for D1 query results."""

    def __init__(self, results: list, columns: list, lastrowid: int = 0, rowcount: int = 0):
        self._results = results
        self._columns = columns
        self._index = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount
        self.description = [(col, None, None, None, None, None, None) for col in columns] if columns else None

    def fetchone(self):
        if self._index >= len(self._results):
            return None
        row = self._results[self._index]
        self._index += 1
        return D1Row(row, self._columns)

    def fetchall(self):
        rows = self._results[self._index:]
        self._index = len(self._results)
        return [D1Row(row, self._columns) for row in rows]

    def fetchmany(self, size=None):
        if size is None:
            size = 1
        rows = self._results[self._index:self._index + size]
        self._index += len(rows)
        return [D1Row(row, self._columns) for row in rows]

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def close(self):
        pass


class D1Connection:
    """A connection-like object that talks to Cloudflare D1 via HTTP API."""

    # Class-level session token for read-your-writes consistency across connections
    _session_token: str = None
    _session_lock = threading.Lock()

    def __init__(self, account_id: str, database_id: str, api_token: str):
        self.account_id = account_id
        self.database_id = database_id
        self.api_token = api_token
        self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}"
        self.row_factory = None
        self._pending_statements = []

    def _execute_api(self, sql: str, params: tuple = None) -> dict:
        """Execute SQL via D1 HTTP API with session affinity for read-your-writes."""
        import urllib.error
        import urllib.request

        url = f"{self.base_url}/query"

        body = {"sql": sql}
        if params:
            # D1 expects positional params as a list
            body["params"] = list(params)

        data = json.dumps(body).encode("utf-8")

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

        # Include session token for read-your-writes consistency
        with D1Connection._session_lock:
            if D1Connection._session_token:
                headers["cf-d1-session-token"] = D1Connection._session_token

        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())

                # Extract session token from response for future requests
                # D1 returns this token after writes to enable read-your-writes
                session_token = resp.headers.get("cf-d1-session-token")
                if session_token:
                    with D1Connection._session_lock:
                        D1Connection._session_token = session_token

        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else str(e)
            raise RuntimeError(f"D1 API error ({e.code}): {error_body}")

        if not result.get("success"):
            errors = result.get("errors", [])
            error_msg = errors[0].get("message") if errors else "Unknown error"
            raise RuntimeError(f"D1 query failed: {error_msg}")

        return result

    def execute(self, sql: str, params: tuple = None) -> D1Cursor:
        """Execute a single SQL statement."""
        result = self._execute_api(sql, params)

        # D1 returns results in a nested structure
        query_result = result.get("result", [{}])[0]
        rows = query_result.get("results", [])
        meta = query_result.get("meta", {})

        # Extract columns from first row if available
        columns = list(rows[0].keys()) if rows else []

        return D1Cursor(
            results=rows,
            columns=columns,
            lastrowid=meta.get("last_row_id", 0),
            rowcount=meta.get("changes", len(rows)),
        )

    def executemany(self, sql: str, params_list: list) -> D1Cursor:
        """Execute SQL for multiple parameter sets."""
        # D1 doesn't have native executemany, so we batch execute
        lastrowid = 0
        total_changes = 0

        for params in params_list:
            result = self._execute_api(sql, params)
            query_result = result.get("result", [{}])[0]
            meta = query_result.get("meta", {})
            lastrowid = meta.get("last_row_id", lastrowid)
            total_changes += meta.get("changes", 0)

        return D1Cursor(results=[], columns=[], lastrowid=lastrowid, rowcount=total_changes)

    def executescript(self, sql_script: str) -> D1Cursor:
        """Execute multiple SQL statements separated by semicolons."""
        # Split by semicolons and execute each
        statements = [s.strip() for s in sql_script.split(";") if s.strip()]
        lastrowid = 0
        total_changes = 0

        for stmt in statements:
            result = self._execute_api(stmt)
            query_result = result.get("result", [{}])[0]
            meta = query_result.get("meta", {})
            lastrowid = meta.get("last_row_id", lastrowid)
            total_changes += meta.get("changes", 0)

        return D1Cursor(results=[], columns=[], lastrowid=lastrowid, rowcount=total_changes)

    def cursor(self) -> "D1Connection":
        """Return self as cursor (D1Connection acts as both)."""
        return self

    def commit(self):
        """No-op - D1 auto-commits each query."""
        pass

    def rollback(self):
        """No-op - D1 doesn't support transactions via HTTP API."""
        pass

    def close(self):
        """No-op - HTTP connections are stateless."""
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class D1Backend(StorageBackend):
    """Cloudflare D1 backend - uses D1 as primary database via HTTP API.

    This backend:
    - Executes all queries directly against D1 (no local caching)
    - Uses R2 for media/image storage only
    - No sync needed - D1 is the source of truth
    """

    def __init__(self, account_id: str, database_id: str, api_token: str):
        """Initialize D1 backend.

        Args:
            account_id: Cloudflare account ID
            database_id: D1 database ID
            api_token: Cloudflare API token with D1 permissions
        """
        self.account_id = account_id
        self.database_id = database_id
        self.api_token = api_token

        logger.info(f"Initialized D1Backend: database={database_id}")

    def connect(self, *, check_same_thread: bool = True) -> D1Connection:
        """Return a D1 connection."""
        return D1Connection(self.account_id, self.database_id, self.api_token)

    def sync_before_use(self) -> None:
        """No-op - D1 is always up to date."""
        pass

    def sync_after_write(self) -> None:
        """No-op - D1 writes are immediate."""
        pass

    def get_info(self) -> dict:
        """Return backend information."""
        return {
            "backend_type": "d1",
            "account_id": self.account_id,
            "database_id": self.database_id,
        }


def parse_backend_uri(uri: str) -> StorageBackend:
    """Parse a storage URI and return the appropriate backend.

    Supported URI formats:
    - file:///path/to/db.sqlite (local SQLite)
    - /path/to/db.sqlite (local SQLite)
    - s3://bucket/path/to/db.sqlite (S3-compatible cloud storage)
    - d1://account_id/database_id (Cloudflare D1)

    Args:
        uri: Storage URI string

    Returns:
        StorageBackend instance
    """
    if uri.startswith("d1://"):
        # D1 URI format: d1://account_id/database_id
        # API token from environment: CLOUDFLARE_API_TOKEN or CF_API_TOKEN
        parts = uri[5:].split("/", 1)
        if len(parts) != 2:
            raise ValueError(
                f"Invalid D1 URI format: {uri}\n"
                "Expected: d1://account_id/database_id"
            )

        account_id, database_id = parts

        api_token = os.getenv("CLOUDFLARE_API_TOKEN") or os.getenv("CF_API_TOKEN")
        if not api_token:
            raise ValueError(
                "D1 backend requires CLOUDFLARE_API_TOKEN or CF_API_TOKEN environment variable.\n"
                "Create a token at: https://dash.cloudflare.com/profile/api-tokens\n"
                "Required permissions: D1 Edit"
            )

        return D1Backend(account_id=account_id, database_id=database_id, api_token=api_token)

    elif uri.startswith("s3://"):
        # Parse cloud storage options from environment
        encrypt = os.getenv("MEMORA_CLOUD_ENCRYPT", "").lower() in ("1", "true", "yes")
        compress = os.getenv("MEMORA_CLOUD_COMPRESS", "").lower() in ("1", "true", "yes")
        cache_dir_env = os.getenv("MEMORA_CACHE_DIR")
        cache_dir = Path(cache_dir_env) if cache_dir_env else None

        return CloudSQLiteBackend(
            cloud_url=uri,
            cache_dir=cache_dir,
            encrypt=encrypt,
            compress=compress,
        )

    elif uri.startswith("file://"):
        # file:// URI
        path = uri[7:]  # Remove file://
        return LocalSQLiteBackend(Path(path))

    else:
        # Assume local path
        return LocalSQLiteBackend(Path(uri))
