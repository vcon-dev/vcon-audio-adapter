#!/usr/bin/env python3
"""Main entry point for audio file vCon adapter."""

import sys
import signal
import logging
import threading
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Tuple
from audio_adapter.config import Config
from audio_adapter.parser import FilenameParser
from audio_adapter.builder import VconBuilder
from audio_adapter.poster import HttpPoster
from audio_adapter.tracker import StateTracker
from audio_adapter.monitor import FileSystemMonitor
from audio_adapter.directory_iterator import DirectoryIterator


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

        # Initialize directory iterator for iterator mode
        self.directory_iterator = None
        if config.traverse_mode == "iterator":
            self.directory_iterator = DirectoryIterator(
                base_directory=config.base_directory,
                supported_formats=config.supported_formats,
                state_file=config.directory_state_file,
                batch_size=config.batch_size,
                sort_order=config.sort_order
            )
            logger.info(f"Directory iterator mode: {config.base_directory}")
            stats = self.directory_iterator.get_statistics()
            logger.info(
                f"Progress: {stats['completed_directories']}/{stats['total_directories']} "
                f"directories completed, {stats['pending_directories']} pending"
            )

        # Initialize monitor based on source type (for single mode or watching)
        self.monitor = None
        if config.source_type == "filesystem":
            watch_dir = config.watch_directory if config.traverse_mode == "single" else None
            if watch_dir:
                self.monitor = FileSystemMonitor(
                    watch_dir,
                    config.supported_formats,
                    self._process_file
                )
        elif config.source_type == "s3":
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

    def _process_batch(self, files: list) -> Tuple[int, int]:
        """Process a batch of files.

        Args:
            files: List of file paths to process

        Returns:
            Tuple of (success_count, error_count)
        """
        start_time = time.time()
        success_count = 0
        error_count = 0

        if self.parallel_posts > 1:
            # Process files in parallel using thread pool
            with ThreadPoolExecutor(max_workers=self.parallel_posts) as executor:
                futures = {
                    executor.submit(self._process_file, filepath): filepath
                    for filepath in files
                }

                for future in as_completed(futures):
                    filepath = futures[future]
                    try:
                        future.result()
                        success_count += 1
                    except Exception as e:
                        error_count += 1
                        logger.error(f"Error processing {filepath}: {e}")
        else:
            # Process files sequentially
            for filepath in files:
                try:
                    self._process_file(filepath)
                    success_count += 1
                except Exception as e:
                    error_count += 1
                    logger.error(f"Error processing {filepath}: {e}")

        # Flush tracker state to disk at end of batch
        self.tracker.flush()

        elapsed = time.time() - start_time
        rate = len(files) / elapsed if elapsed > 0 else 0
        logger.info(
            f"Batch complete: {len(files)} files "
            f"({success_count} success, {error_count} errors) "
            f"in {elapsed:.1f}s ({rate:.1f} files/sec)"
        )

        return success_count, error_count

    def _wait_for_backpressure(self):
        """Block until queue depth drops below backpressure threshold."""
        threshold = self.config.backpressure_threshold
        if threshold <= 0 or not self.config.backpressure_url:
            return
        url = self.config.backpressure_url
        params = {"list_name": self.config.backpressure_queue}
        poll_interval = self.config.backpressure_poll_interval
        waiting = False
        while self.running:
            try:
                resp = requests.get(url, params=params, timeout=5)
                resp.raise_for_status()
                depth = resp.json().get("depth", 0)
            except Exception as e:
                logger.warning(f"Backpressure check failed: {e}")
                if waiting:
                    logger.info("Backpressure check unreachable, resuming")
                return
            if depth < threshold:
                if waiting:
                    logger.info(f"Backpressure released: depth {depth} < {threshold}, resuming")
                return
            if not waiting:
                waiting = True
                logger.info(f"Backpressure active: depth {depth} >= {threshold}, waiting (poll every {poll_interval}s)")
            time.sleep(poll_interval)

    def process_with_iterator(self):
        """Process files using directory iterator with checkpointing."""
        if not self.directory_iterator:
            logger.error("Directory iterator not initialized")
            return

        logger.info("Starting directory iterator processing...")
        total_success = 0
        total_errors = 0
        total_start = time.time()

        while self.running:
            # Get next batch
            directory, files = self.directory_iterator.get_next_batch()

            if directory is None:
                logger.info("All directories processed!")
                break

            if not files:
                continue

            self._wait_for_backpressure()
            if not self.running:
                break

            logger.info(f"Processing directory: {directory} ({len(files)} files)")

            # Process the batch
            success, errors = self._process_batch(files)
            total_success += success
            total_errors += errors

            # Checkpoint progress
            self.directory_iterator.mark_files_processed(len(files), files[-1] if files else None)

            # Check if directory is complete
            stats = self.directory_iterator.get_statistics()
            if stats['current_directory'] is None:
                logger.info(
                    f"Progress: {stats['completed_directories']}/{stats['total_directories']} "
                    f"directories completed"
                )

        total_elapsed = time.time() - total_start
        total_files = total_success + total_errors
        rate = total_files / total_elapsed if total_elapsed > 0 else 0

        logger.info(
            f"Iterator processing complete: {total_files} total files "
            f"({total_success} success, {total_errors} errors) "
            f"in {total_elapsed:.1f}s ({rate:.1f} files/sec overall)"
        )

        # Print final statistics
        stats = self.directory_iterator.get_statistics()
        logger.info(
            f"Final stats: {stats['completed_directories']}/{stats['total_directories']} "
            f"directories completed"
        )

    def process_existing_files(self):
        """Process existing files in the watch directory (single mode)."""
        if not self.config.process_existing:
            logger.info("Skipping existing files (PROCESS_EXISTING=false)")
            return

        if not self.monitor:
            logger.warning("No monitor configured for single mode")
            return

        logger.info("Processing existing files...")
        existing_files = self.monitor.get_existing_files(max_files=self.config.max_files)

        if self.config.max_files > 0:
            logger.info(f"Limited to {self.config.max_files} files")

        if not existing_files:
            logger.info("No existing files found")
            return

        logger.info(f"Processing {len(existing_files)} files with {self.parallel_posts} parallel workers")
        success, errors = self._process_batch(existing_files)

        logger.info(f"Finished processing existing files: {success} success, {errors} errors")

    def start(self):
        """Start the adapter."""
        logger.info("Starting audio file vCon adapter...")
        self.running = True

        if self.config.traverse_mode == "iterator":
            # Iterator mode: process directories with checkpointing
            self.process_with_iterator()
        else:
            # Single mode: process existing files then monitor
            self.process_existing_files()

            # Start monitoring for new files if monitor is configured
            if self.monitor:
                self.monitor.start()
                logger.info("Adapter is running. Press Ctrl+C to stop.")

                # Keep running until interrupted
                try:
                    while self.running:
                        time.sleep(1)
                except KeyboardInterrupt:
                    logger.info("Received interrupt signal")
                finally:
                    self.stop()
            else:
                logger.info("Processing complete (no monitoring configured)")

    def stop(self):
        """Stop the adapter."""
        if self.running:
            logger.info("Stopping adapter...")
            self.running = False
            if self.monitor:
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
