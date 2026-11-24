"""Sync engine: copy files from the SD card to the destination per the rules,
skipping duplicates automatically.

Deduplication strategy:
  1. Destination missing -> copy directly
  2. Destination present -> compare (size, mtime) against the sync record; a match is skipped
  3. Still no match -> compare a sampled content hash; identical means skip and update
     the record, different means overwrite or skip according to the config

All progress is reported upwards through a callback (event_type, payload).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from .config import SyncConfig, SyncRule, match_pattern, validate_destination

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, dict], None]

HASH_CHUNK = 1024 * 1024


@dataclass
class FileTask:
    """One file to process."""

    source: Path
    destination: Path
    size: int
    mtime: float
    rule_source: str


@dataclass
class SyncStats:
    """Statistics for one sync run."""

    total_files: int = 0
    copied_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    total_bytes: int = 0
    copied_bytes: int = 0
    skipped_bytes: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    def to_dict(self) -> dict:
        return {
            "total_files": self.total_files,
            "copied_files": self.copied_files,
            "skipped_files": self.skipped_files,
            "failed_files": self.failed_files,
            "total_bytes": self.total_bytes,
            "copied_bytes": self.copied_bytes,
            "skipped_bytes": self.skipped_bytes,
            "elapsed": round(self.elapsed, 2),
            "finished_at": self.finished_at,
        }


def quick_hash(path: Path, sample_size: int = 64 * 1024) -> str:
    """Sample hash of a file: one chunk from the head and one from the tail, for a
    quick "is the content the same" check.

    Much faster than a full hash and well suited to large files. Returns
    "size:head:tail".
    """
    try:
        size = path.stat().st_size
    except OSError:
        return "missing"
    if size == 0:
        return "0:empty"
    try:
        with path.open("rb") as handle:
            head = handle.read(min(sample_size, size))
            if size > sample_size:
                handle.seek(max(0, size - sample_size))
                tail = handle.read(min(sample_size, size))
            else:
                tail = b""
    except OSError:
        return "unreadable"
    digest = hashlib.sha256()
    digest.update(head)
    digest.update(tail)
    return f"{size}:{digest.hexdigest()}"


def full_hash(path: Path) -> str:
    """Full SHA-256, for strict comparison."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def iter_source_files(card_root: Path, rule: SyncRule,
                      global_exclude: Sequence[str] = ()) -> Iterator[Path]:
    """Walk every file under the rule's source directory (recursively), applying both
    rule-level and global excludes.

    A rule-level exclude matches relative to the source directory, while a global
    exclude matches relative to the SD card root.
    """
    source = card_root / rule.source if rule.source else card_root
    if not source.exists():
        logger.warning("Source path for rule %s does not exist: %s", rule.source, source)
        return

    def excluded(rel_to_source: str, rel_to_card: str) -> bool:
        if rule.matches(rel_to_source):
            return True
        for pattern in global_exclude:
            if match_pattern(rel_to_card, pattern):
                return True
        return False

    if source.is_file():
        if not excluded(source.name, str(source.relative_to(card_root)).replace("\\", "/")):
            yield source
        return

    for dirpath, dirnames, filenames in os.walk(source):
        current = Path(dirpath)
        rel_dir = current.relative_to(source)
        # directory-level exclude
        dirnames[:] = [
            d for d in dirnames
            if not excluded(str(rel_dir / d).replace("\\", "/"),
                            str((current / d).relative_to(card_root)).replace("\\", "/"))
        ]
        for name in filenames:
            rel = str(rel_dir / name).replace("\\", "/")
            rel_card = str((current / name).relative_to(card_root)).replace("\\", "/")
            if excluded(rel, rel_card):
                continue
            yield current / name


def match_rules(card_root: Path, config: SyncConfig) -> list[FileTask]:
    """Expand the config into concrete file tasks, deduplicating sources/destinations."""
    tasks: list[FileTask] = []
    seen: set[tuple[str, str]] = set()

    for rule in config.active_rules():
        destination_root = config.resolve_destination(rule)
        source = card_root / rule.source if rule.source else card_root
        source_is_dir = source.is_dir()

        for file_path in iter_source_files(card_root, rule, config.global_excludes()):
            try:
                stat = file_path.stat()
            except OSError as exc:
                logger.warning("Cannot stat %s: %s", file_path, exc)
                continue

            if source_is_dir:
                rel = file_path.relative_to(source)
                destination = destination_root / rel
            else:
                destination = destination_root / file_path.name

            key = (str(file_path).lower(), str(destination).lower())
            if key in seen:
                continue
            seen.add(key)

            tasks.append(
                FileTask(
                    source=file_path,
                    destination=destination,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    rule_source=rule.source,
                )
            )
    return tasks


def destination_for_overwrite(rule: SyncRule, config: SyncConfig) -> bool:
    if rule.overwrite is not None:
        return rule.overwrite
    return config.overwrite


