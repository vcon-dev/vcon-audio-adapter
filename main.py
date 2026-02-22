#!/usr/bin/env python3
"""Main entry point for audio file vCon adapter."""

import sys
import signal
import logging
import logging.handlers
import json as json_module
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
from audio_adapter.directory_iterator import DirectoryIterator, FileListIterator


# ---------------------------------------------------------------------------
# OTEL setup — optional, only activates if dependencies are installed
# ---------------------------------------------------------------------------
_tracer = None
_meter = None
_otel_available = False

# Metric instruments (populated in _init_otel)
_counter_files_processed = None
_histogram_file_duration = None
_histogram_batch_duration = None
_histogram_backpressure_wait = None

def _init_otel():
    """Initialize OpenTelemetry tracing and metrics for the audio adapter."""
    global _tracer, _meter, _otel_available
    global _counter_files_processed, _histogram_file_duration
    global _histogram_batch_duration, _histogram_backpressure_wait

    try:
        from opentelemetry import trace, metrics
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
    except ImportError:
        return

    endpoint = "http://localhost:4318"
    resource = Resource.create({"service.name": "audio-adapter"})

    # Traces
    trace_provider = TracerProvider(resource=resource)
    trace_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(trace_provider)
    _tracer = trace.get_tracer("audio-adapter")

    # Metrics
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
        export_interval_millis=15000,
    )
    metric_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(metric_provider)
    _meter = metrics.get_meter("audio-adapter")

    _counter_files_processed = _meter.create_counter(
        "adapter.files.processed",
        description="Files processed by the audio adapter",
    )
    _histogram_file_duration = _meter.create_histogram(
        "adapter.files.duration",
        description="Per-file processing time in seconds",
    )
    _histogram_batch_duration = _meter.create_histogram(
        "adapter.batch.duration",
        description="Per-batch processing time in seconds",
    )
    _histogram_backpressure_wait = _meter.create_histogram(
        "adapter.backpressure.wait_duration",
        description="Time spent waiting on backpressure in seconds",
    )

    # Auto-instrument the requests library
    RequestsInstrumentor().instrument()
    _otel_available = True


# ---------------------------------------------------------------------------
# JSON structured logging with trace correlation
# ---------------------------------------------------------------------------
class _JsonFormatter(logging.Formatter):
    """JSON log formatter that includes OTEL trace/span IDs when available."""

    def format(self, record):
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Add trace correlation if OTEL is available
        if _otel_available:
            try:
                from opentelemetry import trace as _trace
                span = _trace.get_current_span()
                ctx = span.get_span_context()
                if ctx and ctx.trace_id:
                    log_entry["trace_id"] = format(ctx.trace_id, "032x")
                    log_entry["span_id"] = format(ctx.span_id, "016x")
            except Exception:
                pass

        return json_module.dumps(log_entry)


