"""vCon builder to create vCons from audio files.

Tags: vcon-lib's `Vcon.add_tag()` writes a `purpose: "tags"` attachment
with an ARRAY `body` under `encoding: "json"` (and may omit `party`/
`dialog`/`start`), which violates the core spec's requirement that every
attachment `body` be a string. A vcon-lib fix is tracked separately; until
it lands, this module builds the tags attachment directly via
`vcon_builder.add_tags()`, which emits the same `["key:value", ...]` list
as a JSON-encoded STRING body, with `party`, `dialog`, and `start` set.
"""

import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from vcon import Vcon
from vcon.party import Party
from vcon.dialog import Dialog

from audio_adapter.vcon_builder import (
    VCON_SYNTAX,
    LawfulBasisConfig,
    add_lawful_basis,
    add_tags,
    sha512_b64url,
)


logger = logging.getLogger(__name__)

# MIME type mapping for audio formats
MIME_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "m4a": "audio/x-m4a",
    "flac": "audio/flac",
    "aac": "audio/aac",
    "wma": "audio/x-ms-wma",
    "aiff": "audio/aiff",
    "opus": "audio/opus",
    "webm": "audio/webm",
}


def get_audio_duration(filepath: str) -> Optional[float]:
    """Get duration of audio file in seconds.

    Tries mutagen first, falls back to None if not available.

    Args:
        filepath: Path to the audio file

    Returns:
        Duration in seconds or None if unable to determine
    """
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(filepath)
        if audio is not None and audio.info is not None:
            return audio.info.length
    except ImportError:
        logger.debug("mutagen not installed, skipping duration extraction")
    except Exception as e:
        logger.debug(f"Could not get audio duration with mutagen: {e}")

    # Try ffprobe as fallback
    try:
        import subprocess
        import json
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", filepath
            ],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            duration = data.get("format", {}).get("duration")
            if duration:
                return float(duration)
    except FileNotFoundError:
        logger.debug("ffprobe not available")
    except Exception as e:
        logger.debug(f"Could not get audio duration with ffprobe: {e}")

    return None


