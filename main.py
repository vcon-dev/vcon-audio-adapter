#!/usr/bin/env python3
"""Main entry point for audio file vCon adapter."""

import sys
import signal
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


class RateLimiter:
    """Token bucket rate limiter for controlling request throughput."""

    def __init__(self, rate: float):
        """Initialize rate limiter.

        Args:
            rate: Maximum requests per second (0 = no limit)
        """
        self.rate = rate
        self.tokens = 1.0
        self.last_time = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        """Acquire a token, blocking if necessary to respect rate limit."""
        if self.rate <= 0:
            return  # No rate limiting

        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_time
            self.last_time = now

            # Add tokens based on elapsed time
            self.tokens = min(1.0, self.tokens + elapsed * self.rate)

            if self.tokens < 1.0:
                # Need to wait for token
                wait_time = (1.0 - self.tokens) / self.rate
                time.sleep(wait_time)
                self.tokens = 0.0
            else:
                self.tokens -= 1.0


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

        # Initialize rate limiter (0 = no limit)
        self.rate_limiter = RateLimiter(config.rate_limit)
        if config.rate_limit > 0:
            logger.info(f"Rate limiting enabled: {config.rate_limit} requests/sec")

        # Initialize thread pool for parallel posting
        self.parallel_posts = config.parallel_posts
        if self.parallel_posts > 1:
            logger.info(f"Parallel posting enabled: {self.parallel_posts} workers")

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

        trunk, sender, receiver, extension = parsed

        # Build vCon
        vcon = self.builder.build(filepath, sender, receiver, extension, trunk=trunk)
        if not vcon:
            logger.error(f"Failed to build vCon from: {filepath}")
            return

        # Apply rate limiting before posting
        self.rate_limiter.acquire()

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
        existing_files = self.monitor.get_existing_files(max_files=self.config.max_files)

        if self.config.max_files > 0:
            logger.info(f"Limited to {self.config.max_files} files")

        start_time = time.time()

        if self.parallel_posts > 1:
            # Process files in parallel using thread pool
            logger.info(f"Processing {len(existing_files)} files with {self.parallel_posts} parallel workers")
            success_count = 0
            error_count = 0

            with ThreadPoolExecutor(max_workers=self.parallel_posts) as executor:
                futures = {
                    executor.submit(self._process_file, filepath): filepath
                    for filepath in existing_files
                }

                for future in as_completed(futures):
                    filepath = futures[future]
                    try:
                        future.result()
                        success_count += 1
                    except Exception as e:
                        error_count += 1
                        logger.error(f"Error processing {filepath}: {e}")

            elapsed = time.time() - start_time
            rate = len(existing_files) / elapsed if elapsed > 0 else 0
            logger.info(
                f"Finished processing {len(existing_files)} files "
                f"({success_count} success, {error_count} errors) "
                f"in {elapsed:.1f}s ({rate:.1f} files/sec)"
            )
        else:
            # Process files sequentially
            for filepath in existing_files:
                self._process_file(filepath)

            elapsed = time.time() - start_time
            rate = len(existing_files) / elapsed if elapsed > 0 else 0
            logger.info(
                f"Finished processing {len(existing_files)} files "
                f"in {elapsed:.1f}s ({rate:.1f} files/sec)"
            )

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
