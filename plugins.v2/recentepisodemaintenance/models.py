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
    def display_name(self) -> str:
        season = f"S{int(self.season_number):02d}" if self.season_number is not None else "S??"
        episode = f"E{int(self.episode_number):02d}" if self.episode_number is not None else "E??"
        title = self.name or "未知标题"
        series = self.series_name or "未知剧集"
        media_file = Path(self.path).name if self.path else "未知文件"
        return f"{series} {season}{episode}｜Jellyfin 标题：{title}｜文件：{media_file}"

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

    def add_error(self, message: str) -> None:
        self.failed += 1
        self.errors.append(message)

    def add_refreshed_title(self, title: str) -> None:
        if title and title not in self.refreshed_titles:
            self.refreshed_titles.append(title)

    def add_reorganized_title(self, title: str) -> None:
        if title and title not in self.reorganized_titles:
            self.reorganized_titles.append(title)

    def summary(self) -> str:
        lines = [
            f"本轮检查 MP 整理记录 {self.reorganize_candidates} 条",
            f"刷新和重新整理操作 {self.operations_used}/{self.operation_limit} 次",
            f"重新整理试运行预览 {self.previewed} 条",
            f"重新整理成功 {self.reorganized} 集",
            f"匹配到 Jellyfin 剧集 {self.refresh_candidates} 集",
            f"元数据刷新试运行预览 {self.refresh_previewed} 集",
            f"元数据刷新成功 {self.refreshed} 集",
            f"跳过 {self.skipped} 集",
            f"失败 {self.failed} 集",
        ]
        if self.refreshed_titles:
            lines.append("元数据刷新成功剧集：")
            lines.extend(f"- {title}" for title in self.refreshed_titles)
        if self.reorganized_titles:
            lines.append("重新整理成功剧集：")
            lines.extend(f"- {title}" for title in self.reorganized_titles)
        if self.errors:
            lines.append("失败详情：")
            lines.extend(f"- {item}" for item in self.errors[:10])
            if len(self.errors) > 10:
                lines.append(f"- 其余 {len(self.errors) - 10} 条错误已省略，请查看日志")
        return "\n".join(lines)
