"""vCon builder to create vCons from audio files."""

import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from vcon import Vcon
from vcon.party import Party
from vcon.dialog import Dialog


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
        """
        self.dialog_type = dialog_type
        self.extract_duration = extract_duration
        self.url_base = url_base or None
        self.url_base_path = Path(url_base_path).resolve() if url_base_path else None

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

            # Set creation time from file modification time
            try:
                vcon.created_at = creation_time.isoformat()
            except AttributeError:
                # Some vcon versions have created_at as read-only
                logger.debug("Could not set created_at attribute (read-only in this vcon version)")

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

            # Create dialog for the audio recording using URL reference
            # instead of embedding the audio data
            dialog = Dialog(
                type=self.dialog_type,
                start=creation_time,
                parties=[0, 1],  # Both sender and receiver participate
                originator=0,   # Sender initiated the call
                mimetype=mime_type,
                filename=path.name,
                url=file_url,
                duration=duration,
            )

            # Add dialog to vCon
            vcon.add_dialog(dialog)

            # Add metadata tags
            vcon.add_tag("source", "audio_adapter")
            vcon.add_tag("original_filename", path.name)
            vcon.add_tag("file_size", str(file_size))
            if trunk:
                vcon.add_tag("trunk", trunk)
            vcon.add_tag("originating", sender)
            vcon.add_tag("destination", receiver)
            if duration:
                vcon.add_tag("duration_seconds", f"{duration:.2f}")

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
