from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


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
        return f"{series} {season}{episode} - {title}"


@dataclass
class OperationResult:
    success: bool = False
    skipped: bool = False
    message: str = ""
    source: Optional[Path] = None
    target: Optional[Path] = None


@dataclass
class RunResult:
    reorganize_candidates: int = 0
    refresh_candidates: int = 0
    previewed: int = 0
    refresh_previewed: int = 0
    reorganized: int = 0
    refreshed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        self.failed += 1
        self.errors.append(message)

    def summary(self) -> str:
        lines = [
            f"MP 最近整理记录 {self.reorganize_candidates} 条",
            f"重新整理试运行预览 {self.previewed} 条",
            f"重新整理成功 {self.reorganized} 集",
            f"匹配到 Jellyfin 剧集 {self.refresh_candidates} 集",
            f"元数据刷新试运行预览 {self.refresh_previewed} 集",
            f"元数据刷新成功 {self.refreshed} 集",
            f"跳过 {self.skipped} 集",
            f"失败 {self.failed} 集",
        ]
        if self.errors:
            lines.append("失败详情：")
            lines.extend(f"- {item}" for item in self.errors[:10])
            if len(self.errors) > 10:
                lines.append(f"- 其余 {len(self.errors) - 10} 条错误已省略，请查看日志")
        return "\n".join(lines)
