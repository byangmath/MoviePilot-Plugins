from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

from .models import EpisodeItem


class JellyfinServiceClient:
    def __init__(self, service: Any):
        self.service = service

    def enabled(self) -> bool:
        return bool(self.service)

    @staticmethod
    def _url(path: str, params: dict[str, Any] | None = None) -> str:
        query = urlencode(params or {})
        if query:
            return f"[HOST]{path.lstrip('/')}?{query}&api_key=[APIKEY]"
        return f"[HOST]{path.lstrip('/')}?api_key=[APIKEY]"

    @staticmethod
    def _json(response: Any) -> Any:
        if response is None:
            raise RuntimeError("媒体服务器无响应")
        if isinstance(response, (dict, list)):
            return response
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
        if getattr(response, "status_code", None) == 204:
            return {}
        if getattr(response, "content", None) in (b"", ""):
            return {}
        json_method = getattr(response, "json", None)
        if callable(json_method):
            return json_method()
        return response

    def recent_added_episodes(
        self,
        days: int,
        library_ids: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[EpisodeItem]:
        since = datetime.now(timezone.utc) - timedelta(days=max(int(days or 1), 1))
        ids = [item.strip() for item in (library_ids or []) if item and item.strip()]
        parent_ids: list[str | None] = ids or [None]
        episodes: dict[str, EpisodeItem] = {}

        for parent_id in parent_ids:
            params: dict[str, Any] = {
                "IncludeItemTypes": "Episode",
                "Recursive": "true",
                "IsMissing": "false",
                "Fields": "Path,DateCreated,ProviderIds,SeriesName,SeasonName,IndexNumber,ParentIndexNumber",
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "Limit": max(int(limit), 1),
                "EnableTotalRecordCount": "false",
            }
            if parent_id:
                params["ParentId"] = parent_id

            response = self.service.get_data(self._url("Items", params))
            for item in self._json(response).get("Items") or []:
                episode = EpisodeItem.from_jellyfin(item)
                if episode.item_id and episode.path and self._created_after(episode.date_created, since):
                    episodes[episode.item_id] = episode

        return sorted(
            episodes.values(),
            key=lambda item: item.date_created,
            reverse=True,
        )[:max(int(limit), 1)]

    def match_recent_episodes(
        self,
        target_paths: Iterable[str | Path],
        days: int,
        library_ids: Iterable[str] | None = None,
    ) -> dict[str, list[EpisodeItem]]:
        """Match recent Jellyfin items to MoviePilot destination paths."""
        targets = {
            self.path_key(path): Path(str(path)).name.casefold()
            for path in target_paths
            if path and self.path_key(path)
        }
        if not targets:
            return {}

        candidate_limit = min(max(len(targets) * 10, 50), 500)
        candidates = self.recent_added_episodes(
            days=days,
            library_ids=library_ids,
            limit=candidate_limit,
        )

        by_path: dict[str, list[EpisodeItem]] = {}
        by_name: dict[str, list[EpisodeItem]] = {}
        for episode in candidates:
            key = self.path_key(episode.path)
            if not key:
                continue
            by_path.setdefault(key, []).append(episode)
            by_name.setdefault(Path(episode.path).name.casefold(), []).append(episode)

        matches: dict[str, list[EpisodeItem]] = {}
        for target_key, filename in targets.items():
            exact = by_path.get(target_key)
            if exact:
                matches[target_key] = exact
                continue

            filename_matches = by_name.get(filename) or []
            unique_ids = {item.item_id for item in filename_matches}
            if len(unique_ids) == 1:
                matches[target_key] = filename_matches
        return matches

    @staticmethod
    def path_key(path: str | Path | None) -> str:
        if not path:
            return ""
        return str(path).strip().replace("\\", "/").rstrip("/").casefold()

    @staticmethod
    def _created_after(value: str, since: datetime) -> bool:
        if not value:
            return False
        try:
            created = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return created >= since
        except ValueError:
            return False

    def libraries(self) -> list[dict[str, str]]:
        response = self.service.get_data(self._url("Library/VirtualFolders"))
        items = self._json(response) or []
        libraries: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("ItemId") or item.get("Id")
            name = item.get("Name")
            if item_id and name:
                libraries.append({"title": str(name), "value": str(item_id)})
        return libraries

    def refresh_episode(
        self,
        item_id: str,
        metadata_mode: str = "FullRefresh",
        image_mode: str = "FullRefresh",
        replace_metadata: bool = True,
        replace_images: bool = True,
    ) -> None:
        params = {
            "MetadataRefreshMode": metadata_mode or "FullRefresh",
            "ImageRefreshMode": image_mode or "FullRefresh",
            "ReplaceAllMetadata": str(bool(replace_metadata)).lower(),
            "ReplaceAllImages": str(bool(replace_images)).lower(),
        }
        response = self.service.post_data(self._url(f"Items/{item_id}/Refresh", params))
        self._json(response)

    def scan_library(self) -> None:
        response = self.service.post_data(self._url("Library/Refresh"))
        self._json(response)
