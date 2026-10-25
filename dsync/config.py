"""SD 卡 YAML 配置的解析与校验。

配置文件默认位于 SD 卡根目录，文件名默认为 ``dsync.yml``
（可用 ``--config-name`` 覆盖）。

支持以下几种写法，从简到繁：

1) 最简形式::

       source: DCIM
       destination: D:/Photos/DCIM

2) 规则列表（推荐）::

       version: 1
       destination_root: D:/SD_Backup
       rules:
         - source: DCIM
           destination: Photos/DCIM
         - source: Documents/*.pdf
           destination: Docs

3) 映射表形式::

       destination_root: D:/SD_Backup
       mappings:
         DCIM: Photos/DCIM
         Documents: Docs

无论哪种写法，最终都会被归一化成 :class:`SyncConfig` -> ``rules`` 列表。
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
"""在 SD 卡根目录查找配置文件的候选文件名（首选 dsync.yml）。"""


class ConfigError(Exception):
    """配置文件缺失或格式非法。"""


def match_pattern(relative_path: str, pattern: str) -> bool:
    """判断相对路径是否命中排除模式。

    同时支持三种匹配方式：
      * 直接 glob 匹配整个相对路径，如 ``a/b/c.tmp``
      * 匹配文件名，如 ``*.tmp``
      * 匹配任意一级目录名，如 ``System Volume Information``
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
    # 支持 "**/xxx" 形式
    if pattern.startswith("**/") and fnmatch.fnmatch(name, pattern[3:]):
        return True
    return False


@dataclass
class SyncRule:
    """一条同步规则：把 SD 卡上的某个源映射到目标路径。"""

    source: str
    """相对于 SD 卡根目录的源路径，可含 ``*`` / ``**`` 通配符。"""

    destination: str
    """目标路径。绝对路径直接使用；相对路径基于 destination_root 解析。"""

    enabled: bool = True
    exclude: tuple[str, ...] = ()
    """额外的排除模式（glob），相对于源目录。"""

    overwrite: bool | None = None
    """覆盖已有目标文件。None 表示继承全局设置。"""

    # ------------------------------------------------------------------
    def matches(self, relative_path: str) -> bool:
        """判断 SD 卡内的一个相对路径是否命中本规则的排除列表。"""
        for pattern in self.exclude:
            if match_pattern(relative_path, pattern):
                return True
        return False


@dataclass
class SyncConfig:
    """归一化后的整份配置。"""

    rules: list[SyncRule] = field(default_factory=list)
    destination_root: str = ""
    """规则中相对目标路径的基准目录。"""

    overwrite: bool = False
    """目标已存在同名文件时是否覆盖（False 则按内容去重跳过）。"""

    create_dirs: bool = True
    """是否自动创建目标目录。"""

    exclude: tuple[str, ...] = ()
    """全局排除模式，作用于所有规则。"""

    max_workers: int = 4
    """并发复制线程数。"""

    enabled: bool = True
    """自动同步总开关。为 False 时插入设备不会自动同步，只能手动触发。"""

    source_path: Path | None = None
    """配置文件自身所在路径。"""

    card_root: Path | None = None
    """配置文件所在的 SD 卡根目录。"""

    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def resolve_destination(self, rule: SyncRule) -> Path:
        """把规则里的目标路径解析成绝对路径。"""
        dest = Path(rule.destination)
        if dest.is_absolute() or (dest.drive and dest.drive != ""):
            return dest
        if self.destination_root:
            return Path(self.destination_root) / dest
        return dest

    def global_excludes(self) -> tuple[str, ...]:
        return self.exclude

    def active_rules(self) -> list[SyncRule]:
        """参与同步的规则（enabled=True）。"""
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
# 解析
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
    """把 source 统一成相对 SD 卡根目录的 POSIX 风格路径。"""
    text = str(source).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/") if text not in ("/", "") else ""


