"""Directory iterator for traversing date/hour organized recording directories.

Supports checkpointing to resume processing after interruption.

Directory structure expected:
    {base_directory}/{date}/{hour}/*.wav
    e.g., /mnt/nas/Freeswitch1/2026-01-19/06/*.wav

Also includes FileListIterator for pre-scanned file lists (no NFS scanning).
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
        # Cache for current directory's file list (avoids re-scanning NFS)
        self._cached_directory: Optional[str] = None
        self._cached_files: List[str] = []
        # Lazy iterator for discovered directories (avoids walking entire NAS tree upfront)
        self._directory_iter: Optional[Iterator[Path]] = None
        # Directories yielded so far (for statistics and pending checks)
        self._yielded_directories: List[Path] = []
        self._discovery_exhausted: bool = False

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

    def _discover_directories(self) -> Iterator[Path]:
        """Lazily discover date/hour directories one date at a time.

        Yields directories as they are found instead of walking the entire
        NAS tree upfront. This avoids a long startup delay on NFS mounts
        with thousands of directories.

        Yields:
            Directory paths in chronological order
        """
        if not self.base_directory.exists():
            logger.error(f"Base directory does not exist: {self.base_directory}")
            return

        # Get date directories (single readdir on base — fast)
        try:
            date_dirs = sorted(
                (d for d in self.base_directory.iterdir()
                 if d.is_dir() and self._is_date_directory(d.name)),
                key=lambda p: p.name,
                reverse=(self.sort_order == "newest_first")
            )
        except Exception as e:
            logger.error(f"Error listing base directory {self.base_directory}: {e}")
            return

        # Yield hour directories one date at a time (one readdir per date)
        for date_dir in date_dirs:
            try:
                hour_dirs = sorted(
                    (d for d in date_dir.iterdir()
                     if d.is_dir() and self._is_hour_directory(d.name)),
                    key=lambda p: p.name,
                    reverse=(self.sort_order == "newest_first")
                )
                for hour_dir in hour_dirs:
                    yield hour_dir
            except Exception as e:
                logger.warning(f"Error listing date directory {date_dir}: {e}")
                continue

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

        Uses os.scandir() for efficient NFS access — gets file type from
        dirent struct without extra stat() calls per file.

        Args:
            directory: Directory to scan

        Returns:
            Sorted list of file paths
        """
        files = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    name = entry.name
                    dot = name.rfind('.')
                    if dot < 0:
                        continue
                    ext = name[dot + 1:].lower()
                    if ext in self.supported_formats:
                        files.append(os.path.join(str(directory), name))
        except PermissionError as e:
            logger.warning(f"Permission denied accessing {directory}: {e}")
        except Exception as e:
            logger.error(f"Error scanning directory {directory}: {e}")

        files.sort()
        return files

    def _next_pending_directory(self) -> Optional[Path]:
        """Get the next directory that hasn't been completed.

        Pulls lazily from the directory iterator, only discovering
        new directories as needed.

        Returns:
            Next pending directory path, or None if exhausted
        """
        completed = set(self.progress.completed_directories)

        # First check if we're resuming a directory in progress
        if self.progress.current_directory:
            current = Path(self.progress.current_directory)
            if str(current) not in completed:
                return current

        # Initialize the lazy iterator on first call
        if self._directory_iter is None:
            self._directory_iter = self._discover_directories()

        # Pull from the lazy iterator until we find a pending directory
        while not self._discovery_exhausted:
            try:
                directory = next(self._directory_iter)
                self._yielded_directories.append(directory)
                if str(directory) not in completed:
                    return directory
            except StopIteration:
                self._discovery_exhausted = True
                logger.info(
                    f"Directory discovery complete: {len(self._yielded_directories)} total directories"
                )
                break

        return None

    def get_next_batch(self) -> Tuple[Optional[str], List[str]]:
        """Get the next batch of files to process.

        Discovers directories lazily — only reads the next date/hour
        directory from NFS when the current one is exhausted.

        Returns:
            Tuple of (directory_path, list_of_files)
            Returns (None, []) if all directories are complete
        """
        directory = self._next_pending_directory()

        if directory is None:
            logger.info("All directories have been processed")
            return None, []

        # Update progress if switching to a new directory
        if self.progress.current_directory != str(directory):
            self.progress.current_directory = str(directory)
            self.progress.files_processed = 0
            self.progress.last_file = None
            self._cached_directory = None

        # Get files from directory (use cache if available)
        dir_str = str(directory)
        if self._cached_directory == dir_str and self._cached_files:
            all_files = self._cached_files
            logger.debug(f"Using cached file list for {directory.name} ({len(all_files)} files)")
        else:
            logger.info(f"Scanning directory {directory.name}...")
            all_files = self._get_files_in_directory(directory)
            self._cached_directory = dir_str
            self._cached_files = all_files
            logger.info(f"Found {len(all_files)} files in {directory.name}")

        # Skip already processed files
        start_idx = self.progress.files_processed
        if start_idx >= len(all_files):
            # Directory is complete
            self.mark_directory_complete(str(directory))
            self._cached_directory = None
            self._cached_files = []
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
            Dictionary with progress statistics.
            Note: total_directories is only known after discovery is exhausted.
        """
        discovered = len(self._yielded_directories)
        completed = len(self.progress.completed_directories)

        return {
            "total_directories": discovered if self._discovery_exhausted else f"{discovered}+",
            "completed_directories": completed,
            "pending_directories": (discovered - completed) if self._discovery_exhausted else "unknown",
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


class FileListIterator:
    """Reads pre-scanned file lists from a local text file.

    Eliminates NFS directory scanning entirely — all file paths are read
    from a local file (one path per line) generated by a prior `find` command.
    Uses line-position checkpointing for efficient resume.
    """

    def __init__(
        self,
        file_list_path: str,
        state_file: str = ".filelist_progress.json",
        batch_size: int = 10000,
    ):
        self.file_list_path = file_list_path
        self.state_file = Path(state_file)
        self.batch_size = batch_size
        self.position = 0
        self.total_lines = 0
        self._load_state()
        self._count_total()

    def _load_state(self):
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    self.position = data.get("position", 0)
                    logger.info(f"FileList: resuming from position {self.position}")
            except Exception as e:
                logger.warning(f"Could not load filelist state: {e}")

    def _save_state(self):
        try:
            with open(self.state_file, 'w') as f:
                json.dump({"position": self.position}, f)
        except Exception as e:
            logger.error(f"Could not save filelist state: {e}")

    def _count_total(self):
        """Count total lines for progress reporting."""
        try:
            with open(self.file_list_path, 'r') as f:
                self.total_lines = sum(1 for _ in f)
            logger.info(f"FileList: {self.total_lines} total files, {self.position} already processed")
        except Exception as e:
            logger.error(f"Could not count filelist lines: {e}")

    def get_next_batch(self) -> Tuple[Optional[str], List[str]]:
        """Get next batch of file paths from the list.

        Returns:
            Tuple of (label, file_paths). Returns (None, []) when exhausted.
        """
        files = []
        try:
            with open(self.file_list_path, 'r') as f:
                # Seek to current position
                for _ in range(self.position):
                    line = f.readline()
                    if not line:
                        return None, []

                # Read batch_size lines
                for _ in range(self.batch_size):
                    line = f.readline()
                    if not line:
                        break
                    path = line.rstrip('\n')
                    if path:
                        files.append(path)
        except Exception as e:
            logger.error(f"Error reading filelist: {e}")
            return None, []

        if not files:
            logger.info("FileList: all files processed")
            return None, []

        pct = (self.position / self.total_lines * 100) if self.total_lines > 0 else 0
        label = f"filelist[{self.position}..{self.position + len(files)}] ({pct:.1f}%)"
        logger.info(f"FileList: returning {len(files)} files at position {self.position}/{self.total_lines}")
        return label, files

    def mark_files_processed(self, count: int, last_file: Optional[str] = None):
        self.position += count
        self._save_state()

    def mark_directory_complete(self, directory: str):
        pass  # No-op for filelist mode

    def get_statistics(self) -> dict:
        remaining = self.total_lines - self.position
        return {
            "total_directories": f"{self.total_lines} files",
            "completed_directories": self.position,
            "pending_directories": remaining,
            "current_directory": f"line {self.position}",
            "files_in_current": 0,
            "started_at": None,
            "updated_at": None,
        }

    def reset_progress(self):
        self.position = 0
        self._save_state()
        logger.info("FileList progress reset")
