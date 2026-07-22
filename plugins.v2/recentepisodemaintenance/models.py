from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Optional
import unicodedata


_EPISODE_MARKER = re.compile(r"s\d{1,3}e\d{1,4}(?:[\s._-]*e\d{1,4})?", re.IGNORECASE)


def _title_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(character for character in normalized if character.isalnum())


@dataclass
class EpisodeItem:
    item_id: str
    name: str = ""
    series_name: str = ""
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    date_created: str = ""
    path: str = ""
    provider_ids: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_jellyfin(cls, item: dict[str, Any]) -> "EpisodeItem":
        return cls(
            item_id=str(item.get("Id") or ""),
            name=item.get("Name") or "",
            series_name=item.get("SeriesName") or "",
            season_number=item.get("ParentIndexNumber"),
            episode_number=item.get("IndexNumber"),
            date_created=item.get("DateCreated") or "",
            path=item.get("Path") or "",
            provider_ids=item.get("ProviderIds") or {},
        )

    @property
    def episode_label(self) -> str:
        season = f"S{int(self.season_number):02d}" if self.season_number is not None else "S??"
        episode = f"E{int(self.episode_number):02d}" if self.episode_number is not None else "E??"
        series = self.series_name or "未知剧集"
        return f"{series} {season}{episode}"

    @property
    def display_name(self) -> str:
        title = self.name or "未知标题"
        media_file = Path(self.path).name if self.path else "未知文件"
        return f"{self.episode_label}｜Jellyfin 标题：{title}｜文件：{media_file}"

    def title_matches_filename(self) -> bool:
        """Check whether Jellyfin's episode title is the title stored in the media filename."""
        return self.title_matches_path(self.path)

    def title_matches_path(self, path: str | Path | None) -> bool:
        """Check whether Jellyfin's episode title occurs in an expected media filename."""
        title_key = _title_key(self.name)
        if not title_key or not path:
            return False

        filename_stem = Path(path).stem
        marker = _EPISODE_MARKER.search(filename_stem)
        if not marker:
            return False

        filename_title_key = _title_key(filename_stem[marker.end():])
        return bool(filename_title_key) and filename_title_key.endswith(title_key)


@dataclass(frozen=True)
class EpisodeTarget:
    path: str


@dataclass
class OperationResult:
    success: bool = False
    skipped: bool = False
    message: str = ""
    source: Optional[Path] = None
    target: Optional[Path] = None


@dataclass
class RunResult:
    operation_limit: int = 0
    operations_used: int = 0
    actions_submitted: int = 0
    reorganize_candidates: int = 0
    refresh_candidates: int = 0
    previewed: int = 0
    refresh_previewed: int = 0
    reorganized: int = 0
    refreshed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    refreshed_titles: list[str] = field(default_factory=list)
    reorganized_titles: list[str] = field(default_factory=list)
    failed_titles: list[str] = field(default_factory=list)
    queue_counts: dict[str, int] = field(default_factory=dict)
    _skipped_keys: set[str] = field(default_factory=set, repr=False)

    def add_error(self, message: str, file_path: str | Path | None = None) -> None:
        self.failed += 1
        self.errors.append(message)
        normalized_path = str(file_path or "").strip()
        if normalized_path and normalized_path not in self.failed_titles:
            self.failed_titles.append(normalized_path)

    def add_refreshed_title(self, title: str) -> None:
        if title and title not in self.refreshed_titles:
            self.refreshed_titles.append(title)

    def add_reorganized_title(self, title: str) -> None:
        if title and title not in self.reorganized_titles:
            self.reorganized_titles.append(title)

    def add_skipped(self, *keys: str) -> None:
        values = keys or (f"unknown:{len(self._skipped_keys)}",)
        for key in values:
            normalized = str(key or "").strip()
            if not normalized:
                normalized = f"unknown:{len(self._skipped_keys)}"
            if normalized in self._skipped_keys:
                continue
            self._skipped_keys.add(normalized)
            self.skipped += 1

    def summary(self) -> str:
        lines = [f"本轮检查：MP 视频整理记录 {self.reorganize_candidates} 条"]
        if self.queue_counts:
            lines[0] += (
                "；队列："
                f"本轮待复查 {self.queue_counts.get('pending', 0)} 条，"
                f"新记录 {self.queue_counts.get('new', 0)} 条，"
                f"到期复查 {self.queue_counts.get('monitoring', 0)} 条"
            )
            lines.append(
                "当前状态："
                f"等待刷新确认 {self.queue_counts.get('refresh_waiting', 0)} 条，"
                f"等待复查 {self.queue_counts.get('monitoring_waiting', 0)} 条，"
                f"等待附件 {self.queue_counts.get('sidecar_waiting', 0)} 条，"
                f"等待清理 {self.queue_counts.get('cleanup_waiting', 0)} 条，"
                f"已完成 {self.queue_counts.get('complete', 0)} 条，"
                f"需人工检查 {self.queue_counts.get('attention', 0)} 条"
            )
        lines.extend(
            [
                f"操作统计：刷新和重新整理 {self.operations_used}/{self.operation_limit} 次；"
                f"重新整理试运行预览 {self.previewed} 条，成功 {self.reorganized} 集；"
                f"元数据刷新试运行预览 {self.refresh_previewed} 集，成功 {self.refreshed} 集",
                f"匹配结果：匹配到 Jellyfin 剧集 {self.refresh_candidates} 集，"
                f"跳过 {self.skipped} 集，失败 {self.failed} 集",
            ]
        )
        if self.refreshed_titles:
            lines.append("元数据刷新成功剧集：")
            lines.extend(f"- {title}" for title in self.refreshed_titles)
        if self.reorganized_titles:
            lines.append("重新整理成功剧集：")
            lines.extend(f"- {title}" for title in self.reorganized_titles)
        if self.failed_titles:
            lines.append("处理失败剧集：")
            lines.extend(f"- {title}" for title in self.failed_titles)
        if self.errors:
            lines.append("失败详情：")
            lines.extend(f"- {item}" for item in self.errors[:10])
            if len(self.errors) > 10:
                lines.append(f"- 其余 {len(self.errors) - 10} 条错误已省略，请查看日志")
        return "\n".join(lines)

    def should_notify(self) -> bool:
        return self.actions_submitted > 0 or self.failed > 0
