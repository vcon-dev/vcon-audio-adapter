#!/usr/bin/env python3
"""Main entry point for audio file vCon adapter."""

import sys
import signal
import logging
from pathlib import Path
from audio_adapter.config import Config
from audio_adapter.parser import FilenameParser
from audio_adapter.builder import VconBuilder
from audio_adapter.poster import HttpPoster
from audio_adapter.tracker import StateTracker
from audio_adapter.monitor import FileSystemMonitor


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class AudioAdapter:
    """Main adapter class that orchestrates all components."""

    def __init__(self, config: Config):
        """Initialize adapter with configuration."""
        self.config = config

        # Initialize components
        self.parser = FilenameParser(config.get_filename_regex())
        self.builder = VconBuilder(
            dialog_type=config.dialog_type,
            extract_duration=config.extract_duration
        )
        self.poster = HttpPoster(
            config.conserver_url,
            config.get_headers(),
            config.ingress_lists
        )
        self.tracker = StateTracker(config.state_file)

        # Initialize monitor based on source type
        if config.source_type == "filesystem":
            self.monitor = FileSystemMonitor(
                config.watch_directory,
                config.supported_formats,
                self._process_file
            )
        elif config.source_type == "s3":
            # S3 monitor would be imported and initialized here
            # For now, raise an error as S3 support needs additional implementation
            raise NotImplementedError(
                "S3 support not yet implemented. "
                "Copy s3_monitor.py from vcon-fadapter if needed."
            )
        else:
            raise ValueError(f"Invalid source type: {config.source_type}")

        self.running = False

    def _process_file(self, filepath: str):
        """Process a single audio file from filesystem.

        Args:
            filepath: Path to the audio file
        """
        # Check if already processed
        if self.tracker.is_processed(filepath):
            logger.debug(f"Skipping already processed file: {filepath}")
            return

        # Parse filename
        parsed = self.parser.parse(filepath)
        if not parsed:
            logger.warning(f"Could not parse filename: {filepath}")
            return

        sender, receiver, extension = parsed

        # Build vCon
        vcon = self.builder.build(filepath, sender, receiver, extension)
        if not vcon:
            logger.error(f"Failed to build vCon from: {filepath}")
            return

        # Post to conserver
        success = self.poster.post(vcon)

        if success:
            # Mark as processed
            self.tracker.mark_processed(filepath, vcon.uuid, "success")

            # Delete file if configured
            if self.config.delete_after_send:
                try:
                    Path(filepath).unlink()
                    logger.info(f"Deleted file after successful post: {filepath}")
                except Exception as e:
                    logger.warning(f"Failed to delete file {filepath}: {e}")
        else:
            # Mark as failed but don't delete
            self.tracker.mark_processed(filepath, vcon.uuid, "failed")
            logger.error(f"Failed to post vCon for: {filepath}")

    def process_existing_files(self):
        """Process existing files in the watch directory."""
        if not self.config.process_existing:
            logger.info("Skipping existing files (PROCESS_EXISTING=false)")
            return

        logger.info("Processing existing files...")
        existing_files = self.monitor.get_existing_files()

        for filepath in existing_files:
            self._process_file(filepath)

        logger.info(f"Finished processing {len(existing_files)} existing files")

    def start(self):
        """Start the adapter."""
        logger.info("Starting audio file vCon adapter...")

        # Process existing files first
        self.process_existing_files()

        # Start monitoring for new files
        self.monitor.start()
        self.running = True

        logger.info("Adapter is running. Press Ctrl+C to stop.")

        # Keep running until interrupted
        try:
            while self.running:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
        finally:
            self.stop()

    def stop(self):
        """Stop the adapter."""
        if self.running:
            logger.info("Stopping adapter...")
            self.running = False
            self.monitor.stop()
            logger.info("Adapter stopped")


def main():
    """Main entry point."""
    try:
        # Load configuration
        config = Config()

        # Create adapter
        adapter = AudioAdapter(config)

        # Set up signal handlers for graceful shutdown
        def signal_handler(sig, frame):
            logger.info("Received shutdown signal")
            adapter.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Start adapter
        adapter.start()

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
