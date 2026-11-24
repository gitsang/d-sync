"""Parsing and validation of the SD card YAML config.

The config file lives in the SD card root; its default name is ``dsync.yml``
(overridable with ``--config-name``).

Three notations are supported, from simple to verbose:

1) Simplified form::

       source: DCIM
       destination: D:/Photos/DCIM

2) Rule list (recommended)::

       version: 1
       destination_root: D:/SD_Backup
       rules:
         - source: DCIM
           destination: Photos/DCIM
         - source: Documents/*.pdf
           destination: Docs

3) Mapping table::

       destination_root: D:/SD_Backup
       mappings:
         DCIM: Photos/DCIM
         Documents: Docs

Whatever the notation, the result is normalized into a :class:`SyncConfig`
with a ``rules`` list.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_NAMES = ("dsync.yml", "dsync.yaml")
"""Candidate config file names looked up in the SD card root (dsync.yml first)."""


class ConfigError(Exception):
    """The config file is missing or malformed."""


def match_pattern(relative_path: str, pattern: str) -> bool:
    """Return whether a relative path matches an exclude pattern.

    Three matching styles are supported:
      * glob the whole relative path, e.g. ``a/b/c.tmp``
      * match the file name, e.g. ``*.tmp``
      * match any directory level, e.g. ``System Volume Information``
    """
    posix = relative_path.replace("\\", "/")
    pattern = pattern.replace("\\", "/").strip()
    if not pattern:
        return False
    if fnmatch.fnmatch(posix, pattern):
        return True
    name = PurePosixPath(posix).name
    if fnmatch.fnmatch(name, pattern):
        return True
    if any(fnmatch.fnmatch(part, pattern) for part in PurePosixPath(posix).parts):
        return True
    # Support the "**/xxx" form
    if pattern.startswith("**/") and fnmatch.fnmatch(name, pattern[3:]):
        return True
    return False


@dataclass
class SyncRule:
    """One sync rule: maps a source on the SD card to a destination path."""

    source: str
    """Source path relative to the SD card root; may contain ``*`` / ``**`` wildcards."""

    destination: str
    """Destination path. Absolute paths are used as-is, relative ones resolve against destination_root."""

    enabled: bool = True
    exclude: tuple[str, ...] = ()
    """Extra exclude patterns (glob), relative to the source directory."""

    overwrite: bool | None = None
    """Overwrite existing destination files. None inherits the global setting."""

    # ------------------------------------------------------------------
    def matches(self, relative_path: str) -> bool:
        """Return whether a relative path inside the SD card hits this rule's exclude list."""
        for pattern in self.exclude:
            if match_pattern(relative_path, pattern):
                return True
        return False


@dataclass
class SyncConfig:
    """The whole config after normalization."""

    rules: list[SyncRule] = field(default_factory=list)
    destination_root: str = ""
    """Base directory for the relative destinations used by rules."""

    overwrite: bool = False
    """Whether to overwrite an existing destination file (False skips by content dedupe)."""

    create_dirs: bool = True
    """Whether to create destination directories automatically."""

    exclude: tuple[str, ...] = ()
    """Global exclude patterns, applied to every rule."""

    max_workers: int = 4
    """Number of concurrent copy threads."""

    enabled: bool = True
    """Master switch for automatic sync. When False, inserting a device does not sync; trigger it manually."""

    source_path: Path | None = None
    """Path of the config file itself."""

    card_root: Path | None = None
    """SD card root directory that holds the config file."""

    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def resolve_destination(self, rule: SyncRule) -> Path:
        """Resolve the destination path of a rule into an absolute path."""
        dest = Path(rule.destination)
        if dest.is_absolute() or (dest.drive and dest.drive != ""):
            return dest
        if self.destination_root:
            return Path(self.destination_root) / dest
        return dest

    def global_excludes(self) -> tuple[str, ...]:
        return self.exclude

    def active_rules(self) -> list[SyncRule]:
        """Rules that take part in a sync (enabled=True)."""
        return [rule for rule in self.rules if rule.enabled]

    def summary(self) -> dict[str, Any]:
        return {
            "rules": len(self.active_rules()),
            "destination_root": self.destination_root,
            "overwrite": self.overwrite,
            "exclude": list(self.exclude),
            "enable": self.enabled,
            "source": str(self.source_path) if self.source_path else "",
        }


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------
def _first(mapping: dict, keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    # Accept the common localized YAML booleans too, so a hand-written Chinese
    # config still parses.
    if text in ("1", "true", "yes", "y", "on", "是", "开"):
        return True
    if text in ("0", "false", "no", "n", "off", "否", "关"):
        return False
    return default


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(";") if item.strip())
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value),)


