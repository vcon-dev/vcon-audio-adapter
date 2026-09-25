"""Spec-compliance helpers for vCon construction (syntax 0.4.0,
draft-ietf-vcon-vcon-core-02).

`LawfulBasisConfig`, `add_lawful_basis()`, `finalize_vcon()`, and
`sha512_b64url()` below are copied verbatim (imports/logging only adapted)
from the adapter template's `vcon_builder.py`
(`vcon-dev/vcon-adapter-template`, pull request #1) so this adapter and the template can be
kept in sync. `add_tags()` is new to this adapter, not present in the
template.

Lawful basis (draft-howe-vcon-lawful-basis, extension name "lawful_basis"):
use `LawfulBasisConfig` + `add_lawful_basis()` below. Do NOT use vcon-lib's
`Vcon.add_lawful_basis_attachment()` -- as of vcon-lib 0.9.6 it writes a
non-string `body` (a dict), which violates the core spec requirement that
every attachment `body` be a string.

`finalize_vcon()` strips vcon-lib's empty `meta`/`metadata` placeholders
before delivery. `add_tags()` is a spec-compliant replacement for
vcon-lib's `Vcon.add_tag()`, whose `tags` attachment writes an array
`body` under `encoding: "json"` instead of the string `body` the core
schema requires -- see the module docstring on tags in `builder.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vcon import Vcon

if TYPE_CHECKING:
    from collections.abc import Mapping

log = logging.getLogger(__name__)

VCON_SYNTAX = "0.4.0"

# The six GDPR lawful bases, per draft-howe-vcon-lawful-basis.
VALID_LAWFUL_BASES = frozenset(
    {
        "consent",
        "contract",
        "legal_obligation",
        "vital_interests",
        "public_task",
        "legitimate_interests",
    }
)

# Set once `add_lawful_basis()` has logged its "no lawful basis configured"
# warning, so a long-running adapter emits it only once per process rather
# than once per event.
_warned_no_lawful_basis = False


def sha512_b64url(data: bytes) -> str:
    """Return content_hash formatted as `sha512-<base64url-of-digest>` per spec."""
    digest = hashlib.sha512(data).digest()
    return "sha512-" + urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _walk_dicts(node: Any) -> Any:
    """Yield every dict found anywhere in a nested JSON-like structure
    (the dicts themselves, so callers can mutate them in place).
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_dicts(item)


def finalize_vcon(vcon_dict: dict[str, Any]) -> dict[str, Any]:
    """Strip vcon-lib's empty `meta`/`metadata` placeholders from every
    object in `vcon_dict` (parties, dialog, and anywhere else the library
    defaults them to `{}` instead of omitting the key), in place, and
    return it.

    vcon-lib 0.9.6's `Dialog.__init__` always sets both `self.meta` and
    `self.metadata` to `{}` when neither is passed to `add_dialog()`, and
    its `to_dict()` (like `Party.to_dict()`) only omits attributes that are
    `None` -- so every dialog added via `add_dialog()` serializes with two
    empty-object fields, which the core spec disallows ("no empty
    meta/metadata/group/redacted"). Call this once on the finished vCon
    dict before serializing or delivering it.
    """
    for node in _walk_dicts(vcon_dict):
        for key in ("meta", "metadata"):
            if node.get(key) == {}:
                del node[key]
    return vcon_dict


