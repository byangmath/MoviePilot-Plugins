from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests

from .models import EpisodeItem


class JellyfinClient:
    def __init__(self, server_url: str, api_key: str, user_id: str = "", timeout: int = 30):
        self.server_url = (server_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.user_id = user_id or ""
        self.timeout = timeout

    def enabled(self) -> bool:
        return bool(self.server_url and self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "X-Emby-Token": self.api_key,
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.server_url}/{path.lstrip('/')}"

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

        endpoint = f"/Users/{self.user_id}/Items" if self.user_id else "/Items"
        response = requests.get(
            self._url(endpoint),
            params=params,
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        items = response.json().get("Items") or []
        episodes = [EpisodeItem.from_jellyfin(item) for item in items]
        return [item for item in episodes if item.item_id and item.path and item.premiere_date]

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
        response = requests.post(
            self._url(f"/Items/{item_id}/Refresh"),
            params=params,
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()

    def scan_library(self) -> None:
        response = requests.post(
            self._url("/Library/Refresh"),
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
