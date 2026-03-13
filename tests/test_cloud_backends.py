"""Tests for cloud storage backends using moto to mock AWS S3."""

import pytest

# These imports will fail if optional dependencies aren't installed
pytest.importorskip("boto3")
pytest.importorskip("moto")
pytest.importorskip("filelock")

import boto3
from moto import mock_aws

from memora.backends import (
    CloudSQLiteBackend,
    LocalSQLiteBackend,
    parse_backend_uri,
)


class TestLocalSQLiteBackend:
    """Tests for LocalSQLiteBackend."""

    def test_local_backend_creation(self, tmp_path):
        """Test creating a local SQLite backend."""
        db_path = tmp_path / "test.db"
        backend = LocalSQLiteBackend(db_path)

        assert backend.db_path == db_path
        info = backend.get_info()
        assert info["backend_type"] == "local_sqlite"
        assert info["db_path"] == str(db_path)
        assert info["exists"] is False

    def test_local_backend_connect(self, tmp_path):
        """Test connecting to local SQLite database."""
        db_path = tmp_path / "test.db"
        backend = LocalSQLiteBackend(db_path)

        conn = backend.connect()
        assert conn is not None

        # Create a simple table and insert data
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO test (name) VALUES ('test')")
        conn.commit()

        # Verify data
        cursor = conn.execute("SELECT name FROM test")
        row = cursor.fetchone()
        assert row[0] == "test"

        conn.close()

        # Verify database file was created
        info = backend.get_info()
        assert info["exists"] is True
        assert info["size_bytes"] > 0

    def test_local_backend_sync_noop(self, tmp_path):
        """Test that sync operations are no-ops for local backend."""
        db_path = tmp_path / "test.db"
        backend = LocalSQLiteBackend(db_path)

        # These should do nothing
        backend.sync_before_use()
        backend.sync_after_write()


