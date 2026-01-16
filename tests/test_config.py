"""Tests for configuration module."""

import os
import pytest
from audio_adapter.config import Config


class TestConfig:
    """Test cases for Config class."""

    def test_config_requires_conserver_url(self, monkeypatch):
        """Test that CONSERVER_URL is required."""
        monkeypatch.setenv("WATCH_DIRECTORY", "/tmp")
        monkeypatch.delenv("CONSERVER_URL", raising=False)

        with pytest.raises(ValueError, match="CONSERVER_URL"):
            Config()

    def test_config_requires_watch_directory_for_filesystem(self, monkeypatch):
        """Test that WATCH_DIRECTORY is required for filesystem source."""
        monkeypatch.setenv("CONSERVER_URL", "http://localhost:8000/vcon")
        monkeypatch.setenv("SOURCE_TYPE", "filesystem")
        monkeypatch.delenv("WATCH_DIRECTORY", raising=False)

        with pytest.raises(ValueError, match="WATCH_DIRECTORY"):
            Config()

    def test_config_loads_defaults(self, monkeypatch, tmp_path):
        """Test that config loads with proper defaults."""
        monkeypatch.setenv("CONSERVER_URL", "http://localhost:8000/vcon")
        monkeypatch.setenv("WATCH_DIRECTORY", str(tmp_path))

        config = Config()

        assert config.source_type == "filesystem"
        assert config.dialog_type == "recording"
        assert config.extract_duration is True
        assert config.process_existing is True
        assert config.delete_after_send is False
        assert "wav" in config.supported_formats
        assert "mp3" in config.supported_formats

    def test_config_parses_ingress_lists(self, monkeypatch, tmp_path):
        """Test that ingress lists are parsed correctly."""
        monkeypatch.setenv("CONSERVER_URL", "http://localhost:8000/vcon")
        monkeypatch.setenv("WATCH_DIRECTORY", str(tmp_path))
        monkeypatch.setenv("INGRESS_LISTS", "transcribe, analyze, store")

        config = Config()

        assert config.ingress_lists == ["transcribe", "analyze", "store"]

    def test_config_parses_supported_formats(self, monkeypatch, tmp_path):
        """Test that supported formats are parsed correctly."""
        monkeypatch.setenv("CONSERVER_URL", "http://localhost:8000/vcon")
        monkeypatch.setenv("WATCH_DIRECTORY", str(tmp_path))
        monkeypatch.setenv("SUPPORTED_FORMATS", "wav, mp3, ogg")

        config = Config()

        assert config.supported_formats == ["wav", "mp3", "ogg"]

    def test_config_get_headers_with_token(self, monkeypatch, tmp_path):
        """Test that headers include API token when configured."""
        monkeypatch.setenv("CONSERVER_URL", "http://localhost:8000/vcon")
        monkeypatch.setenv("WATCH_DIRECTORY", str(tmp_path))
        monkeypatch.setenv("CONSERVER_API_TOKEN", "test-token-123")

        config = Config()
        headers = config.get_headers()

        assert headers["Content-Type"] == "application/json"
        assert headers["x-conserver-api-token"] == "test-token-123"

    def test_config_get_headers_without_token(self, monkeypatch, tmp_path):
        """Test that headers work without API token."""
        monkeypatch.setenv("CONSERVER_URL", "http://localhost:8000/vcon")
        monkeypatch.setenv("WATCH_DIRECTORY", str(tmp_path))
        monkeypatch.delenv("CONSERVER_API_TOKEN", raising=False)

        config = Config()
        headers = config.get_headers()

        assert headers["Content-Type"] == "application/json"
        assert "x-conserver-api-token" not in headers

    def test_config_invalid_dialog_type(self, monkeypatch, tmp_path):
        """Test that invalid dialog type raises error."""
        monkeypatch.setenv("CONSERVER_URL", "http://localhost:8000/vcon")
        monkeypatch.setenv("WATCH_DIRECTORY", str(tmp_path))
        monkeypatch.setenv("DIALOG_TYPE", "invalid")

        with pytest.raises(ValueError, match="DIALOG_TYPE"):
            Config()

    def test_config_filename_regex(self, monkeypatch, tmp_path):
        """Test that filename regex is compiled correctly."""
        monkeypatch.setenv("CONSERVER_URL", "http://localhost:8000/vcon")
        monkeypatch.setenv("WATCH_DIRECTORY", str(tmp_path))

        config = Config()
        pattern = config.get_filename_regex()

        # Test that default pattern works
        match = pattern.match("15085551212_19995551234.wav")
        assert match is not None
        assert match.group(1) == "15085551212"
        assert match.group(2) == "19995551234"
        assert match.group(3) == "wav"
