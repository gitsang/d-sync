"""同步引擎：按规则把 SD 卡文件复制到目标路径，自动跳过重复文件。

去重策略：
  1. 目标不存在 -> 直接复制
  2. 目标存在 -> 比较 (size, mtime) 与已同步记录；命中则跳过
  3. 仍未命中 -> 抽样比对内容哈希；相同则跳过并更新记录，不同则按配置覆盖/跳过

所有进度通过回调 (event_type, payload) 抛给上层。
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
    """一个待处理的文件。"""

    source: Path
    destination: Path
    size: int
    mtime: float
    rule_source: str


@dataclass
class SyncStats:
    """一次同步任务的统计结果。"""

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
    """对文件做抽样哈希：首尾各取一段，用于快速判断内容是否一致。

    比全量哈希快得多，适合大文件。返回 "size:head:tail" 形式。
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
    """全量 SHA-256，用于严格比对。"""
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
    """按规则遍历源目录下所有文件（递归），应用规则级与全局排除规则。

    规则级 exclude 相对于源目录匹配，全局 exclude 相对于 SD 卡根目录匹配。
    """
    source = card_root / rule.source if rule.source else card_root
    if not source.exists():
        logger.warning("规则 %s 的源路径不存在: %s", rule.source, source)
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
        # 目录级排除
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
    """把配置展开成具体的文件任务列表，并做源/目标去重。"""
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
                logger.warning("无法读取文件信息 %s: %s", file_path, exc)
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
    """执行一次同步任务。"""

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
        """本次任务是否被取消。"""
        return self._cancelled

    def _emit(self, event: str, **payload) -> None:
        try:
            self.progress(event, payload)
        except Exception:  # pragma: no cover
            logger.exception("进度回调异常")

    # ------------------------------------------------------------------
    def run(self, card_root: Path) -> SyncStats:
        """执行同步，返回统计信息。"""
        self.stats = SyncStats()
        self._cancelled = False

        self._emit("phase", phase="planning", message="正在解析配置与扫描文件…")

        # --- 校验目标路径 ---
        for rule in self.config.active_rules():
            destination = self.config.resolve_destination(rule)
            error = validate_destination(str(destination), card_root)
            if error:
                self._emit("error", message=f"规则 {rule.source} 目标路径非法：{error}")
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

        # --- 复制 ---
        self._emit("phase", phase="copying", message=f"开始同步 {self.stats.total_files} 个文件…")

        for index, task in enumerate(tasks, start=1):
            if self._cancelled:
                self._emit("cancelled", message="同步已取消")
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
                logger.exception("复制失败 %s", task.source)
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
        """处理单个文件，返回 copied / skipped / failed。"""
        destination = task.destination

        if destination.exists() and destination.is_file():
            if self._is_duplicate(task):
                logger.debug("跳过重复文件: %s", task.source)
                return "skipped"
            overwrite = destination_for_overwrite(self._rule_of(task), self.config)
            if not overwrite:
                # 内容不同但不允许覆盖 -> 保留目标，跳过
                logger.info("目标已存在且内容不同（未开启覆盖），跳过: %s", destination)
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
        """判断目标文件是否与源文件内容相同。"""
        destination = task.destination
        try:
            dst_stat = destination.stat()
        except OSError:
            return False

        # 大小不同 -> 一定不是重复
        if dst_stat.st_size != task.size:
            return False

        # 大小相同，抽样比对
        if quick_hash(task.source) == quick_hash(destination):
            return True

        # 抽样不一致时对小文件做全量比对，避免误判
        if task.size <= 32 * 1024 * 1024:
            try:
                return full_hash(task.source) == full_hash(destination)
            except OSError:
                return False
        return False