@mock_aws
class TestCloudSQLiteBackend:
    """Tests for CloudSQLiteBackend using moto to mock S3."""

    def setup_method(self, method):
        """Set up S3 mock and test bucket."""
        self.bucket_name = "test-memory-bucket"
        self.key_name = "test/memories.db"
        self.cloud_url = f"s3://{self.bucket_name}/{self.key_name}"

        # Create mock S3 bucket
        self.s3_client = boto3.client("s3", region_name="us-east-1")
        self.s3_client.create_bucket(Bucket=self.bucket_name)

    def test_cloud_backend_creation(self, tmp_path):
        """Test creating a cloud SQLite backend."""
        backend = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path,
        )

        assert backend.cloud_url == self.cloud_url
        assert backend.bucket == self.bucket_name
        assert backend.key == self.key_name
        assert backend.cache_dir == tmp_path

        info = backend.get_info()
        assert info["backend_type"] == "cloud_sqlite"
        assert info["bucket"] == self.bucket_name
        assert info["key"] == self.key_name

    def test_cloud_backend_invalid_url(self, tmp_path):
        """Test that invalid S3 URLs raise errors."""
        with pytest.raises(ValueError, match="must start with s3://"):
            CloudSQLiteBackend("http://example.com/db.sqlite", cache_dir=tmp_path)

        with pytest.raises(ValueError, match="Invalid S3 URL"):
            CloudSQLiteBackend("s3://bucket-only", cache_dir=tmp_path)

    def test_cloud_backend_sync_new_database(self, tmp_path):
        """Test syncing when remote database doesn't exist yet."""
        backend = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path,
        )

        # Sync should succeed even if remote doesn't exist
        backend.sync_before_use()

        # Create a local database
        conn = backend.connect()
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO test (name) VALUES ('test')")
        conn.commit()
        conn.close()

        # Push to cloud
        backend.sync_after_write()

        # Verify object was uploaded to S3
        response = self.s3_client.head_object(
            Bucket=self.bucket_name,
            Key=self.key_name
        )
        assert response["ContentLength"] > 0

    def test_cloud_backend_auto_creates_and_uploads_empty_db(self, tmp_path):
        """Test that connect() auto-creates and uploads empty database when remote doesn't exist."""
        backend = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path,
        )

        # Just calling connect() should auto-create and upload the database
        conn = backend.connect()

        # Verify the database has the memora schema (memories table exists)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        )
        assert cursor.fetchone() is not None, "memories table should exist"

        # Also verify FTS and embeddings tables
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        )
        assert cursor.fetchone() is not None, "memories_fts table should exist"

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_embeddings'"
        )
        assert cursor.fetchone() is not None, "memories_embeddings table should exist"

        conn.close()

        # Verify object was uploaded to S3
        response = self.s3_client.head_object(
            Bucket=self.bucket_name,
            Key=self.key_name
        )
        assert response["ContentLength"] > 0

    def test_cloud_backend_sync_download(self, tmp_path):
        """Test downloading database from S3."""
        # First, create and upload a database
        backend1 = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path / "cache1",
        )

        conn = backend1.connect()
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO test (name) VALUES ('original')")
        conn.commit()
        conn.close()
        backend1.sync_after_write()

        # Now create a second backend pointing to same cloud URL
        backend2 = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path / "cache2",
        )

        # Sync should download the database
        backend2.sync_before_use()

        # Verify we can read the data
        conn2 = backend2.connect()
        cursor = conn2.execute("SELECT name FROM test")
        row = cursor.fetchone()
        assert row[0] == "original"
        conn2.close()

    def test_cloud_backend_dirty_tracking(self, tmp_path):
        """Test that dirty tracking prevents unnecessary uploads."""
        backend = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path,
        )

        # Create initial database
        conn = backend.connect()
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        # Upload to cloud
        backend.sync_after_write()

        # Get ETag
        response1 = self.s3_client.head_object(
            Bucket=self.bucket_name,
            Key=self.key_name
        )
        etag1 = response1["ETag"]

        # Sync again without changes - should skip upload
        backend.sync_after_write()

        # ETag should be the same (no new upload)
        response2 = self.s3_client.head_object(
            Bucket=self.bucket_name,
            Key=self.key_name
        )
        etag2 = response2["ETag"]
        assert etag1 == etag2

    def test_cloud_backend_force_sync_pull(self, tmp_path):
        """Test force pulling from cloud."""
        # Create and upload initial database
        backend1 = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path / "cache1",
        )

        conn = backend1.connect()
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO test (name) VALUES ('version1')")
        conn.commit()
        conn.close()
        backend1.sync_after_write()

        # Create second backend and sync
        backend2 = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path / "cache2",
        )
        backend2.sync_before_use()

        # Now update from first backend
        conn1 = backend1.connect()
        conn1.execute("UPDATE test SET name = 'version2'")
        conn1.commit()
        conn1.close()
        backend1.sync_after_write()

        # Force pull in second backend
        backend2.force_sync_pull()

        # Verify updated data
        conn2 = backend2.connect()
        cursor = conn2.execute("SELECT name FROM test")
        row = cursor.fetchone()
        assert row[0] == "version2"
        conn2.close()

    def test_cloud_backend_force_sync_push(self, tmp_path):
        """Test force pushing to cloud."""
        backend = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path,
        )

        # Create database
        conn = backend.connect()
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        # Force push even though we haven't called regular sync
        backend.force_sync_push()

        # Verify object exists in S3
        response = self.s3_client.head_object(
            Bucket=self.bucket_name,
            Key=self.key_name
        )
        assert response["ContentLength"] > 0

    def test_cloud_backend_encryption(self, tmp_path):
        """Test server-side encryption on upload."""
        backend = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path,
            encrypt=True,
        )

        # Create and upload database
        conn = backend.connect()
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        backend.sync_after_write()

        # Verify encryption was applied
        response = self.s3_client.head_object(
            Bucket=self.bucket_name,
            Key=self.key_name
        )
        assert response.get("ServerSideEncryption") == "AES256"

    def test_cloud_backend_etag_caching(self, tmp_path):
        """Test that ETag caching prevents unnecessary downloads."""
        # Upload initial database
        backend1 = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path / "cache1",
        )

        conn = backend1.connect()
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        backend1.sync_after_write()

        # Create second backend and sync
        backend2 = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path / "cache2",
        )
        backend2.sync_before_use()

        # Get last modified time of cache
        cache_mtime1 = backend2.cache_path.stat().st_mtime

        # Sync again - should skip download due to matching ETag
        backend2.sync_before_use()

        # Cache file should not have been modified
        cache_mtime2 = backend2.cache_path.stat().st_mtime
        assert cache_mtime1 == cache_mtime2


