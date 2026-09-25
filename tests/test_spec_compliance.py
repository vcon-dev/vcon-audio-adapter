"""Validate a vCon built by `VconBuilder.build()` (with a lawful-basis
attachment configured via `LAWFUL_BASIS`) against the vendored official
JSON schema, plus the non-negotiables the schema alone doesn't fully
enforce.

`assert_spec_compliant()` is copied verbatim from the adapter template's
`tests/test_spec_compliance.py` (`vcon-dev/vcon-adapter-template`,
pull request #1) -- it only depends on `jsonschema`
(stdlib `json`/`pathlib` aside) and the vendored schema file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from audio_adapter.builder import VconBuilder
from audio_adapter.vcon_builder import LawfulBasisConfig, finalize_vcon

SCHEMA_PATH = Path(__file__).parent / "schema" / "vcon_json_schema.json"


def _walk(node: Any) -> Any:
    """Yield every dict found anywhere in a nested JSON-like structure."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def assert_spec_compliant(vcon_dict: dict[str, Any], schema_path: Path | str) -> None:
    """Assert `vcon_dict` conforms to the vendored vCon core JSON schema and
    to the project's non-negotiable field-naming rules.

    Raises `AssertionError` with the collected schema errors (if any), or on
    the first non-negotiable violation.
    """
    schema = json.loads(Path(schema_path).read_text())
    validator = jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = sorted(validator.iter_errors(vcon_dict), key=lambda e: list(e.path))
    assert not errors, "schema violations:\n" + "\n".join(
        f"  {list(e.path)}: {e.message}" for e in errors
    )

    # `mediatype`, never `mimetype`, anywhere in the document.
    for node in _walk(vcon_dict):
        assert "mimetype" not in node, f"found legacy `mimetype` key in: {node}"

    # Every attachment carries purpose/start/party/dialog, and a string body.
    for att in vcon_dict.get("attachments", []):
        assert "purpose" in att, f"attachment missing `purpose`: {att}"
        assert "type" not in att, f"attachment uses legacy `type` instead of `purpose`: {att}"
        assert "start" in att, f"attachment missing `start`: {att}"
        assert "party" in att, f"attachment missing `party`: {att}"
        assert "dialog" in att, f"attachment missing `dialog`: {att}"
        if "body" in att:
            assert isinstance(att["body"], str), f"attachment `body` is not a string: {att}"

    # Every analysis body is a string too.
    for analysis in vcon_dict.get("analysis", []):
        if "body" in analysis:
            assert isinstance(analysis["body"], str), (
                f"analysis `body` is not a string: {analysis}"
            )
        assert "schema_version" not in analysis, f"legacy `schema_version` in: {analysis}"
        assert "vendor" in analysis, f"analysis missing required `vendor`: {analysis}"

    # No empty `meta`/`metadata`/`group`/`redacted` anywhere in the document.
    for node in _walk(vcon_dict):
        for key in ("meta", "metadata", "group", "redacted"):
            if key in node:
                assert node[key] not in ({}, [], None), f"empty `{key}` present in: {node}"


@pytest.fixture
def wav_bytes() -> bytes:
    """Minimal RIFF header so mutagen / ffprobe don't error during build()."""
    return (
        b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
        b"\x40\x1f\x00\x00\x40\x1f\x00\x00\x01\x00\x08\x00data\x00\x00\x00\x00"
    )


@pytest.fixture
def sample_vcon_dict(tmp_path: Path, wav_bytes: bytes) -> dict[str, Any]:
    """A representative vCon built through this adapter's own `VconBuilder`,
    from a fixture filename, with a lawful-basis attachment, for the
    compliance check below.
    """
    audio = tmp_path / "15085551212_19995551234.wav"
    audio.write_bytes(wav_bytes)

    cfg = LawfulBasisConfig(
        lawful_basis="consent",
        purposes=("recording", "transcription"),
        jurisdiction="US-MA",
        expiration="2027-01-02T12:00:00Z",
        proof_mechanism="audio_recording",
        proof_description="Verbal consent captured at start of recording",
    )
    builder = VconBuilder(extract_duration=False, lawful_basis_cfg=cfg)

    vcon = builder.build(
        filepath=str(audio),
        sender="15085551212",
        receiver="19995551234",
        extension="wav",
        trunk="trunk1",
    )
    assert vcon is not None

    # Same path a real adapter's delivery gets: HttpPoster.post() calls
    # finalize_vcon() before serializing. No test-side stripping here -- if
    # finalize_vcon() regresses or stops being called, this fixture (and
    # test_sample_vcon_with_lawful_basis_is_spec_compliant) fails, because
    # vcon-lib's Dialog.to_dict() leaves empty `meta`/`metadata`
    # placeholders on the dialog added above.
    return finalize_vcon(json.loads(vcon.to_json()))


def test_schema_file_present_and_parses() -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    assert schema.get("title") or schema.get("$id"), "schema file looks empty/malformed"


def test_sample_vcon_with_lawful_basis_is_spec_compliant(
    sample_vcon_dict: dict[str, Any],
) -> None:
    assert_spec_compliant(sample_vcon_dict, SCHEMA_PATH)


def test_sample_vcon_has_lawful_basis_attachment(sample_vcon_dict: dict[str, Any]) -> None:
    lawful_basis_atts = [
        att for att in sample_vcon_dict["attachments"] if att["purpose"] == "lawful_basis"
    ]
    assert len(lawful_basis_atts) == 1
    body = json.loads(lawful_basis_atts[0]["body"])
    assert body["lawful_basis"] == "consent"
    assert "lawful_basis" in sample_vcon_dict.get("extensions", [])


_FIXTURE_UUID = "01a0da6b-7289-8143-9dd8-dd37220d739c"


def test_mimetype_key_fails_the_check() -> None:
    bad = {
        "vcon": "0.4.0",
        "uuid": _FIXTURE_UUID,
        "created_at": "2026-01-02T12:00:00+00:00",
    }
    bad["dialog"] = [{"type": "recording", "mimetype": "audio/wav"}]
    with pytest.raises(AssertionError, match="mimetype"):
        assert_spec_compliant(bad, SCHEMA_PATH)


def test_non_string_analysis_body_fails_the_check() -> None:
    bad = {
        "vcon": "0.4.0",
        "uuid": _FIXTURE_UUID,
        "created_at": "2026-01-02T12:00:00+00:00",
    }
    bad["analysis"] = [
        {
            "type": "summary",
            "dialog": 0,
            "vendor": "v",
            "encoding": "none",
            "body": {"not": "a string"},
        }
    ]
    with pytest.raises(AssertionError, match="not a string"):
        assert_spec_compliant(bad, SCHEMA_PATH)
