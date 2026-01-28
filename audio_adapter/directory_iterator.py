"""Directory iterator for traversing date/hour organized recording directories.

Supports checkpointing to resume processing after interruption.

Directory structure expected:
    {base_directory}/{date}/{hour}/*.wav
    e.g., /mnt/nas/Freeswitch1/2026-01-19/06/*.wav
"""

import json
import logging
import os
from pathlib import Path
from typing import Iterator, List, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class DirectoryProgress:
    """Tracks progress through directories."""
    current_directory: Optional[str] = None
    files_processed: int = 0
    completed_directories: List[str] = None
    last_file: Optional[str] = None
    started_at: Optional[str] = None
    updated_at: Optional[str] = None

    def __post_init__(self):
        if self.completed_directories is None:
            self.completed_directories = []


class DirectoryIterator:
    """Iterates through date/hour directories with checkpointing support."""

    def __init__(
        self,
        base_directory: str,
        supported_formats: List[str],
        state_file: str = ".directory_progress.json",
        batch_size: int = 1000,
        sort_order: str = "oldest_first"
    ):
        """Initialize directory iterator.

        Args:
            base_directory: Root directory containing date subdirectories
            supported_formats: List of audio file extensions to process
            state_file: Path to checkpoint file
            batch_size: Number of files to process before checkpointing
            sort_order: "oldest_first" or "newest_first"
        """
        self.base_directory = Path(base_directory)
        self.supported_formats = set(ext.lower().lstrip('.') for ext in supported_formats)
        self.state_file = Path(state_file)
        self.batch_size = batch_size
        self.sort_order = sort_order
        self.progress = self._load_progress()

    def _load_progress(self) -> DirectoryProgress:
        """Load progress from checkpoint file."""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    progress = DirectoryProgress(**data)
                    logger.info(
                        f"Loaded progress: {len(progress.completed_directories)} directories completed, "
                        f"current: {progress.current_directory}, files: {progress.files_processed}"
                    )
                    return progress
            except Exception as e:
                logger.warning(f"Could not load progress file: {e}")
        return DirectoryProgress(started_at=datetime.utcnow().isoformat())

    def _save_progress(self):
        """Save progress to checkpoint file."""
        self.progress.updated_at = datetime.utcnow().isoformat()
        try:
            with open(self.state_file, 'w') as f:
                json.dump(asdict(self.progress), f, indent=2)
        except Exception as e:
            logger.error(f"Could not save progress file: {e}")

    def _discover_directories(self) -> List[Path]:
        """Discover all date/hour directories to process.

        Returns:
            List of directory paths sorted by date/hour
        """
        directories = []

        if not self.base_directory.exists():
            logger.error(f"Base directory does not exist: {self.base_directory}")
            return directories

        # Find all date directories (YYYY-MM-DD format)
        for date_dir in sorted(self.base_directory.iterdir()):
            if not date_dir.is_dir():
                continue
            # Check if it looks like a date directory
            if not self._is_date_directory(date_dir.name):
                continue

            # Find hour subdirectories
            for hour_dir in sorted(date_dir.iterdir()):
                if not hour_dir.is_dir():
                    continue
                # Check if it looks like an hour directory (00-23)
                if not self._is_hour_directory(hour_dir.name):
                    continue

                directories.append(hour_dir)

        # Sort by path (which gives chronological order for YYYY-MM-DD/HH format)
        directories.sort(key=lambda p: str(p))

        if self.sort_order == "newest_first":
            directories.reverse()

        logger.info(f"Discovered {len(directories)} directories to process")
        return directories

    def _is_date_directory(self, name: str) -> bool:
        """Check if directory name looks like a date (YYYY-MM-DD)."""
        try:
            datetime.strptime(name, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    def _is_hour_directory(self, name: str) -> bool:
        """Check if directory name looks like an hour (00-23)."""
        try:
            hour = int(name)
            return 0 <= hour <= 23
        except ValueError:
            return False

    def _get_files_in_directory(self, directory: Path) -> List[str]:
        """Get list of audio files in a directory.

        Args:
            directory: Directory to scan

        Returns:
            Sorted list of file paths
        """
        files = []
        try:
            for ext in self.supported_formats:
                for filepath in directory.glob(f"*.{ext}"):
                    if filepath.is_file():
                        files.append(str(filepath.absolute()))
        except PermissionError as e:
            logger.warning(f"Permission denied accessing {directory}: {e}")
        except Exception as e:
            logger.error(f"Error scanning directory {directory}: {e}")

        files.sort()
        return files

    def get_pending_directories(self) -> List[Path]:
        """Get list of directories that haven't been completed.

        Returns:
            List of directory paths still needing processing
        """
        all_dirs = self._discover_directories()
        completed = set(self.progress.completed_directories)
        return [d for d in all_dirs if str(d) not in completed]

    def get_next_batch(self) -> Tuple[Optional[str], List[str]]:
        """Get the next batch of files to process.

        Returns:
            Tuple of (directory_path, list_of_files)
            Returns (None, []) if all directories are complete
        """
        pending = self.get_pending_directories()

        if not pending:
            logger.info("All directories have been processed")
            return None, []

        # Get current or next directory
        if self.progress.current_directory:
            current = Path(self.progress.current_directory)
            if current in pending:
                directory = current
            else:
                directory = pending[0]
                self.progress.current_directory = str(directory)
                self.progress.files_processed = 0
                self.progress.last_file = None
        else:
            directory = pending[0]
            self.progress.current_directory = str(directory)
            self.progress.files_processed = 0

        # Get files from directory
        all_files = self._get_files_in_directory(directory)

        # Skip already processed files
        start_idx = self.progress.files_processed
        if start_idx >= len(all_files):
            # Directory is complete
            self.mark_directory_complete(str(directory))
            return self.get_next_batch()  # Recurse to get next directory

        # Get batch
        end_idx = min(start_idx + self.batch_size, len(all_files))
        batch = all_files[start_idx:end_idx]

        logger.info(
            f"Directory {directory.name}: returning files {start_idx+1}-{end_idx} of {len(all_files)}"
        )

        return str(directory), batch

    def mark_files_processed(self, count: int, last_file: Optional[str] = None):
        """Mark files as processed and checkpoint.

        Args:
            count: Number of files processed in this batch
            last_file: Path to last processed file
        """
        self.progress.files_processed += count
        if last_file:
            self.progress.last_file = last_file
        self._save_progress()
        logger.debug(f"Checkpointed: {self.progress.files_processed} files in {self.progress.current_directory}")

    def mark_directory_complete(self, directory: str):
        """Mark a directory as fully processed.

        Args:
            directory: Path to completed directory
        """
        if directory not in self.progress.completed_directories:
            self.progress.completed_directories.append(directory)
        self.progress.current_directory = None
        self.progress.files_processed = 0
        self.progress.last_file = None
        self._save_progress()
        logger.info(f"Completed directory: {directory}")

    def get_statistics(self) -> dict:
        """Get processing statistics.

        Returns:
            Dictionary with progress statistics
        """
        all_dirs = self._discover_directories()
        pending = self.get_pending_directories()

        return {
            "total_directories": len(all_dirs),
            "completed_directories": len(self.progress.completed_directories),
            "pending_directories": len(pending),
            "current_directory": self.progress.current_directory,
            "files_in_current": self.progress.files_processed,
            "started_at": self.progress.started_at,
            "updated_at": self.progress.updated_at,
        }

    def reset_progress(self):
        """Reset all progress (start fresh)."""
        self.progress = DirectoryProgress(started_at=datetime.utcnow().isoformat())
        self._save_progress()
        logger.info("Progress reset")