class TestBackendURIParsing:
    """Tests for parse_backend_uri function."""

    def test_parse_s3_uri(self):
        """Test parsing S3 URIs."""
        backend = parse_backend_uri("s3://bucket/path/to/db.sqlite")
        assert isinstance(backend, CloudSQLiteBackend)
        assert backend.bucket == "bucket"
        assert backend.key == "path/to/db.sqlite"

    def test_parse_file_uri(self, tmp_path):
        """Test parsing file:// URIs."""
        uri = f"file://{tmp_path}/test.db"
        backend = parse_backend_uri(uri)
        assert isinstance(backend, LocalSQLiteBackend)
        assert backend.db_path == tmp_path / "test.db"

    def test_parse_local_path(self, tmp_path):
        """Test parsing local file paths."""
        backend = parse_backend_uri(str(tmp_path / "test.db"))
        assert isinstance(backend, LocalSQLiteBackend)
        assert backend.db_path == tmp_path / "test.db"

    def test_parse_with_env_vars(self, tmp_path, monkeypatch):
        """Test that environment variables affect cloud backend creation."""
        monkeypatch.setenv("MEMORA_CLOUD_ENCRYPT", "true")
        monkeypatch.setenv("MEMORA_CACHE_DIR", str(tmp_path))

        backend = parse_backend_uri("s3://bucket/key.db")
        assert isinstance(backend, CloudSQLiteBackend)
        assert backend.encrypt is True
        assert backend.cache_dir == tmp_path


@mock_aws
class TestCloudBackendFileLocking:
    """Tests for file locking in cloud backends."""

    def setup_method(self, method):
        """Set up S3 mock and test bucket."""
        self.bucket_name = "test-memory-bucket"
        self.key_name = "test/memories.db"
        self.cloud_url = f"s3://{self.bucket_name}/{self.key_name}"

        # Create mock S3 bucket
        self.s3_client = boto3.client("s3", region_name="us-east-1")
        self.s3_client.create_bucket(Bucket=self.bucket_name)

    def test_file_locking_prevents_concurrent_access(self, tmp_path):
        """Test that file locking prevents concurrent access."""
        import threading
        import time

        backend = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path,
        )

        # Create initial database
        conn = backend.connect()
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        backend.sync_after_write()

        # Try to acquire lock from another thread
        lock_acquired = threading.Event()
        error_occurred = threading.Event()

        def try_acquire_lock():
            try:
                # This should timeout because main thread holds lock
                with backend.lock.acquire(timeout=0.5):
                    lock_acquired.set()
            except Exception:
                error_occurred.set()

        # Acquire lock in main thread
        with backend.lock:
            thread = threading.Thread(target=try_acquire_lock)
            thread.start()
            time.sleep(0.1)  # Give thread time to try
            thread.join(timeout=2)

        # Lock should not have been acquired by thread
        assert not lock_acquired.is_set()
        assert error_occurred.is_set()


