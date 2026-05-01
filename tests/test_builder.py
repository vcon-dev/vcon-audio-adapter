"""Tests for the VconBuilder dialog URL emission.

Default (url_base unset) must keep the legacy file:// URL exactly as before.
When AUDIO_URL_BASE is set, the dialog URL becomes
"{url_base}/<path-relative-to-url_base_path>".
"""

import json
import pytest
from audio_adapter.builder import VconBuilder


@pytest.fixture
def wav_bytes():
    """Minimal RIFF header so mutagen / ffprobe don't error during build()."""
    return b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x40\x1f\x00\x00\x40\x1f\x00\x00\x01\x00\x08\x00data\x00\x00\x00\x00"


def _dialog_url(vcon) -> str:
    """Pull dialog[0].url out of a built vCon, regardless of vcon version internals."""
    payload = json.loads(vcon.to_json())
    return payload["dialog"][0]["url"]


def _make_audio_file(tmp_path, name, wav_bytes):
    f = tmp_path / name
    f.write_bytes(wav_bytes)
    return f


class TestDialogUrl:
    def test_default_emits_file_url(self, tmp_path, wav_bytes):
        audio = _make_audio_file(tmp_path, "15551234567_15559876543.wav", wav_bytes)
        builder = VconBuilder(extract_duration=False)

        vcon = builder.build(
            filepath=str(audio),
            sender="15551234567",
            receiver="15559876543",
            extension="wav",
        )

        assert vcon is not None
        assert _dialog_url(vcon) == f"file://{audio.absolute()}"

    def test_url_base_emits_http_url(self, tmp_path, wav_bytes):
        audio = _make_audio_file(tmp_path, "15551234567_15559876543.wav", wav_bytes)
        builder = VconBuilder(
            extract_duration=False,
            url_base="http://audio-fileserver.vconic-test.svc.cluster.local",
            url_base_path=str(tmp_path),
        )

        vcon = builder.build(
            filepath=str(audio),
            sender="15551234567",
            receiver="15559876543",
            extension="wav",
        )

        assert vcon is not None
        assert _dialog_url(vcon) == (
            "http://audio-fileserver.vconic-test.svc.cluster.local/"
            "15551234567_15559876543.wav"
        )

    def test_url_base_strips_trailing_slash(self, tmp_path, wav_bytes):
        audio = _make_audio_file(tmp_path, "a_b.wav", wav_bytes)
        builder = VconBuilder(
            extract_duration=False,
            url_base="https://files.example.com/audio/",
            url_base_path=str(tmp_path),
        )

        vcon = builder.build(filepath=str(audio), sender="a", receiver="b", extension="wav")

        assert _dialog_url(vcon) == "https://files.example.com/audio/a_b.wav"

    def test_url_base_preserves_subdirectory(self, tmp_path, wav_bytes):
        nested = tmp_path / "2026" / "05"
        nested.mkdir(parents=True)
        audio = _make_audio_file(nested, "a_b.wav", wav_bytes)
        builder = VconBuilder(
            extract_duration=False,
            url_base="http://files",
            url_base_path=str(tmp_path),
        )

        vcon = builder.build(filepath=str(audio), sender="a", receiver="b", extension="wav")

        assert _dialog_url(vcon) == "http://files/2026/05/a_b.wav"

    def test_url_base_falls_back_to_filename_when_outside_base(
        self, tmp_path, wav_bytes
    ):
        outside_dir = tmp_path / "elsewhere"
        outside_dir.mkdir()
        audio = _make_audio_file(outside_dir, "a_b.wav", wav_bytes)

        watch_dir = tmp_path / "watched"
        watch_dir.mkdir()

        builder = VconBuilder(
            extract_duration=False,
            url_base="http://files",
            url_base_path=str(watch_dir),
        )

        vcon = builder.build(filepath=str(audio), sender="a", receiver="b", extension="wav")

        assert _dialog_url(vcon) == "http://files/a_b.wav"

    def test_empty_url_base_uses_file_url(self, tmp_path, wav_bytes):
        """Empty string from env var must behave the same as unset."""
        audio = _make_audio_file(tmp_path, "a_b.wav", wav_bytes)
        builder = VconBuilder(
            extract_duration=False,
            url_base="",
            url_base_path=str(tmp_path),
        )

        vcon = builder.build(filepath=str(audio), sender="a", receiver="b", extension="wav")

        assert _dialog_url(vcon) == f"file://{audio.absolute()}"
