"""Tests for filename parser.

The parser expects regex patterns with 4 capture groups:
    (trunk)_(originating/sender)_(destination/receiver).(extension)
matching the filename format documented in audio_adapter/parser.py.
"""

import re
import pytest
from audio_adapter.parser import FilenameParser


class TestFilenameParser:
    """Test cases for FilenameParser."""

    def test_parse_standard_filename(self):
        """Test parsing standard trunk_sender_receiver.extension filename."""
        pattern = re.compile(r"(\d+)_(\d+)_(\d+)\.(wav|mp3|ogg)")
        parser = FilenameParser(pattern)

        result = parser.parse("/path/to/10508_15085551212_19995551234.wav")

        assert result is not None
        trunk, sender, receiver, extension = result
        assert trunk == "10508"
        assert sender == "15085551212"
        assert receiver == "19995551234"
        assert extension == "wav"

    def test_parse_mp3_file(self):
        """Test parsing MP3 filename."""
        pattern = re.compile(r"(\d+)_(\d+)_(\d+)\.(wav|mp3|ogg)")
        parser = FilenameParser(pattern)

        result = parser.parse("99001_18005551234_12125551234.mp3")

        assert result is not None
        trunk, sender, receiver, extension = result
        assert trunk == "99001"
        assert sender == "18005551234"
        assert receiver == "12125551234"
        assert extension == "mp3"

    def test_parse_invalid_filename(self):
        """Test parsing filename that doesn't match pattern."""
        pattern = re.compile(r"(\d+)_(\d+)_(\d+)\.(wav|mp3|ogg)")
        parser = FilenameParser(pattern)

        result = parser.parse("invalid_filename.txt")

        assert result is None

    def test_parse_custom_pattern(self):
        """Test parsing with custom date prefix pattern."""
        pattern = re.compile(r"\d{8}_(\d+)_(\d+)_(\d+)\.(wav|mp3)")
        parser = FilenameParser(pattern)

        result = parser.parse("20240115_10508_15085551212_19995551234.wav")

        assert result is not None
        trunk, sender, receiver, extension = result
        assert trunk == "10508"
        assert sender == "15085551212"
        assert receiver == "19995551234"
        assert extension == "wav"

    def test_parse_extracts_filename_from_path(self):
        """Test that parser extracts filename from full path."""
        pattern = re.compile(r"(\d+)_(\d+)_(\d+)\.(wav)")
        parser = FilenameParser(pattern)

        result = parser.parse("/var/audio/recordings/2024/01/10508_15085551212_19995551234.wav")

        assert result is not None
        trunk, sender, _receiver, _extension = result
        assert trunk == "10508"
        assert sender == "15085551212"

    def test_parse_case_insensitive(self):
        """Test that pattern matching works with different cases."""
        pattern = re.compile(r"(\d+)_(\d+)_(\d+)\.(wav|WAV)", re.IGNORECASE)
        parser = FilenameParser(pattern)

        result = parser.parse("10508_15085551212_19995551234.WAV")

        assert result is not None
        _trunk, _sender, _receiver, extension = result
        assert extension.upper() == "WAV"