@mock_aws
class TestConflictDetection:
    """Tests for concurrent write conflict detection."""

    def setup_method(self, method):
        """Set up S3 mock and test bucket."""
        self.bucket_name = "test-memory-bucket"
        self.key_name = "test/memories.db"
        self.cloud_url = f"s3://{self.bucket_name}/{self.key_name}"

        # Create mock S3 bucket
        self.s3_client = boto3.client("s3", region_name="us-east-1")
        self.s3_client.create_bucket(Bucket=self.bucket_name)

    def test_concurrent_write_conflict_detection(self, tmp_path):
        """Test that concurrent writes are detected and raise ConflictError."""
        from memora.backends import ConflictError

        # Agent 1: Create and upload initial database
        backend1 = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path / "agent1",
        )

        conn1 = backend1.connect()
        conn1.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value TEXT)")
        conn1.execute("INSERT INTO test (value) VALUES ('agent1_v1')")
        conn1.commit()
        conn1.close()
        backend1.sync_after_write()

        # Agent 2: Download the same database
        backend2 = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path / "agent2",
        )
        backend2.sync_before_use()

        # Agent 1: Make a change and upload
        conn1 = backend1.connect()
        conn1.execute("INSERT INTO test (value) VALUES ('agent1_v2')")
        conn1.commit()
        conn1.close()
        backend1.sync_after_write()

        # Agent 2: Make a local change WITHOUT syncing first
        # (simulates making changes while offline or before sync)
        # Don't use connect() because it will sync_before_use()
        import sqlite3
        conn2 = sqlite3.connect(backend2.cache_path)
        conn2.execute("INSERT INTO test (value) VALUES ('agent2_v1')")
        conn2.commit()
        conn2.close()

        # Mark as dirty manually (normally done by TrackedConnection)
        backend2._is_dirty = True

        # This should raise ConflictError when trying to upload
        with pytest.raises(ConflictError, match="modified by another process"):
            backend2.sync_after_write()

    def test_no_conflict_after_sync_pull(self, tmp_path):
        """Test that sync_pull resolves conflicts."""

        # Agent 1: Create and upload initial database
        backend1 = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path / "agent1",
        )

        conn1 = backend1.connect()
        conn1.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value TEXT)")
        conn1.execute("INSERT INTO test (value) VALUES ('agent1_v1')")
        conn1.commit()
        conn1.close()
        backend1.sync_after_write()

        # Agent 2: Download the same database
        backend2 = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path / "agent2",
        )
        backend2.sync_before_use()

        # Agent 1: Make a change and upload
        conn1 = backend1.connect()
        conn1.execute("INSERT INTO test (value) VALUES ('agent1_v2')")
        conn1.commit()
        conn1.close()
        backend1.sync_after_write()

        # Agent 2: Pull latest changes before writing
        backend2.force_sync_pull()

        # Agent 2: Now make a change (should work)
        conn2 = backend2.connect()
        conn2.execute("INSERT INTO test (value) VALUES ('agent2_v1')")
        conn2.commit()
        conn2.close()

        # This should succeed now
        backend2.sync_after_write()  # Should not raise


@mock_aws
class TestReadOptimization:
    """Tests for read-heavy workload optimization."""

    def setup_method(self, method):
        """Set up S3 mock and test bucket."""
        self.bucket_name = "test-memory-bucket"
        self.key_name = "test/memories.db"
        self.cloud_url = f"s3://{self.bucket_name}/{self.key_name}"

        # Create mock S3 bucket
        self.s3_client = boto3.client("s3", region_name="us-east-1")
        self.s3_client.create_bucket(Bucket=self.bucket_name)

    def test_read_operations_dont_trigger_hash(self, tmp_path):
        """Test that read operations don't hash the database."""
        # Create initial database
        backend = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path,
        )

        conn = backend.connect()
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value TEXT)")
        for i in range(100):
            conn.execute(f"INSERT INTO test (value) VALUES ('value_{i}')")
        conn.commit()
        conn.close()
        backend.sync_after_write()

        # Reset dirty flag
        backend._is_dirty = False

        # Perform multiple read operations
        for _ in range(10):
            # Call sync_after_write (simulating what would happen after a read)
            # This should skip immediately due to dirty flag
            backend.sync_after_write()

        # Verify no upload occurred (would fail if it tried to upload without changes)
        assert not backend._is_dirty

    def test_dirty_flag_prevents_unnecessary_uploads(self, tmp_path):
        """Test that dirty flag prevents uploads when database unchanged."""
        backend = CloudSQLiteBackend(
            cloud_url=self.cloud_url,
            cache_dir=tmp_path,
        )

        conn = backend.connect()
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        backend.sync_after_write()

        # Get initial ETag
        response1 = self.s3_client.head_object(
            Bucket=self.bucket_name,
            Key=self.key_name
        )
        etag1 = response1["ETag"]

        # Reset dirty flag
        backend._is_dirty = False

        # Call sync_after_write multiple times
        for _ in range(5):
            backend.sync_after_write()

        # Verify no new upload occurred (ETag unchanged)
        response2 = self.s3_client.head_object(
            Bucket=self.bucket_name,
            Key=self.key_name
        )
        etag2 = response2["ETag"]

        assert etag1 == etag2, "ETag changed, indicating an unnecessary upload occurred"
