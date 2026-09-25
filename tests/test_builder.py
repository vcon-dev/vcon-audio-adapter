"""Tests for the VconBuilder dialog URL emission.

Default (url_base unset) must keep the legacy file:// URL exactly as before.
When AUDIO_URL_BASE is set, the dialog URL becomes
"{url_base}/<path-relative-to-url_base_path>".
"""

import json
from datetime import datetime, timedelta

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


def _vcon_dict(vcon) -> dict:
    return json.loads(vcon.to_json())


class TestSpecCompliance:
    """CON-1086: `vcon` syntax, `mediatype` (not `mimetype`), a spec-compliant
    tags attachment, and a UTC-aware `created_at`.
    """

    def test_vcon_syntax_is_0_4_0(self, tmp_path, wav_bytes):
        audio = _make_audio_file(tmp_path, "15551234567_15559876543.wav", wav_bytes)
        builder = VconBuilder(extract_duration=False)

        vcon = builder.build(
            filepath=str(audio), sender="15551234567", receiver="15559876543", extension="wav"
        )

        assert _vcon_dict(vcon)["vcon"] == "0.4.0"

    def test_created_at_is_utc_aware_iso8601(self, tmp_path, wav_bytes):
        audio = _make_audio_file(tmp_path, "a_b.wav", wav_bytes)
        builder = VconBuilder(extract_duration=False)

        vcon = builder.build(filepath=str(audio), sender="a", receiver="b", extension="wav")

        created_at = _vcon_dict(vcon)["created_at"]
        # datetime.fromisoformat rejects a bare offset-less timestamp; this
        # also fails if created_at were e.g. "...Z" without being parseable,
        # or missing the offset entirely.
        parsed = datetime.fromisoformat(created_at)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)

    def test_dialog_uses_mediatype_not_mimetype(self, tmp_path, wav_bytes):
        audio = _make_audio_file(tmp_path, "a_b.wav", wav_bytes)
        builder = VconBuilder(extract_duration=False)

        vcon = builder.build(filepath=str(audio), sender="a", receiver="b", extension="wav")

        dialog = _vcon_dict(vcon)["dialog"][0]
        assert dialog["mediatype"] == "audio/wav"
        assert "mimetype" not in dialog

    def test_tags_attachment_has_raw_body_and_party_dialog_start(self, tmp_path, wav_bytes):
        audio = _make_audio_file(tmp_path, "15551234567_15559876543.wav", wav_bytes)
        builder = VconBuilder(extract_duration=False)

        vcon = builder.build(
            filepath=str(audio),
            sender="15551234567",
            receiver="15559876543",
            extension="wav",
            trunk="trunk1",
        )

        payload = _vcon_dict(vcon)
        tags_atts = [a for a in payload["attachments"] if a["purpose"] == "tags"]
        assert len(tags_atts) == 1
        att = tags_atts[0]

        assert att["encoding"] == "json"
        assert not isinstance(att["body"], str), (
            "encoding=json body should be the raw list, not a json.dumps() string"
        )
        assert att["mediatype"] == "application/json"
        assert att["party"] == 0
        assert att["dialog"] == 0
        assert "start" in att

        tags = att["body"]
        assert isinstance(tags, list)
        assert "source:audio_adapter" in tags
        assert "original_filename:15551234567_15559876543.wav" in tags
        assert "trunk:trunk1" in tags
        assert "originating:15551234567" in tags
        assert "destination:15559876543" in tags

    def test_no_lawful_basis_by_default(self, tmp_path, wav_bytes, monkeypatch):
        """Unset LAWFUL_BASIS -> no lawful_basis attachment, never a default."""
        monkeypatch.delenv("LAWFUL_BASIS", raising=False)
        audio = _make_audio_file(tmp_path, "a_b.wav", wav_bytes)
        builder = VconBuilder(extract_duration=False)

        vcon = builder.build(filepath=str(audio), sender="a", receiver="b", extension="wav")

        payload = _vcon_dict(vcon)
        assert not [a for a in payload["attachments"] if a["purpose"] == "lawful_basis"]
        assert "lawful_basis" not in payload.get("extensions", [])

    def test_lawful_basis_attachment_when_configured(self, tmp_path, wav_bytes):
        from audio_adapter.vcon_builder import LawfulBasisConfig

        audio = _make_audio_file(tmp_path, "a_b.wav", wav_bytes)
        cfg = LawfulBasisConfig(lawful_basis="consent")
        builder = VconBuilder(extract_duration=False, lawful_basis_cfg=cfg)

        vcon = builder.build(filepath=str(audio), sender="a", receiver="b", extension="wav")

        payload = _vcon_dict(vcon)
        lawful_basis_atts = [a for a in payload["attachments"] if a["purpose"] == "lawful_basis"]
        assert len(lawful_basis_atts) == 1
        assert lawful_basis_atts[0]["start"] == payload["created_at"]
        assert "lawful_basis" in payload["extensions"]
