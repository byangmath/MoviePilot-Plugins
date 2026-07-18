from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import sleep
from typing import Any, Iterable
from urllib.parse import urlencode

from .models import EpisodeItem, EpisodeTarget

logger = logging.getLogger(__name__)


class JellyfinServiceClient:
    _REQUEST_ATTEMPTS = 3
    _REQUEST_RETRY_DELAY_SECONDS = 30

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

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request_json("get_data", path, params)

    def _post_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request_json("post_data", path, params)

    def _request_json(
        self,
        method_name: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = self._url(path, params)
        last_error: Exception | None = None
        for attempt in range(1, self._REQUEST_ATTEMPTS + 1):
            try:
                method = getattr(self.service, method_name)
                return self._json(method(url))
            except Exception as err:
                last_error = err
                if attempt >= self._REQUEST_ATTEMPTS:
                    break
                logger.warning(
                    "[最近剧集维护] Jellyfin 请求失败，%s 秒后重试（%s/%s）：%s",
                    self._REQUEST_RETRY_DELAY_SECONDS,
                    attempt,
                    self._REQUEST_ATTEMPTS,
                    err,
                )
                sleep(self._REQUEST_RETRY_DELAY_SECONDS)
        if last_error:
            raise last_error
        raise RuntimeError("媒体服务器无响应")

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
                "Fields": "Path,DateCreated,SeriesName,SeasonName,IndexNumber,ParentIndexNumber",
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "Limit": max(int(limit), 1),
                "EnableTotalRecordCount": "false",
            }
            if parent_id:
                params["ParentId"] = parent_id

            for item in self._get_json("Items", params).get("Items") or []:
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
        targets: Iterable[EpisodeTarget],
        days: int,
        library_ids: Iterable[str] | None = None,
    ) -> dict[str, list[EpisodeItem]]:
        """Match MoviePilot history targets to Jellyfin episodes."""
        target_map = {
            self.path_key(target.path): target
            for target in targets
            if target.path and self.path_key(target.path)
        }
        if not target_map:
            return {}

        candidate_limit = min(max(len(target_map) * 10, 50), 500)
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
        for target_key, target in target_map.items():
            exact = by_path.get(target_key)
            if exact:
                matches[target_key] = exact
                continue

            filename = Path(target.path).name.casefold()
            filename_matches = by_name.get(filename) or []
            unique_ids = {item.item_id for item in filename_matches}
            if len(unique_ids) == 1:
                matches[target_key] = filename_matches

        unmatched = [target for key, target in target_map.items() if key not in matches]
        if unmatched:
            matches.update(self._match_by_series(unmatched, library_ids))
        return matches

    def _match_by_series(
        self,
        targets: Iterable[EpisodeTarget],
        library_ids: Iterable[str] | None,
    ) -> dict[str, list[EpisodeItem]]:
        series_items = self._series_items(library_ids)
        by_folder: dict[str, set[str]] = {}

        for item in series_items:
            series_id = str(item.get("Id") or "")
            folder = self._path_folder_key(item.get("Path"))
            if not series_id or not folder:
                continue
            by_folder.setdefault(folder, set()).add(series_id)

        targets_by_series: dict[str, list[EpisodeTarget]] = {}
        for target in targets:
            folder = self._target_series_folder_key(target.path)
            series_ids = by_folder.get(folder) or set()
            for series_id in series_ids:
                targets_by_series.setdefault(series_id, []).append(target)

        matches: dict[str, list[EpisodeItem]] = {}
        for series_id, series_targets in targets_by_series.items():
            episodes = self._series_episodes(series_id)
            by_path: dict[str, list[EpisodeItem]] = {}
            by_tail: dict[str, list[EpisodeItem]] = {}
            by_name: dict[str, list[EpisodeItem]] = {}
            for episode in episodes:
                path_key = self.path_key(episode.path)
                if path_key:
                    by_path.setdefault(path_key, []).append(episode)
                    by_tail.setdefault(self._path_tail_key(episode.path), []).append(episode)
                    by_name.setdefault(Path(episode.path).name.casefold(), []).append(episode)

            for target in series_targets:
                target_key = self.path_key(target.path)
                candidates = by_path.get(target_key) or []
                if not candidates:
                    tail_matches = by_tail.get(self._path_tail_key(target.path)) or []
                    if len({item.item_id for item in tail_matches}) == 1:
                        candidates = tail_matches
                if not candidates:
                    filename_matches = by_name.get(Path(target.path).name.casefold()) or []
                    if len({item.item_id for item in filename_matches}) == 1:
                        candidates = filename_matches
                if candidates:
                    existing = {item.item_id for item in matches.get(target_key) or []}
                    matches.setdefault(target_key, []).extend(
                        item for item in candidates if item.item_id not in existing
                    )
        return matches

    @classmethod
    def _target_series_folder_key(cls, path: str | Path | None) -> str:
        parts = cls._path_parts(path)
        if len(parts) < 2:
            return ""
        parent = parts[-2]
        if parent.startswith("season ") or parent.startswith("season_") or parent.startswith("season-"):
            return parts[-3] if len(parts) >= 3 else ""
        return parent

    @classmethod
    def _path_folder_key(cls, path: str | Path | None) -> str:
        parts = cls._path_parts(path)
        return parts[-1] if parts else ""

    @classmethod
    def _path_tail_key(cls, path: str | Path | None, count: int = 3) -> str:
        parts = cls._path_parts(path)
        return "/".join(parts[-count:])

    @staticmethod
    def _path_parts(path: str | Path | None) -> tuple[str, ...]:
        if not path:
            return ()
        return tuple(
            part.casefold()
            for part in str(path).strip().replace("\\", "/").split("/")
            if part
        )

    def _series_items(self, library_ids: Iterable[str] | None) -> list[dict[str, Any]]:
        ids = [item.strip() for item in (library_ids or []) if item and item.strip()]
        parent_ids: list[str | None] = ids or [None]
        series: dict[str, dict[str, Any]] = {}
        for parent_id in parent_ids:
            params: dict[str, Any] = {
                "IncludeItemTypes": "Series",
                "Recursive": "true",
                "IsMissing": "false",
                "Fields": "Path",
            }
            if parent_id:
                params["ParentId"] = parent_id
            for item in self._paged_items(params):
                item_id = str(item.get("Id") or "")
                if item_id:
                    series[item_id] = item
        return list(series.values())

    def _series_episodes(self, series_id: str) -> list[EpisodeItem]:
        params = {
            "ParentId": series_id,
            "IncludeItemTypes": "Episode",
            "Recursive": "true",
            "IsMissing": "false",
            "Fields": "Path,DateCreated,SeriesName,SeasonName,IndexNumber,ParentIndexNumber",
        }
        episodes: dict[str, EpisodeItem] = {}
        for item in self._paged_items(params):
            episode = EpisodeItem.from_jellyfin(item)
            if episode.item_id:
                episodes[episode.item_id] = episode
        return list(episodes.values())

    def _paged_items(self, params: dict[str, Any], page_size: int = 500) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        start_index = 0
        while True:
            page_params = {
                **params,
                "StartIndex": start_index,
                "Limit": page_size,
                "EnableTotalRecordCount": "true",
            }
            payload = self._get_json("Items", page_params) or {}
            page = payload.get("Items") or []
            items.extend(item for item in page if isinstance(item, dict))
            start_index += len(page)
            total = int(payload.get("TotalRecordCount") or 0)
            if not page or len(page) < page_size or (total and start_index >= total):
                break
        return items

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
        items = self._get_json("Library/VirtualFolders") or []
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
        self._post_json(f"Items/{item_id}/Refresh", params)

    def scan_library(self) -> None:
        self._post_json("Library/Refresh")
