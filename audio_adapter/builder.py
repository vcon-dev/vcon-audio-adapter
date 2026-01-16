"""vCon builder to create vCons from audio files."""

import base64
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

    def __init__(self, dialog_type: str = "recording", extract_duration: bool = True):
        """Initialize builder.

        Args:
            dialog_type: Type of dialog to create ("recording" or "audio")
            extract_duration: Whether to extract audio duration
        """
        self.dialog_type = dialog_type
        self.extract_duration = extract_duration

    def build(
        self,
        filepath: str,
        sender: str,
        receiver: str,
        extension: str
    ) -> Optional[Vcon]:
        """Build a vCon from an audio file.

        Args:
            filepath: Path to the audio file
            sender: Sender phone number
            receiver: Receiver phone number
            extension: File extension

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

            # Read audio file
            try:
                with open(path, 'rb') as f:
                    audio_data = f.read()
            except Exception as e:
                logger.error(f"Error reading audio file {filepath}: {e}")
                return None

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

            # Encode audio as base64
            audio_base64 = base64.b64encode(audio_data).decode('utf-8')

            # Get MIME type
            mime_type = MIME_TYPES.get(extension.lower(), "audio/wav")

            # Create dialog for the audio recording
            dialog = Dialog(
                type=self.dialog_type,
                start=creation_time,
                parties=[0, 1],  # Both sender and receiver participate
                originator=0,   # Sender initiated the call
                mimetype=mime_type,
                filename=path.name,
                body=audio_base64,
                encoding="base64",
                duration=duration,
            )

            # Add dialog to vCon
            vcon.add_dialog(dialog)

            # Add metadata tags
            vcon.add_tag("source", "audio_adapter")
            vcon.add_tag("original_filename", path.name)
            vcon.add_tag("file_size", str(file_size))
            vcon.add_tag("sender", sender)
            vcon.add_tag("receiver", receiver)
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