class VconBuilder:
    """Builds vCon objects from audio files."""

    def __init__(
        self,
        dialog_type: str = "recording",
        extract_duration: bool = True,
        url_base: Optional[str] = None,
        url_base_path: Optional[str] = None,
        lawful_basis_cfg: Optional[LawfulBasisConfig] = None,
    ):
        """Initialize builder.

        Args:
            dialog_type: Type of dialog to create ("recording" or "audio")
            extract_duration: Whether to extract audio duration
            url_base: Optional HTTP(S) URL prefix. When set, the dialog URL
                becomes "{url_base}/<relative-path>" instead of file://.
            url_base_path: Filesystem directory the relative path is computed
                against (e.g. WATCH_DIRECTORY). Required when url_base is set
                for files outside the directory we fall back to the filename.
            lawful_basis_cfg: Lawful-basis configuration (see
                `LawfulBasisConfig`). Defaults to `LawfulBasisConfig.from_env()`
                (this adapter has no YAML config block, so there is nothing to
                merge via `resolve()`). Leaving `LAWFUL_BASIS*` unset means no
                lawful-basis attachment is added; a warning is logged once.
        """
        self.dialog_type = dialog_type
        self.extract_duration = extract_duration
        self.url_base = url_base or None
        self.url_base_path = Path(url_base_path).resolve() if url_base_path else None
        self.lawful_basis_cfg = lawful_basis_cfg or LawfulBasisConfig.from_env()

    def build(
        self,
        filepath: str,
        sender: str,
        receiver: str,
        extension: str,
        trunk: Optional[str] = None
    ) -> Optional[Vcon]:
        """Build a vCon from an audio file.

        Args:
            filepath: Path to the audio file
            sender: Sender/originating phone number (caller)
            receiver: Receiver/destination phone number (callee)
            extension: File extension
            trunk: Optional trunk/gateway identifier

        Returns:
            Vcon object or None if building fails
        """
        try:
            path = Path(filepath)

            if not path.exists():
                logger.error(f"File does not exist: {filepath}")
                return None

            # Get file metadata
            file_stat = path.stat()
            creation_time = datetime.fromtimestamp(
                file_stat.st_mtime,
                tz=timezone.utc
            )
            file_size = file_stat.st_size

            # Get audio duration if configured
            duration = None
            if self.extract_duration:
                duration = get_audio_duration(filepath)
                if duration:
                    logger.debug(f"Audio duration: {duration:.2f} seconds")

            # Create vCon
            vcon = Vcon.build_new()
            vcon.vcon_dict["vcon"] = VCON_SYNTAX

            # created_at as UTC ISO 8601 (creation_time is tz-aware UTC, so
            # isoformat() already carries a "+00:00" offset). vcon-lib's
            # `created_at` is a read-only property as of 0.9.4 (no fset), so
            # `vcon.created_at = ...` raises AttributeError and previously
            # left the build_new()-assigned timestamp in place silently.
            # Write the dict field directly instead -- this works regardless
            # of whether a given vcon-lib release happens to expose a
            # setter.
            created_at = creation_time.isoformat()
            vcon.vcon_dict["created_at"] = created_at

            # Add parties
            sender_party = Party(tel=sender)
            receiver_party = Party(tel=receiver)
            vcon.add_party(sender_party)
            vcon.add_party(receiver_party)

            # Get MIME type
            mime_type = MIME_TYPES.get(extension.lower(), "audio/wav")

            # Build dialog URL. Default is the legacy file:// reference; when
            # url_base is configured, emit an HTTP(S) URL pointing at a static
            # file server that exposes the audio directory.
            file_url = self._build_dialog_url(path)

            # The core schema requires `content_hash` on any dialog that
            # carries `url` (external media). Compute it from the file's own
            # bytes so it verifies against whatever `file_url` points at.
            content_hash = sha512_b64url(path.read_bytes())

            # Create dialog for the audio recording using URL reference
            # instead of embedding the audio data
            dialog = Dialog(
                type=self.dialog_type,
                start=creation_time,
                parties=[0, 1],  # Both sender and receiver participate
                originator=0,   # Sender initiated the call
                mediatype=mime_type,
                filename=path.name,
                url=file_url,
                content_hash=content_hash,
                duration=duration,
            )

            # Add dialog to vCon
            vcon.add_dialog(dialog)

            # Add metadata tags. Built directly via add_tags() rather than
            # vcon-lib's Vcon.add_tag() -- see the module docstring above.
            tags = [
                ("source", "audio_adapter"),
                ("original_filename", path.name),
                ("file_size", str(file_size)),
            ]
            if trunk:
                tags.append(("trunk", trunk))
            tags.append(("originating", sender))
            tags.append(("destination", receiver))
            if duration:
                tags.append(("duration_seconds", f"{duration:.2f}"))
            add_tags(vcon, tags, start=created_at, party=0, dialog=0)

            # Lawful basis (draft-howe-vcon-lawful-basis). granted_at is the
            # vCon's own created_at unless the adapter operator configures an
            # explicit LAWFUL_BASIS_* override elsewhere -- there is no
            # separate "granted at" signal available at ingest time (the
            # adapter only sees a file that already exists on disk), so the
            # file's creation timestamp is the honest choice: it is the
            # earliest moment this adapter can attest to. Unset LAWFUL_BASIS
            # means no attachment is added (see add_lawful_basis()).
            add_lawful_basis(vcon, self.lawful_basis_cfg, granted_at=created_at, party=0, dialog=0)

            logger.info(
                f"Created vCon {vcon.uuid} from {filepath} "
                f"(sender: {sender}, receiver: {receiver})"
            )

            return vcon

        except Exception as e:
            logger.error(f"Error building vCon from {filepath}: {e}")
            return None

    def _build_dialog_url(self, path: Path) -> str:
        """Compose the dialog `url` field for an audio file."""
        if not self.url_base:
            return f"file://{path.absolute()}"

        if self.url_base_path:
            try:
                relative = path.resolve().relative_to(self.url_base_path)
            except ValueError:
                logger.warning(
                    f"File {path} is outside url_base_path {self.url_base_path}; "
                    f"falling back to filename only"
                )
                relative = Path(path.name)
        else:
            relative = Path(path.name)

        return f"{self.url_base.rstrip('/')}/{relative.as_posix()}"
