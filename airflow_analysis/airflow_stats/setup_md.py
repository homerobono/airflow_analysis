"""Parse per-config ``setup.md`` files.

Each ``Config X/setup.md`` may contain free-form notes followed by an optional
fenced ``yaml`` block describing the fan layout. The yaml block is what lets us
map generic LHM headers (``System Fan #N``) onto physical slots (``FRONT_2``,
``TOP_1``, ...). When the block is missing the loader warns and per-slot
attribution is skipped, but every other analysis still runs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_logger = logging.getLogger(__name__)

# Match a fenced ```yaml ... ``` block (case-insensitive language tag).
_YAML_BLOCK_RE = re.compile(
    r"```ya?ml\s*\n(?P<body>.*?)```",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class ConfigSetup:
    """Parsed contents of one ``Config X/setup.md`` file."""

    name: str
    path: Path
    notes: str = ""
    fan_map: dict[str, str] = field(default_factory=dict)
    fans: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw_yaml: dict[str, Any] = field(default_factory=dict)

    @property
    def has_fan_map(self) -> bool:
        return bool(self.fan_map)


def parse_setup_md(path: Path, config_name: str) -> ConfigSetup:
    """Parse a single ``setup.md`` file.

    The free-form text (with any yaml block stripped) is preserved as ``notes``;
    the optional yaml block populates ``fan_map`` and ``fans``.
    """
    text = path.read_text(encoding="utf-8", errors="replace")

    yaml_block = ""
    match = _YAML_BLOCK_RE.search(text)
    if match:
        yaml_block = match.group("body")
        notes = (text[: match.start()] + text[match.end() :]).strip()
    else:
        notes = text.strip()

    fan_map: dict[str, str] = {}
    fans: dict[str, dict[str, Any]] = {}
    raw: dict[str, Any] = {}

    if yaml_block:
        try:
            loaded = yaml.safe_load(yaml_block) or {}
            if isinstance(loaded, dict):
                raw = loaded
                fan_map = {str(k): str(v) for k, v in (loaded.get("fan_map") or {}).items()}
                fans_raw = loaded.get("fans") or {}
                if isinstance(fans_raw, dict):
                    for slot, meta in fans_raw.items():
                        if isinstance(meta, dict):
                            fans[str(slot)] = dict(meta)
                        else:
                            fans[str(slot)] = {"note": str(meta)}
        except yaml.YAMLError as exc:
            _logger.warning("Bad YAML block in %s: %s", path, exc)

    if not fan_map:
        _logger.warning(
            "%s: no fan_map yaml block found; per-slot fan attribution will be skipped.",
            path,
        )

    return ConfigSetup(
        name=config_name,
        path=path,
        notes=notes,
        fan_map=fan_map,
        fans=fans,
        raw_yaml=raw,
    )