def add_tags(
    vcon: Vcon,
    tags: list[tuple[str, str]],
    *,
    start: str,
    party: int = 0,
    dialog: int = 0,
) -> None:
    """Attach a spec-compliant `tags` attachment to `vcon`.

    Replaces vcon-lib's `Vcon.add_tag()`, which writes `purpose: "tags"`
    with an ARRAY body under `encoding: "json"` and may omit `party`/
    `dialog`/`start` -- the core schema requires every attachment `body` to
    be a string. This emits the same `["key:value", ...]` list, but as a
    JSON-encoded STRING body (`json.dumps`), with `party`, `dialog`,
    `start`, and `mediatype` set. A vcon-lib fix for `add_tag()` is tracked
    separately; use this helper until that lands.

    `tags` is a list of `(name, value)` pairs, appended in order as
    `"name:value"` strings, matching the shape vcon-lib's `add_tag()` and
    `get_tag()` already use elsewhere -- so any code that reads this
    attachment's body as a list of `"key:value"` strings keeps working.
    """
    if not tags:
        return

    body = [f"{name}:{value}" for name, value in tags]
    vcon.vcon_dict["attachments"].append(
        {
            "purpose": "tags",
            "start": start,
            "party": party,
            "dialog": dialog,
            "mediatype": "application/json",
            "encoding": "json",
            "body": json.dumps(body),
        }
    )


