"""Layered Backblaze B2 Tests: Error Handling, Consistency, and Single Real B2 Smoke Test."""

import pytest
import uuid
from botocore.exceptions import ClientError
from unittest.mock import patch, MagicMock

from app.config import get_settings
from app.b2 import (
    get_b2_client,
    create_b2_object_async,
    delete_b2_object_async,
    upload_b2_file_async,
    check_b2_object_exists_async,
    delete_b2_prefix_versions,
)

settings = get_settings()


class TestB2MockedFailureHandling:
    """Level 1: Tests simulating B2 failures, rate limits, timeouts without touching production B2."""

    async def test_b2_timeout_handling_returns_gracefully(self):
        """Simulate B2 timeout and verify it does not crash."""
        with patch("app.b2.get_b2_client") as mock_client:
            mock_client.return_value.put_object.side_effect = TimeoutError("B2 Connection Timed Out")
            res = await create_b2_object_async("test/timeout.txt", b"test")
            assert res is False

    async def test_b2_rate_limit_simulation_returns_gracefully(self):
        """Simulate 429 Too Many Requests response from B2 S3 API."""
        with patch("app.b2.get_b2_client") as mock_client:
            err_response = {"Error": {"Code": "SlowDown", "Message": "Please reduce your request rate."}}
            mock_client.return_value.put_object.side_effect = ClientError(err_response, "PutObject")
            res = await upload_b2_file_async("test/rate_limit.txt", b"test")
            assert res is False

    async def test_b2_object_not_found_handling(self):
        """Simulate 404 NoSuchKey and verify check returns False cleanly."""
        with patch("app.b2.get_b2_client") as mock_client:
            err_response = {"Error": {"Code": "404", "Message": "Not Found"}}
            mock_client.return_value.head_object.side_effect = ClientError(err_response, "HeadObject")
            exists = await check_b2_object_exists_async("test/nonexistent.keep")
            assert exists is False


class TestRealB2SmokeTest:
    """Level 3: Exactly ONE minimal real Backblaze B2 smoke test to verify live auth and S3 operations."""

    async def test_single_real_b2_smoke_test(self):
        """
        Executes exactly ONE isolated end-to-end smoke test against Backblaze B2:
        1. Upload tiny probe file (32 bytes)
        2. Verify head_object exists
        3. Delete object & purge versions
        4. Verify deletion
        """
        if not (settings.B2_KEY_ID and settings.B2_APPLICATION_KEY and settings.B2_BUCKET):
            pytest.skip("Backblaze B2 credentials not present in environment.")

        probe_id = uuid.uuid4().hex[:8]
        test_key = f"smoke_tests/smoke_probe_{probe_id}.txt"
        test_content = b"GetFileNova B2 Live Smoke Test OK"

        print(f"\n[B2 SMOKE TEST] Uploading 1 probe file to key: {test_key}...")
        # Step 1: Upload 1 tiny probe
        upload_success = await upload_b2_file_async(test_key, test_content)
        assert upload_success is True

        # Step 2: Verify it exists in B2
        exists_after_upload = await check_b2_object_exists_async(test_key)
        assert exists_after_upload is True
        print("[B2 SMOKE TEST] Probe verified in Backblaze B2 bucket!")

        # Step 3: Delete probe and purge versions
        del_success = await delete_b2_object_async(test_key)
        assert del_success is True

        # Purge any residual delete markers for versioned bucket hygiene
        delete_b2_prefix_versions(test_key)

        # Step 4: Verify deletion
        exists_after_delete = await check_b2_object_exists_async(test_key)
        assert exists_after_delete is False
        print("[B2 SMOKE TEST] Probe successfully cleaned up from Backblaze B2.")
