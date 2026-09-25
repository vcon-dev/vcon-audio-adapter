"""HTTP poster to send vCons to conserver endpoint."""

import json
import logging
import requests
from typing import Dict, List, Optional
from vcon import Vcon

from audio_adapter.vcon_builder import finalize_vcon


logger = logging.getLogger(__name__)


class HttpPoster:
    """Posts vCons to HTTP conserver endpoint."""

    def __init__(self, url: str, headers: Dict[str, str], ingress_lists: Optional[List[str]] = None):
        """Initialize HTTP poster.

        Args:
            url: Conserver endpoint URL
            headers: HTTP headers to include in requests
            ingress_lists: Optional list of ingress queue names to route vCons to
        """
        self.url = url
        self.headers = headers
        self.ingress_lists = ingress_lists or []

    def post(self, vcon: Vcon) -> bool:
        """Post vCon to conserver endpoint.

        Args:
            vcon: Vcon object to post

        Returns:
            True if post was successful, False otherwise
        """
        try:
            # Build URL with ingress_lists query parameter if configured
            url = self.url
            params = {}
            if self.ingress_lists:
                # The /vcon endpoint accepts ingress_lists as repeated query params
                params['ingress_lists'] = self.ingress_lists
                logger.info(
                    f"Posting vCon {vcon.uuid} to {url} "
                    f"with ingress_lists: {', '.join(self.ingress_lists)}"
                )
            else:
                logger.info(f"Posting vCon {vcon.uuid} to {url}")

            # Convert vCon to dict, strip vcon-lib's empty meta/metadata
            # placeholders (finalize_vcon(), see vcon_builder.py), and
            # ensure 'vcon' version field is present.
            # Use 0.3.0 for compatibility with vcon-mcp REST API
            #
            # NOTE (CON-1086): this downgrades every outbound vCon from the
            # spec-target 0.4.0 that VconBuilder.build() produces to 0.3.0.
            # Out of scope for this card (not in its findings list) but
            # worth flagging: the delivery path undoes the syntax-version
            # work above it. See the CON-1086 report.
            vcon_dict = finalize_vcon(json.loads(vcon.to_json()))
            if 'vcon' not in vcon_dict or vcon_dict['vcon'] == '0.0.1':
                vcon_dict['vcon'] = '0.3.0'
            vcon_json = json.dumps(vcon_dict)

            # POST to endpoint
            response = requests.post(
                url,
                params=params,
                data=vcon_json,
                headers=self.headers,
                timeout=30
            )

            # Check if response indicates success
            if response.status_code >= 200 and response.status_code < 300:
                logger.info(
                    f"Successfully posted vCon {vcon.uuid} "
                    f"(status: {response.status_code})"
                )
                return True
            else:
                logger.error(
                    f"Failed to post vCon {vcon.uuid} "
                    f"(status: {response.status_code}, response: {response.text[:200]})"
                )
                return False

        except Exception as e:
            logger.error(f"Error posting vCon {vcon.uuid} to {self.url}: {e}")
            return False