# Configure logging — use JSON formatter for structured output
logging.basicConfig(level=logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(_JsonFormatter())
logging.root.handlers = [_handler]
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


class _NullTracker:
    """No-op state tracker for filelist mode.

    Filelist mode uses position-based checkpointing in FileListIterator,
    so per-file state tracking is unnecessary. This avoids 100MB+ JSON
    state files when processing millions of files.
    """

    def is_processed(self, filepath, s3_key=None):
        return False

    def mark_processed(self, filepath, vcon_uuid, status="success", s3_key=None, etag=None):
        pass

    def flush(self):
        pass


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

        # Initialize directory iterator for iterator/filelist mode
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
            logger.info(
                f"Resumed with {len(self.directory_iterator.progress.completed_directories)} "
                f"directories already completed (lazy discovery enabled)"
            )
        elif config.traverse_mode == "filelist":
            self.directory_iterator = FileListIterator(
                file_list_path=config.file_list,
                state_file=config.directory_state_file,
                batch_size=config.batch_size,
            )
            logger.info(f"Filelist mode: {config.file_list}")
            # Use a no-op tracker to avoid massive state files — position
            # checkpointing in FileListIterator handles resume.
            self.tracker = _NullTracker()

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

    def _process_file(self, filepath: str) -> str:
        """Process a single audio file from filesystem.

        Args:
            filepath: Path to the audio file

        Returns:
            "success", "skipped", or "error"
        """
        file_start = time.time()

        # Check if already processed
        if self.tracker.is_processed(filepath):
            logger.debug(f"Skipping already processed file: {filepath}")
            if _counter_files_processed:
                _counter_files_processed.add(1, {"status": "skipped"})
            return "skipped"

        # Wrap in a span if tracing is available
        span_cm = _tracer.start_as_current_span(
            "adapter.process_file",
            attributes={"file_path": filepath},
        ) if _tracer else None

        span = None
        if span_cm:
            span = span_cm.__enter__()

        try:
            result = self._process_file_inner(filepath)

            if span:
                span.set_attribute("result", result)
            if _counter_files_processed:
                _counter_files_processed.add(1, {"status": result})
            if _histogram_file_duration:
                _histogram_file_duration.record(time.time() - file_start, {"status": result})
            return result
        except Exception as e:
            if span:
                span.set_attribute("result", "error")
                span.record_exception(e)
            if _counter_files_processed:
                _counter_files_processed.add(1, {"status": "error"})
            raise
        finally:
            if span_cm:
                span_cm.__exit__(None, None, None)

    def _process_file_inner(self, filepath: str) -> str:
        """Inner file processing logic (extracted for span wrapping)."""
        # Parse filename
        parsed = self.parser.parse(filepath)
        if not parsed:
            logger.warning(f"Could not parse filename: {filepath}")
            return "error"

        trunk, sender, receiver, extension = parsed

        # Build vCon
        vcon = self.builder.build(filepath, sender, receiver, extension, trunk=trunk)
        if not vcon:
            logger.error(f"Failed to build vCon from: {filepath}")
            return "error"

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
            return "success"
        else:
            # Mark as failed but don't delete
            self.tracker.mark_processed(filepath, vcon.uuid, "failed")
            logger.error(f"Failed to post vCon for: {filepath}")
            return "error"

    def _process_batch(self, files: list) -> Tuple[int, int, int]:
        """Process a batch of files.

        Args:
            files: List of file paths to process

        Returns:
            Tuple of (success_count, error_count, skip_count)
        """
        batch_span_cm = _tracer.start_as_current_span(
            "adapter.process_batch",
            attributes={"batch_size": len(files)},
        ) if _tracer else None
        if batch_span_cm:
            batch_span_cm.__enter__()

        start_time = time.time()
        success_count = 0
        error_count = 0
        skip_count = 0

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
                        result = future.result()
                        if result == "skipped":
                            skip_count += 1
                        elif result == "error":
                            error_count += 1
                        else:
                            success_count += 1
                    except Exception as e:
                        error_count += 1
                        logger.error(f"Error processing {filepath}: {e}")
        else:
            # Process files sequentially
            for filepath in files:
                try:
                    result = self._process_file(filepath)
                    if result == "skipped":
                        skip_count += 1
                    elif result == "error":
                        error_count += 1
                    else:
                        success_count += 1
                except Exception as e:
                    error_count += 1
                    logger.error(f"Error processing {filepath}: {e}")

        # Flush tracker state to disk at end of batch
        self.tracker.flush()

        elapsed = time.time() - start_time
        posted = success_count + error_count
        rate = posted / elapsed if elapsed > 0 and posted > 0 else 0
        logger.info(
            f"Batch complete: {len(files)} files "
            f"({success_count} ok, {error_count} err, {skip_count} skip) "
            f"in {elapsed:.1f}s ({rate:.1f} posted/sec)"
        )

        if _histogram_batch_duration:
            _histogram_batch_duration.record(elapsed)
        if batch_span_cm:
            batch_span_cm.__exit__(None, None, None)

        return success_count, error_count, skip_count

    def _wait_for_backpressure(self):
        """Block until queue depth drops below backpressure threshold."""
        threshold = self.config.backpressure_threshold
        if threshold <= 0 or not self.config.backpressure_url:
            return
        url = self.config.backpressure_url
        params = {"list_name": self.config.backpressure_queue}
        poll_interval = self.config.backpressure_poll_interval
        waiting = False
        wait_start = time.time()

        bp_span_cm = _tracer.start_as_current_span(
            "adapter.backpressure_wait",
            attributes={"threshold": threshold, "queue": self.config.backpressure_queue},
        ) if _tracer else None
        if bp_span_cm:
            bp_span_cm.__enter__()

        while self.running:
            try:
                resp = requests.get(url, params=params, timeout=5)
                resp.raise_for_status()
                depth = resp.json().get("depth", 0)
            except Exception as e:
                logger.warning(f"Backpressure check failed: {e}")
                if waiting:
                    logger.info("Backpressure check unreachable, resuming")
                if bp_span_cm:
                    bp_span_cm.__exit__(None, None, None)
                return
            if depth < threshold:
                if waiting:
                    wait_duration = time.time() - wait_start
                    logger.info(f"Backpressure released: depth {depth} < {threshold}, resuming")
                    if _histogram_backpressure_wait:
                        _histogram_backpressure_wait.record(wait_duration)
                if bp_span_cm:
                    bp_span_cm.__exit__(None, None, None)
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
        total_skipped = 0
        total_start = time.time()
        last_directory = None

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

            # Log when entering a new directory
            if directory != last_directory:
                dir_name = Path(directory).name
                parent_name = Path(directory).parent.name
                logger.info(f"--- {parent_name}/{dir_name} ({len(files)} files) ---")
                last_directory = directory

            # Process the batch
            success, errors, skipped = self._process_batch(files)
            total_success += success
            total_errors += errors
            total_skipped += skipped

            # Checkpoint progress
            self.directory_iterator.mark_files_processed(len(files), files[-1] if files else None)

            # Running totals after each batch
            elapsed = time.time() - total_start
            total_posted = total_success + total_errors
            overall_rate = total_posted / elapsed if elapsed > 0 and total_posted > 0 else 0
            stats = self.directory_iterator.get_statistics()

            logger.info(
                f">> Total posted: {total_posted} ({total_success} ok, {total_errors} err) | "
                f"Skipped: {total_skipped} | "
                f"{overall_rate:.1f}/s | "
                f"{elapsed:.0f}s elapsed | "
                f"Dirs: {stats['completed_directories']}/{stats['total_directories']}"
            )

        total_elapsed = time.time() - total_start
        total_posted = total_success + total_errors

        logger.info("=" * 60)
        logger.info("  ALL DONE")
        logger.info(f"  Posted:    {total_posted} ({total_success} ok, {total_errors} err)")
        logger.info(f"  Skipped:   {total_skipped}")
        logger.info(f"  Wall time: {total_elapsed:.0f}s ({total_elapsed/60:.1f} min)")
        if total_posted > 0 and total_elapsed > 0:
            logger.info(f"  Rate:      {total_posted / total_elapsed:.1f} files/s")
        stats = self.directory_iterator.get_statistics()
        logger.info(f"  Dirs:      {stats['completed_directories']}/{stats['total_directories']}")
        logger.info("=" * 60)

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
        success, errors, skipped = self._process_batch(existing_files)

        logger.info(f"Finished processing existing files: {success} ok, {errors} err, {skipped} skipped")

    def start(self):
        """Start the adapter."""
        logger.info("Starting audio file vCon adapter...")
        self.running = True

        if self.config.traverse_mode in ("iterator", "filelist"):
            # Iterator/filelist mode: process directories/lists with checkpointing
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
        # Initialize OpenTelemetry (no-op if deps not installed)
        _init_otel()
        if _otel_available:
            logger.info("OpenTelemetry initialized (exporting to localhost:4318)")
        else:
            logger.info("OpenTelemetry dependencies not installed, running without instrumentation")

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
