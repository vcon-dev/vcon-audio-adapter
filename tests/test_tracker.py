"""Tests for state tracker."""

import json
import pytest
from audio_adapter.tracker import StateTracker


class TestStateTracker:
    """Test cases for StateTracker."""

    def test_is_processed_returns_false_for_new_file(self, tmp_path):
        """Test that new files are not marked as processed."""
        state_file = tmp_path / "state.json"
        tracker = StateTracker(str(state_file))

        result = tracker.is_processed("/path/to/new_file.wav")

        assert result is False

    def test_mark_processed_and_check(self, tmp_path):
        """Test marking a file as processed."""
        state_file = tmp_path / "state.json"
        tracker = StateTracker(str(state_file))

        tracker.mark_processed("/path/to/file.wav", "uuid-123", "success")

        assert tracker.is_processed("/path/to/file.wav") is True

    def test_get_vcon_uuid(self, tmp_path):
        """Test retrieving vCon UUID for processed file."""
        state_file = tmp_path / "state.json"
        tracker = StateTracker(str(state_file))

        tracker.mark_processed("/path/to/file.wav", "uuid-456", "success")
        uuid = tracker.get_vcon_uuid("/path/to/file.wav")

        assert uuid == "uuid-456"

    def test_state_persists_to_file(self, tmp_path):
        """Test that state is persisted to file."""
        state_file = tmp_path / "state.json"
        tracker = StateTracker(str(state_file))

        tracker.mark_processed("/path/to/file.wav", "uuid-789", "success")

        # Read file directly
        with open(state_file) as f:
            data = json.load(f)

        assert "/path/to/file.wav" in data
        assert data["/path/to/file.wav"]["vcon_uuid"] == "uuid-789"

    def test_state_loads_from_existing_file(self, tmp_path):
        """Test that state is loaded from existing file."""
        state_file = tmp_path / "state.json"

        # Create pre-existing state file
        with open(state_file, "w") as f:
            json.dump({
                "/existing/file.wav": {
                    "vcon_uuid": "existing-uuid",
                    "timestamp": "2024-01-15T10:00:00",
                    "status": "success"
                }
            }, f)

        tracker = StateTracker(str(state_file))

        assert tracker.is_processed("/existing/file.wav") is True
        assert tracker.get_vcon_uuid("/existing/file.wav") == "existing-uuid"

    def test_s3_key_tracking(self, tmp_path):
        """Test tracking with S3 keys."""
        state_file = tmp_path / "state.json"
        tracker = StateTracker(str(state_file))

        tracker.mark_processed(
            "/tmp/local_copy.wav",
            "uuid-s3",
            "success",
            s3_key="bucket/path/file.wav"
        )

        # Should be tracked by S3 key
        assert tracker.is_processed("/tmp/local_copy.wav", s3_key="bucket/path/file.wav") is True
        # Local path alone shouldn't match
        assert tracker.is_processed("/tmp/local_copy.wav") is False

    def test_failed_status_tracking(self, tmp_path):
        """Test tracking failed processing."""
        state_file = tmp_path / "state.json"
        tracker = StateTracker(str(state_file))

        tracker.mark_processed("/path/to/failed.wav", "uuid-fail", "failed")

        assert tracker.is_processed("/path/to/failed.wav") is True

        # Verify status in state
        with open(state_file) as f:
            data = json.load(f)
        assert data["/path/to/failed.wav"]["status"] == "failed"
