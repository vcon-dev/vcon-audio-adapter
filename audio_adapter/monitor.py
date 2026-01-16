"""File system monitor to watch for new audio files."""

import logging
import time
from pathlib import Path
from typing import Callable, List
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


logger = logging.getLogger(__name__)


class AudioFileHandler(FileSystemEventHandler):
    """Handles file system events for audio files."""

    def __init__(
        self,
        supported_formats: List[str],
        callback: Callable[[str], None]
    ):
        """Initialize handler.

        Args:
            supported_formats: List of supported file extensions (lowercase)
            callback: Function to call when a new audio file is detected
        """
        self.supported_formats = set(ext.lower() for ext in supported_formats)
        self.callback = callback

    def on_created(self, event):
        """Handle file creation event."""
        if event.is_directory:
            return

        filepath = event.src_path
        extension = Path(filepath).suffix.lstrip('.').lower()

        if extension in self.supported_formats:
            logger.info(f"New audio file detected: {filepath}")
            # Small delay to ensure file is fully written
            time.sleep(0.5)
            self.callback(filepath)


class FileSystemMonitor:
    """Monitors file system for new audio files."""

    def __init__(
        self,
        watch_directory: str,
        supported_formats: List[str],
        callback: Callable[[str], None]
    ):
        """Initialize file system monitor.

        Args:
            watch_directory: Directory to monitor
            supported_formats: List of supported file extensions
            callback: Function to call when a new audio file is detected
        """
        self.watch_directory = Path(watch_directory)
        if not self.watch_directory.exists():
            raise ValueError(f"Watch directory does not exist: {watch_directory}")

        self.supported_formats = supported_formats
        self.callback = callback
        self.observer = None
        self.handler = AudioFileHandler(supported_formats, callback)

    def start(self):
        """Start monitoring for new files."""
        self.observer = Observer()
        self.observer.schedule(
            self.handler,
            str(self.watch_directory),
            recursive=False
        )
        self.observer.start()
        logger.info(f"Started monitoring directory: {self.watch_directory}")

    def stop(self):
        """Stop monitoring."""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            logger.info("Stopped file system monitoring")

    def get_existing_files(self) -> List[str]:
        """Get list of existing audio files in the directory.

        Returns:
            List of file paths
        """
        existing_files = []
        supported_set = set(ext.lower() for ext in self.supported_formats)

        try:
            for filepath in self.watch_directory.iterdir():
                if filepath.is_file():
                    extension = filepath.suffix.lstrip('.').lower()
                    if extension in supported_set:
                        existing_files.append(str(filepath.absolute()))
        except Exception as e:
            logger.error(f"Error scanning directory for existing files: {e}")

        logger.info(f"Found {len(existing_files)} existing audio files")
        return existing_files