@dataclass(frozen=True)
class LawfulBasisConfig:
    """Configuration for the `lawful_basis` vCon extension.

    Populated either from environment variables (`from_env`), from an
    adapter's YAML `vcon.lawful_basis:` block (`from_yaml`), or a merge of
    both (`resolve`, where environment variables win over YAML per-field).

    Never default `lawful_basis` in code -- an unset value means the adapter
    builds vCons with no lawful-basis attachment, and `add_lawful_basis()`
    logs a warning rather than inventing a basis.
    """

    lawful_basis: str | None
    purposes: tuple[str, ...] = ("recording",)
    jurisdiction: str | None = None
    expiration: str | None = None  # ISO 8601
    proof_mechanism: str | None = None  # mechanism_type, e.g. "external_system"
    proof_description: str | None = None

    def __post_init__(self) -> None:
        if self.lawful_basis is not None and self.lawful_basis not in VALID_LAWFUL_BASES:
            raise ValueError(
                f"invalid lawful_basis {self.lawful_basis!r}; must be one of "
                f"{sorted(VALID_LAWFUL_BASES)} or unset"
            )

    @staticmethod
    def _split_purposes(raw: str) -> tuple[str, ...]:
        return tuple(p.strip() for p in raw.split(",") if p.strip())

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LawfulBasisConfig:
        """Build from env vars: LAWFUL_BASIS, LAWFUL_BASIS_PURPOSE (comma-separated),
        LAWFUL_BASIS_JURISDICTION, LAWFUL_BASIS_EXPIRATION,
        LAWFUL_BASIS_PROOF_MECHANISM, LAWFUL_BASIS_PROOF_DESCRIPTION.
        """
        env = os.environ if env is None else env

        purposes_raw = env.get("LAWFUL_BASIS_PURPOSE")
        purposes = cls._split_purposes(purposes_raw) if purposes_raw else cls.purposes

        return cls(
            lawful_basis=env.get("LAWFUL_BASIS") or None,
            purposes=purposes,
            jurisdiction=env.get("LAWFUL_BASIS_JURISDICTION") or None,
            expiration=env.get("LAWFUL_BASIS_EXPIRATION") or None,
            proof_mechanism=env.get("LAWFUL_BASIS_PROOF_MECHANISM") or None,
            proof_description=env.get("LAWFUL_BASIS_PROOF_DESCRIPTION") or None,
        )

    @classmethod
    def from_yaml(cls, block: Mapping[str, Any] | None) -> LawfulBasisConfig:
        """Build from an adapter's YAML `vcon.lawful_basis:` block, e.g.:

        vcon:
          lawful_basis:
            lawful_basis: consent
            purposes: [recording, transcription]
            jurisdiction: US-MA
            expiration: "2026-01-02T12:00:00Z"
            proof_mechanism: external_system
            proof_description: "Verbal consent captured at start of recording"
        """
        block = block or {}
        purposes_raw = block.get("purposes")
        if isinstance(purposes_raw, str):
            purposes = cls._split_purposes(purposes_raw)
        elif purposes_raw:
            purposes = tuple(purposes_raw)
        else:
            purposes = cls.purposes

        return cls(
            lawful_basis=block.get("lawful_basis") or None,
            purposes=purposes,
            jurisdiction=block.get("jurisdiction") or None,
            expiration=block.get("expiration") or None,
            proof_mechanism=block.get("proof_mechanism") or None,
            proof_description=block.get("proof_description") or None,
        )

    @classmethod
    def resolve(
        cls,
        *,
        yaml_block: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> LawfulBasisConfig:
        """Merge YAML `vcon.lawful_basis` with env vars. Env wins per-field: any
        `LAWFUL_BASIS*` var that is set overrides the corresponding YAML value;
        an unset var falls back to YAML, then to the field default.
        """
        yaml_cfg = cls.from_yaml(yaml_block)
        env = os.environ if env is None else env

        purposes_raw = env.get("LAWFUL_BASIS_PURPOSE")
        purposes = cls._split_purposes(purposes_raw) if purposes_raw else yaml_cfg.purposes

        return cls(
            lawful_basis=env.get("LAWFUL_BASIS") or yaml_cfg.lawful_basis,
            purposes=purposes,
            jurisdiction=env.get("LAWFUL_BASIS_JURISDICTION") or yaml_cfg.jurisdiction,
            expiration=env.get("LAWFUL_BASIS_EXPIRATION") or yaml_cfg.expiration,
            proof_mechanism=env.get("LAWFUL_BASIS_PROOF_MECHANISM") or yaml_cfg.proof_mechanism,
            proof_description=(
                env.get("LAWFUL_BASIS_PROOF_DESCRIPTION") or yaml_cfg.proof_description
            ),
        )


def add_lawful_basis(
    vcon: Vcon,
    cfg: LawfulBasisConfig,
    *,
    granted_at: str,
    party: int = 0,
    dialog: int = 0,
) -> bool:
    """Attach a `lawful_basis` record (draft-howe-vcon-lawful-basis) to `vcon`.

    Returns False and adds nothing if `cfg.lawful_basis` is unset (logging a
    warning once per process). Returns True and appends the attachment
    (plus the `lawful_basis` extension, deduped) otherwise.

    `granted_at` is the ISO 8601 timestamp the basis was established (used
    as the attachment's `start` and each purpose grant's `granted_at`).
    """
    global _warned_no_lawful_basis

    if not cfg.lawful_basis:
        if not _warned_no_lawful_basis:
            log.warning(
                "no lawful_basis configured; building vCons without a "
                "lawful-basis attachment (set LAWFUL_BASIS or the "
                "vcon.lawful_basis config block)"
            )
            _warned_no_lawful_basis = True
        return False

    body: dict[str, Any] = {"lawful_basis": cfg.lawful_basis}
    if cfg.expiration is not None:
        body["expiration"] = cfg.expiration
    body["purpose_grants"] = [
        {"purpose": purpose, "granted": True, "granted_at": granted_at} for purpose in cfg.purposes
    ]
    if cfg.jurisdiction is not None:
        body["jurisdiction"] = cfg.jurisdiction
    if cfg.proof_mechanism is not None:
        proof_mechanism: dict[str, Any] = {"mechanism_type": cfg.proof_mechanism}
        if cfg.proof_description is not None:
            proof_mechanism["description"] = cfg.proof_description
        body["proof_mechanisms"] = [proof_mechanism]

    vcon.vcon_dict["attachments"].append(
        {
            "purpose": "lawful_basis",
            "start": granted_at,
            "party": party,
            "dialog": dialog,
            "encoding": "json",
            # The vendored core schema (tests/schema/vcon_json_schema.json)
            # requires `mediatype` on any attachment with a non-empty `body`.
            "mediatype": "application/json",
            "body": json.dumps(body),
        }
    )

    extensions = vcon.vcon_dict.setdefault("extensions", [])
    if "lawful_basis" not in extensions:
        extensions.append("lawful_basis")

    return True
