"""Configuration management for audio adapter using .env file."""

import os
import re
from datetime import datetime
from typing import Dict, List, Optional
from dotenv import load_dotenv


class Config:
    """Manages configuration from environment variables."""

    def __init__(self, env_file: Optional[str] = None):
        """Load configuration from .env file."""
        if env_file:
            load_dotenv(env_file)
        else:
            load_dotenv()

        # Source type selection
        self.source_type = os.getenv("SOURCE_TYPE", "filesystem").lower()
        if self.source_type not in ("filesystem", "s3"):
            raise ValueError(f"SOURCE_TYPE must be 'filesystem' or 's3', got: {self.source_type}")

        # Filesystem settings (required when SOURCE_TYPE=filesystem)
        self.watch_directory = os.getenv("WATCH_DIRECTORY")
        if self.source_type == "filesystem" and not self.watch_directory:
            raise ValueError("WATCH_DIRECTORY is required when SOURCE_TYPE=filesystem")

        # S3 settings (required when SOURCE_TYPE=s3)
        self.s3_bucket_name = os.getenv("S3_BUCKET_NAME")
        if self.source_type == "s3" and not self.s3_bucket_name:
            raise ValueError("S3_BUCKET_NAME is required when SOURCE_TYPE=s3")

        self.s3_prefix = os.getenv("S3_PREFIX", "")
        self.s3_region = os.getenv("S3_REGION")

        # AWS credentials (optional, will use boto3 default chain if not provided)
        self.aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
        self.aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        self.aws_session_token = os.getenv("AWS_SESSION_TOKEN")

        # S3 date filtering
        self.s3_date_filter = os.getenv("S3_DATE_FILTER")
        self.s3_date_range_start = os.getenv("S3_DATE_RANGE_START")
        self.s3_date_range_end = os.getenv("S3_DATE_RANGE_END")

        # Validate date formats if provided
        self._validate_date_filters()

        # S3-specific behavior
        self.s3_poll_interval = float(os.getenv("S3_POLL_INTERVAL", "30.0"))
        s3_delete_str = os.getenv("S3_DELETE_AFTER_SEND", "false").lower()
        self.s3_delete_after_send = s3_delete_str in ("true", "1", "yes")

        # Conserver settings (required)
        self.conserver_url = os.getenv("CONSERVER_URL")
        if not self.conserver_url:
            raise ValueError("CONSERVER_URL environment variable is required")

        # Optional settings with defaults
        self.conserver_api_token = os.getenv("CONSERVER_API_TOKEN")
        self.conserver_header_name = os.getenv(
            "CONSERVER_HEADER_NAME",
            "x-conserver-api-token"
        )

        # Filename pattern - configurable regex
        # Default pattern: sender_receiver.extension (e.g., 15085551212_19995551234.wav)
        default_pattern = r"(\d+)_(\d+)\.(wav|mp3|ogg|m4a|flac|aac|wma|aiff|opus)"
        self.filename_pattern = os.getenv("FILENAME_PATTERN", default_pattern)

        # Supported audio formats
        supported_formats_str = os.getenv(
            "SUPPORTED_FORMATS",
            "wav,mp3,ogg,m4a,flac,aac,wma,aiff,opus"
        )
        self.supported_formats = [
            ext.strip().lower()
            for ext in supported_formats_str.split(",")
        ]

        # File deletion
        delete_after_send_str = os.getenv("DELETE_AFTER_SEND", "false").lower()
        self.delete_after_send = delete_after_send_str in ("true", "1", "yes")

        # State tracking
        self.state_file = os.getenv("STATE_FILE", ".audio_adapter_state.json")

        # Polling interval
        self.poll_interval = float(os.getenv("POLL_INTERVAL", "1.0"))

        # Process existing files
        process_existing_str = os.getenv("PROCESS_EXISTING", "true").lower()
        self.process_existing = process_existing_str in ("true", "1", "yes")

        # Ingress lists for vCon routing
        ingress_lists_str = os.getenv("INGRESS_LISTS", "")
        self.ingress_lists = [
            item.strip()
            for item in ingress_lists_str.split(",")
            if item.strip()
        ]

        # Audio-specific settings
        # Dialog type: "recording" (default) or "audio"
        self.dialog_type = os.getenv("DIALOG_TYPE", "recording")
        if self.dialog_type not in ("recording", "audio"):
            raise ValueError(f"DIALOG_TYPE must be 'recording' or 'audio', got: {self.dialog_type}")

        # Whether to extract audio duration using mutagen/ffprobe
        extract_duration_str = os.getenv("EXTRACT_DURATION", "true").lower()
        self.extract_duration = extract_duration_str in ("true", "1", "yes")

    def get_headers(self) -> Dict[str, str]:
        """Get HTTP headers for conserver requests."""
        headers = {"Content-Type": "application/json"}
        if self.conserver_api_token:
            headers[self.conserver_header_name] = self.conserver_api_token
        return headers

    def get_filename_regex(self) -> re.Pattern:
        """Get compiled regex pattern for filename parsing."""
        return re.compile(self.filename_pattern, re.IGNORECASE)

    def get_aws_credentials(self) -> Optional[Dict[str, str]]:
        """Get AWS credentials if provided, otherwise None (uses boto3 default chain)."""
        if self.aws_access_key_id and self.aws_secret_access_key:
            creds = {
                "aws_access_key_id": self.aws_access_key_id,
                "aws_secret_access_key": self.aws_secret_access_key,
            }
            if self.aws_session_token:
                creds["aws_session_token"] = self.aws_session_token
            return creds
        return None

    def _validate_date_filters(self):
        """Validate date filter formats."""
        date_formats = ["%Y/%m/%d", "%Y-%m-%d", "%Y%m%d"]

        if self.s3_date_filter:
            self._validate_date_string(self.s3_date_filter, "S3_DATE_FILTER", date_formats)

        if self.s3_date_range_start:
            self._validate_date_string(
                self.s3_date_range_start, "S3_DATE_RANGE_START", date_formats
            )

        if self.s3_date_range_end:
            self._validate_date_string(
                self.s3_date_range_end, "S3_DATE_RANGE_END", date_formats
            )

        # Validate that start is before end if both provided
        if self.s3_date_range_start and self.s3_date_range_end:
            start = self._parse_date_string(self.s3_date_range_start, date_formats)
            end = self._parse_date_string(self.s3_date_range_end, date_formats)
            if start > end:
                raise ValueError(
                    "S3_DATE_RANGE_START must be before or equal to S3_DATE_RANGE_END"
                )

    def _validate_date_string(self, date_str: str, var_name: str, formats: List[str]):
        """Validate that a date string matches one of the expected formats."""
        if not self._parse_date_string(date_str, formats):
            raise ValueError(
                f"{var_name} must be in format YYYY/MM/DD, YYYY-MM-DD, or YYYYMMDD"
            )

    def _parse_date_string(self, date_str: str, formats: List[str]) -> Optional[datetime]:
        """Parse date string using multiple formats."""
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return None