class SyncEngine:
    """Execute one sync run."""

    def __init__(self, config: SyncConfig, progress: ProgressCallback | None = None) -> None:
        self.config = config
        self.progress = progress or (lambda event, payload: None)
        self.stats = SyncStats()
        self._cancelled = False

    # ------------------------------------------------------------------
    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        """Whether this run was cancelled."""
        return self._cancelled

    def _emit(self, event: str, **payload) -> None:
        try:
            self.progress(event, payload)
        except Exception:  # pragma: no cover
            logger.exception("Progress callback failed")

    # ------------------------------------------------------------------
    def run(self, card_root: Path) -> SyncStats:
        """Run the sync and return the statistics."""
        self.stats = SyncStats()
        self._cancelled = False

        self._emit("phase", phase="planning", message="Parsing the config and scanning files...")

        # --- validate the destination paths ---
        for rule in self.config.active_rules():
            destination = self.config.resolve_destination(rule)
            error = validate_destination(str(destination), card_root)
            if error:
                self._emit("error", message=f"Invalid destination for rule {rule.source}: {error}")
                self.stats.failed_files += 1
                continue

        tasks = match_rules(card_root, self.config)
        self.stats.total_files = len(tasks)
        self.stats.total_bytes = sum(task.size for task in tasks)

        self._emit(
            "plan",
            total_files=self.stats.total_files,
            total_bytes=self.stats.total_bytes,
            rules=[r.source for r in self.config.active_rules()],
        )

        if not tasks:
            self.stats.finished_at = time.time()
            self._emit("finished", stats=self.stats.to_dict(), cancelled=False)
            return self.stats

        # --- copy ---
        self._emit("phase", phase="copying", message=f"Starting sync of {self.stats.total_files} files...")

        for index, task in enumerate(tasks, start=1):
            if self._cancelled:
                self._emit("cancelled", message="Sync cancelled")
                break

            self._emit(
                "file_start",
                index=index,
                total=self.stats.total_files,
                source=str(task.source),
                destination=str(task.destination),
                size=task.size,
                rule=task.rule_source,
            )

            try:
                outcome = self._process(task)
            except Exception as exc:
                logger.exception("Copy failed %s", task.source)
                self.stats.failed_files += 1
                self._emit(
                    "file_done",
                    index=index,
                    total=self.stats.total_files,
                    source=str(task.source),
                    destination=str(task.destination),
                    status="failed",
                    level="error",
                    message=str(exc),
                )
                continue

            if outcome == "copied":
                self.stats.copied_files += 1
                self.stats.copied_bytes += task.size
                status = "copied"
                level = "info"
            elif outcome == "skipped":
                self.stats.skipped_files += 1
                self.stats.skipped_bytes += task.size
                status = "skipped"
                level = "debug"
            else:
                self.stats.failed_files += 1
                status = "failed"
                level = "error"

            self._emit(
                "file_done",
                index=index,
                total=self.stats.total_files,
                source=str(task.source),
                destination=str(task.destination),
                status=status,
                level=level,
                size=task.size,
            )

        self.stats.finished_at = time.time()
        self._emit(
            "finished",
            stats=self.stats.to_dict(),
            cancelled=self._cancelled,
        )
        return self.stats

    # ------------------------------------------------------------------
    def _process(self, task: FileTask) -> str:
        """Process one file, returning copied / skipped / failed."""
        destination = task.destination

        if destination.exists() and destination.is_file():
            if self._is_duplicate(task):
                logger.debug("Skipping duplicate file: %s", task.source)
                return "skipped"
            overwrite = destination_for_overwrite(self._rule_of(task), self.config)
            if not overwrite:
                # content differs but overwriting is not allowed -> keep the destination and skip
                logger.info("Destination exists with different content and overwrite is off; skipping: %s", destination)
                return "skipped"
        elif destination.exists() and destination.is_dir():
            destination = destination / task.source.name

        if self.config.create_dirs:
            destination.parent.mkdir(parents=True, exist_ok=True)

        temp = destination.with_name(destination.name + ".dsync-part")
        try:
            shutil.copy2(task.source, temp)
            os.replace(temp, destination)
        except OSError:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
            raise
        return "copied"

    def _rule_of(self, task: FileTask) -> SyncRule:
        for rule in self.config.rules:
            if rule.source == task.rule_source:
                return rule
        return SyncRule(source="", destination="")

    def _is_duplicate(self, task: FileTask) -> bool:
        """Decide whether the destination file has the same content as the source."""
        destination = task.destination
        try:
            dst_stat = destination.stat()
        except OSError:
            return False

        # different size -> definitely not a duplicate
        if dst_stat.st_size != task.size:
            return False

        # same size, compare the sampled hash
        if quick_hash(task.source) == quick_hash(destination):
            return True

        # When the samples differ, do a full comparison for small files to avoid false negatives
        if task.size <= 32 * 1024 * 1024:
            try:
                return full_hash(task.source) == full_hash(destination)
            except OSError:
                return False
        return False
