"""Filename parser to extract parties from audio filenames.

Filename format: {trunk}_{originating}_{destination}_{date}_{time}.{ext}
Example: 10508_18445876385_993313013855570_2026-01-19_06:08:16.wav
  - trunk: 10508 (trunk/gateway identifier)
  - originating: 18445876385 (caller number)
  - destination: 993313013855570 (callee number)
"""

import re
import logging
from typing import Optional, Tuple
from pathlib import Path


logger = logging.getLogger(__name__)


class FilenameParser:
    """Parses filenames to extract party information."""

    def __init__(self, pattern: re.Pattern):
        """Initialize parser with regex pattern.

        Args:
            pattern: Compiled regex pattern with capture groups for:
                    - trunk (group 1)
                    - originating/sender (group 2)
                    - destination/receiver (group 3)
                    - extension (group 4)
        """
        self.pattern = pattern

    def parse(self, filepath: str) -> Optional[Tuple[str, str, str, str]]:
        """Parse filename to extract trunk, sender, receiver, and extension.

        Args:
            filepath: Path to the file

        Returns:
            Tuple of (trunk, sender, receiver, extension) or None if parsing fails
        """
        filename = Path(filepath).name

        match = self.pattern.match(filename)
        if not match:
            logger.warning(f"Filename does not match pattern: {filename}")
            return None

        groups = match.groups()
        if len(groups) < 3:
            logger.warning(
                f"Pattern did not capture enough groups (need 3+): {filename}"
            )
            return None

        trunk = groups[0]
        sender = groups[1]      # originating number (caller)
        receiver = groups[2]    # destination number (callee)
        extension = groups[3] if len(groups) > 3 else ""

        logger.debug(
            f"Parsed {filename}: trunk={trunk}, sender={sender}, "
            f"receiver={receiver}, ext={extension}"
        )

        return (trunk, sender, receiver, extension)