def _normalize_source(source: str) -> str:
    """Normalize source into a POSIX-style path relative to the SD card root."""
    text = str(source).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/") if text not in ("/", "") else ""


def _rule_from_mapping(item: dict, index: int, defaults: dict) -> SyncRule:
    source = _first(item, ("source", "src", "from", "input", "path"))
    if source is None:
        raise ConfigError(f"Rule {index + 1} is missing the source field")
    source = str(source)

    destination = _first(item, ("destination", "dest", "to", "target", "output", "dst"))
    if destination is None:
        raise ConfigError(f"Rule {index + 1} (source={source}) is missing the destination field")

    exclude = _as_tuple(_first(item, ("exclude", "ignore", "filter"), ()))
    overwrite = _first(item, ("overwrite", "force"))

    return SyncRule(
        source=_normalize_source(source),
        destination=str(destination).strip(),
        enabled=_as_bool(_first(item, ("enabled", "active"), True), True),
        exclude=exclude,
        overwrite=None if overwrite is None else _as_bool(overwrite),
    )


def parse_config(data: Any, source_path: Path | None = None, card_root: Path | None = None) -> SyncConfig:
    """Normalize already-loaded YAML data into a :class:`SyncConfig`."""
    if data is None:
        raise ConfigError("Config file is empty")
    if not isinstance(data, dict):
        raise ConfigError(f"Config top level must be a mapping (dict), got {type(data).__name__}")

    defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}

    destination_root = str(
        _first(data, ("destination_root", "dest_root", "output_root", "target_root", "base_dir"), "")
        or _first(defaults, ("destination_root", "dest_root", "output_root", "target_root", "base_dir"), "")
        or ""
    ).strip()

    overwrite = _as_bool(
        _first(data, ("overwrite", "force", "replace"), None)
        if _first(data, ("overwrite", "force", "replace"), None) is not None
        else _first(defaults, ("overwrite", "force", "replace"), None),
        False,
    )

    global_exclude = _as_tuple(
        _first(data, ("exclude", "ignore", "filter"), None)
        if _first(data, ("exclude", "ignore", "filter"), None) is not None
        else _first(defaults, ("exclude", "ignore", "filter"), None)
    )

    create_dirs = _as_bool(_first(data, ("create_dirs", "mkdirs", "create_directories"), True), True)
    max_workers = int(_first(data, ("max_workers", "workers", "concurrency"), 4) or 4)

    # Master auto-sync switch: enable / auto_sync
    # Note: the rule-level "enabled" key means something else (whether one rule
    # participates), so that key name is deliberately not shared here.
    enabled = _as_bool(
        _first(data, ("enable", "auto_sync"), None)
        if _first(data, ("enable", "auto_sync"), None) is not None
        else _first(defaults, ("enable", "auto_sync"), None),
        True,
    )

    rules: list[SyncRule] = []

    # --- Form 1: a single rule (source/destination at the top level) ---
    if _first(data, ("source", "src", "from")) is not None:
        merged = dict(data)
        rules.append(_rule_from_mapping(merged, 0, defaults))

    # --- Form 2: a rules / sync / items / tasks list ---
    else:
        list_node = None
        for key in ("rules", "mappings", "sync", "items", "tasks", "jobs", "copies"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                list_node = candidate
                break
            if isinstance(candidate, dict):
                list_node = candidate
                break

        if isinstance(list_node, list):
            for index, item in enumerate(list_node):
                if not isinstance(item, dict):
                    raise ConfigError(f"Rule {index + 1} must be a mapping (dict)")
                rules.append(_rule_from_mapping(item, index, defaults))
        elif isinstance(list_node, dict):
            # --- Form 3: mapping table {source: destination} ---
            for source, destination in list_node.items():
                if isinstance(destination, dict):
                    item = dict(destination)
                    item.setdefault("source", source)
                    rules.append(_rule_from_mapping(item, len(rules), defaults))
                else:
                    rules.append(
                        SyncRule(
                            source=_normalize_source(str(source)),
                            destination=str(destination),
                        )
                    )
        else:
            raise ConfigError(
                "No sync rules found. Use a rules list, a mappings table, "
                "or put source / destination directly at the top level."
            )

    # Keep enabled=False rules (the dashboard shows and toggles them); filter them at sync time

    config = SyncConfig(
        rules=rules,
        destination_root=destination_root,
        overwrite=overwrite,
        create_dirs=create_dirs,
        exclude=global_exclude,
        max_workers=max(1, min(max_workers, 16)),
        enabled=enabled,
        source_path=source_path,
        card_root=card_root,
        extra={k: v for k, v in data.items() if k not in {
            "rules", "sync", "items", "tasks", "jobs", "copies", "mappings",
            "source", "src", "from", "destination", "dest", "to", "target",
            "output", "dst", "destination_root", "dest_root", "output_root",
            "target_root", "base_dir", "overwrite", "force", "replace",
            "exclude", "ignore", "filter", "defaults", "create_dirs", "mkdirs",
            "create_directories", "max_workers", "workers", "concurrency", "version",
            "enable", "auto_sync",
        }},
    )
    return config


def load_config_file(path: Path, card_root: Path | None = None) -> SyncConfig:
    """Read and parse a config file from disk."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("PyYAML is not installed; cannot parse the config file") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config file {path.name} has invalid YAML: {exc}") from exc

    return parse_config(data, source_path=path, card_root=card_root)


def find_config_file(card_root: Path, config_names: Iterable[str] = DEFAULT_CONFIG_NAMES) -> Path | None:
    """Look for a config file in the SD card root."""
    for name in config_names:
        candidate = card_root / name
        if candidate.is_file():
            return candidate
    return None


def validate_destination(destination: str, card_root: Path | None = None) -> str | None:
    """Validate a destination path and return an error description (None when valid)."""
    if not destination.strip():
        return "Destination path is empty"
    try:
        target = Path(destination)
    except (TypeError, ValueError):
        return f"Invalid destination path: {destination}"
    if card_root is not None:
        try:
            resolved = target.resolve() if target.is_absolute() else Path(os.path.abspath(target)).resolve()
            card_resolved = card_root.resolve()
            if resolved == card_resolved or card_resolved in resolved.parents:
                return f"Destination path cannot be inside the SD card: {destination}"
        except OSError:
            pass
    return None


# ----------------------------------------------------------------------
# Serialization: editable model <-> YAML text
# ----------------------------------------------------------------------
def config_to_editable(config: SyncConfig) -> dict:
    """Convert a config object into the frontend editable model (plain JSON-serializable data)."""
    return {
        "version": 1,
        "enable": config.enabled,
        "destination_root": config.destination_root,
        "overwrite": config.overwrite,
        "exclude": list(config.exclude),
        "rules": [
            {
                "source": rule.source,
                "destination": rule.destination,
                "exclude": list(rule.exclude),
                "overwrite": rule.overwrite,
                "enabled": rule.enabled,
            }
            for rule in config.rules
        ],
    }


def editable_to_yaml_text(model: dict) -> str:
    """Serialize the frontend editable model into YAML text.

    Only meaningful fields are written so the file stays tidy; a short comment
    header explains how the file is managed.
    """
    import yaml

    lines: list[str] = [
        "# D-Sync sync configuration",
        "# You can edit this file in the D-Sync dashboard; saving applies it immediately.",
        "",
    ]

    document: dict[str, Any] = {"version": 1}

    document["enable"] = bool(model.get("enable", True))

    destination_root = str(model.get("destination_root") or "").strip()
    if destination_root:
        document["destination_root"] = destination_root

    document["overwrite"] = bool(model.get("overwrite", False))

    exclude = _as_tuple(model.get("exclude"))
    if exclude:
        document["exclude"] = list(exclude)

    rules: list[dict[str, Any]] = []
    for item in model.get("rules") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip().replace("\\", "/")
        destination = str(item.get("destination") or "").strip()
        if not source or not destination:
            continue
        rule: dict[str, Any] = {"source": source, "destination": destination}
        rule_exclude = _as_tuple(item.get("exclude"))
        if rule_exclude:
            rule["exclude"] = list(rule_exclude)
        if item.get("overwrite") is not None:
            rule["overwrite"] = bool(item["overwrite"])
        if not item.get("enabled", True):
            rule["enabled"] = False
        rules.append(rule)

    if not rules:
        raise ConfigError("At least one sync rule is required")

    document["rules"] = rules

    body = yaml.safe_dump(
        document,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )
    return "\n".join(lines) + body


def parse_yaml_text(text: str) -> SyncConfig:
    """Parse YAML text into a config object (used for pre-save validation in the dashboard)."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("PyYAML is not installed; cannot parse the config") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML: {exc}") from exc

    return parse_config(data)
