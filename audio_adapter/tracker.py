"""State tracker to prevent reprocessing of files."""

import json
import logging
import threading
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime


logger = logging.getLogger(__name__)


class StateTracker:
    """Tracks processed files to avoid duplicates. Thread-safe for parallel processing."""

    def __init__(self, state_file: str):
        """Initialize state tracker.

        Args:
            state_file: Path to JSON file storing state
        """
        self.state_file = Path(state_file)
        self.state: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        """Load state from file."""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    self.state = json.load(f)
                logger.info(f"Loaded state for {len(self.state)} processed files")
            except Exception as e:
                logger.error(f"Error loading state file: {e}")
                self.state = {}
        else:
            self.state = {}

    def _save(self):
        """Save state to file."""
        try:
            with open(self.state_file, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving state file: {e}")

    def is_processed(self, filepath: str, s3_key: Optional[str] = None) -> bool:
        """Check if file has been processed.

        Args:
            filepath: Path to the file (or S3 key for S3 objects)
            s3_key: Optional S3 key for tracking S3 objects separately

        Returns:
            True if file has been processed
        """
        identifier = s3_key if s3_key else filepath
        with self._lock:
            return identifier in self.state

    def mark_processed(
        self,
        filepath: str,
        vcon_uuid: str,
        status: str = "success",
        s3_key: Optional[str] = None,
        etag: Optional[str] = None
    ):
        """Mark file as processed.

        Args:
            filepath: Path to the file
            vcon_uuid: UUID of the created vCon
            status: Processing status (success, failed, etc.)
            s3_key: Optional S3 key for S3 objects
            etag: Optional ETag for S3 object versioning
        """
        identifier = s3_key if s3_key else filepath
        entry = {
            "vcon_uuid": vcon_uuid,
            "timestamp": datetime.utcnow().isoformat(),
            "status": status
        }
        if s3_key:
            entry["s3_key"] = s3_key
        if etag:
            entry["etag"] = etag

        with self._lock:
            self.state[identifier] = entry
            self._save()
        logger.debug(f"Marked {identifier} as processed (status: {status})")

    def get_vcon_uuid(self, filepath: str, s3_key: Optional[str] = None) -> Optional[str]:
        """Get vCon UUID for a processed file.

        Args:
            filepath: Path to the file (or S3 key for S3 objects)
            s3_key: Optional S3 key for tracking S3 objects separately

        Returns:
            vCon UUID or None if not processed
        """
        identifier = s3_key if s3_key else filepath
        with self._lock:
            entry = self.state.get(identifier)
            return entry.get("vcon_uuid") if entry else None

    def is_s3_object_processed(self, s3_key: str, etag: Optional[str] = None) -> bool:
        """Check if S3 object has been processed.

        Args:
            s3_key: S3 object key
            etag: Optional ETag for version checking

        Returns:
            True if object has been processed with matching ETag (if provided)
        """
        with self._lock:
            if s3_key not in self.state:
                return False

            # If ETag provided, check if it matches
            if etag:
                stored_etag = self.state[s3_key].get("etag")
                return stored_etag == etag

            return True
