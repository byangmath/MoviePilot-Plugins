from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
        json_method = getattr(response, "json", None)
        if callable(json_method):
            return json_method()
        return response

    def recent_episodes(self, days: int, library_ids: Iterable[str] | None = None) -> list[EpisodeItem]:
        since = datetime.now(timezone.utc) - timedelta(days=max(int(days or 1), 1))
        params: dict[str, Any] = {
            "IncludeItemTypes": "Episode",
            "Recursive": "true",
            "IsMissing": "false",
            "MinPremiereDate": since.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "Fields": "Path,PremiereDate,ProviderIds,SeriesName,SeasonName,IndexNumber,ParentIndexNumber",
            "SortBy": "PremiereDate,DateCreated",
            "SortOrder": "Descending",
        }
        ids = [item.strip() for item in (library_ids or []) if item and item.strip()]
        if ids:
            params["ParentId"] = ",".join(ids)

        response = self.service.get_data(self._url("Items", params))
        items = self._json(response).get("Items") or []
        episodes = [EpisodeItem.from_jellyfin(item) for item in items]
        return [item for item in episodes if item.item_id and item.path and item.premiere_date]

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
        image_mode: str = "Default",
        replace_metadata: bool = False,
        replace_images: bool = False,
    ) -> None:
        params = {
            "MetadataRefreshMode": metadata_mode or "FullRefresh",
            "ImageRefreshMode": image_mode or "Default",
            "ReplaceAllMetadata": str(bool(replace_metadata)).lower(),
            "ReplaceAllImages": str(bool(replace_images)).lower(),
        }
        response = self.service.post_data(self._url(f"Items/{item_id}/Refresh", params))
        self._json(response)

    def scan_library(self) -> None:
        response = self.service.post_data(self._url("Library/Refresh"))
        self._json(response)