def _rule_from_mapping(item: dict, index: int, defaults: dict) -> SyncRule:
    source = _first(item, ("source", "src", "from", "input", "path"))
    if source is None:
        raise ConfigError(f"第 {index + 1} 条规则缺少 source 字段")
    source = str(source)

    destination = _first(item, ("destination", "dest", "to", "target", "output", "dst"))
    if destination is None:
        raise ConfigError(f"规则 {index + 1}（source={source}）缺少 destination 字段")

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
    """把已加载的 YAML 数据归一化成 :class:`SyncConfig`。"""
    if data is None:
        raise ConfigError("配置文件内容为空")
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射（dict），实际为 {type(data).__name__}")

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

    # 自动同步总开关：enable / auto_sync
    # 注意：规则级的 enabled 含义不同（单条规则是否参与），这里不共用该键名。
    enabled = _as_bool(
        _first(data, ("enable", "auto_sync"), None)
        if _first(data, ("enable", "auto_sync"), None) is not None
        else _first(defaults, ("enable", "auto_sync"), None),
        True,
    )

    rules: list[SyncRule] = []

    # --- 形式 1：单条规则（顶层直接有 source/destination） ---
    if _first(data, ("source", "src", "from")) is not None:
        merged = dict(data)
        rules.append(_rule_from_mapping(merged, 0, defaults))

    # --- 形式 2：rules / sync / items / tasks 列表 ---
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
                    raise ConfigError(f"第 {index + 1} 条规则必须是映射（dict）")
                rules.append(_rule_from_mapping(item, index, defaults))
        elif isinstance(list_node, dict):
            # --- 形式 3：映射表 {source: destination} ---
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
                "未找到任何同步规则。请使用 rules 列表、mappings 映射表，"
                "或直接在顶层写 source / destination。"
            )

    # 保留 enabled=False 的规则（供页面展示与切换），实际参与同步时再过滤

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
    """从磁盘读取并解析配置文件。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}: {exc}") from exc

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("缺少 PyYAML 依赖，无法解析配置文件") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 {path.name} YAML 格式错误: {exc}") from exc

    return parse_config(data, source_path=path, card_root=card_root)


def find_config_file(card_root: Path, config_names: Iterable[str] = DEFAULT_CONFIG_NAMES) -> Path | None:
    """在 SD 卡根目录查找配置文件。"""
    for name in config_names:
        candidate = card_root / name
        if candidate.is_file():
            return candidate
    return None


def validate_destination(destination: str, card_root: Path | None = None) -> str | None:
    """检查目标路径是否合法，返回错误描述（合法则返回 None）。"""
    if not destination.strip():
        return "目标路径为空"
    try:
        target = Path(destination)
    except (TypeError, ValueError):
        return f"目标路径非法: {destination}"
    if card_root is not None:
        try:
            resolved = target.resolve() if target.is_absolute() else Path(os.path.abspath(target)).resolve()
            card_resolved = card_root.resolve()
            if resolved == card_resolved or card_resolved in resolved.parents:
                return f"目标路径不能位于 SD 卡内部: {destination}"
        except OSError:
            pass
    return None


# ----------------------------------------------------------------------
# 序列化：编辑模型 <-> YAML 文本
# ----------------------------------------------------------------------
def config_to_editable(config: SyncConfig) -> dict:
    """把配置对象转换成前端可编辑的模型（纯数据，可 JSON 序列化）。"""
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
    """把前端编辑模型序列化成 YAML 文本。

    只输出有意义的字段，保持文件整洁；同时附带注释说明各字段用途。
    """
    import yaml

    lines: list[str] = [
        "# D-Sync 同步配置",
        "# 可在 D-Sync 看板页面直接编辑此文件，保存后自动生效。",
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
        raise ConfigError("至少需要一条同步规则")

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
    """解析 YAML 文本为配置对象（供页面保存前的预校验使用）。"""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("缺少 PyYAML 依赖，无法解析配置") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 格式错误: {exc}") from exc

    return parse_config(data)
