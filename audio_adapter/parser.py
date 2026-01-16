"""Filename parser to extract sender and receiver from audio filenames."""

import re
import logging
from typing import Optional, Tuple
from pathlib import Path


logger = logging.getLogger(__name__)


class FilenameParser:
    """Parses filenames to extract sender and receiver information."""

    def __init__(self, pattern: re.Pattern):
        """Initialize parser with regex pattern.

        Args:
            pattern: Compiled regex pattern with at least 2 capture groups
                    for sender and receiver
        """
        self.pattern = pattern

    def parse(self, filepath: str) -> Optional[Tuple[str, str, str]]:
        """Parse filename to extract sender, receiver, and extension.

        Args:
            filepath: Path to the file

        Returns:
            Tuple of (sender, receiver, extension) or None if parsing fails
        """
        filename = Path(filepath).name

        match = self.pattern.match(filename)
        if not match:
            logger.warning(f"Filename does not match pattern: {filename}")
            return None

        groups = match.groups()
        if len(groups) < 2:
            logger.warning(
                f"Pattern did not capture enough groups (need 2+): {filename}"
            )
            return None

        sender = groups[0]
        receiver = groups[1]
        extension = groups[2] if len(groups) > 2 else ""

        logger.debug(
            f"Parsed {filename}: sender={sender}, receiver={receiver}, "
            f"ext={extension}"
        )

        return (sender, receiver, extension)
