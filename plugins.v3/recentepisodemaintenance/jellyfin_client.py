from __future__ import annotations

import logging
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import sleep
from typing import Any, Iterable
from urllib.parse import urlencode

from .models import EpisodeItem, EpisodeTarget

logger = logging.getLogger(__name__)


class DanmuSubmissionError(RuntimeError):
    """Distinguish rejected operations from writes or queue requests with unknown results."""

    def __init__(self, message: str, *, uncertain: bool, cause: Exception | None = None):
        super().__init__(message)
        self.uncertain = uncertain
        status = getattr(getattr(cause, "response", None), "status_code", None)
        self.status_code = status if isinstance(status, int) else None


class JellyfinServiceClient:
    _REQUEST_ATTEMPTS = 3
    _REQUEST_RETRY_DELAY_SECONDS = 30
    # Movie/Episode fields consumed by Jellyfin's ItemUpdateController, plus identity.
    _DANMU_EDIT_FIELDS = frozenset({
        "Id", "Type", "Path", "Name", "ForcedSortName", "OriginalTitle",
        "CriticRating", "CommunityRating", "IndexNumber", "ParentIndexNumber",
        "Overview", "Genres", "AirsAfterSeasonNumber", "AirsBeforeEpisodeNumber",
        "AirsBeforeSeasonNumber", "Taglines", "Studios", "DateCreated", "EndDate",
        "PremiereDate", "ProductionYear", "OfficialRating", "CustomRating", "Tags",
        "ProductionLocations", "PreferredMetadataCountryCode", "PreferredMetadataLanguage",
        "DisplayOrder", "AspectRatio", "LockData", "LockedFields", "ProviderIds",
        "Video3DFormat", "People",
    })

    def __init__(self, service: Any):
        self.service = service

    def enabled(self) -> bool:
        return bool(self.service)

    @staticmethod
    def _url(path: str, params: dict[str, Any] | None = None) -> str:
        query = urlencode(params or {}).replace("%5BUSER%5D", "[USER]")
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

    def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        retry: bool = True,
    ) -> Any:
        return self._request_json("get_data", path, params, retry=retry)

    def _post_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        retry: bool = True,
    ) -> Any:
        return self._request_json("post_data", path, params, retry=retry)

    def _request_json(
        self,
        method_name: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        retry: bool = True,
    ) -> Any:
        url = self._url(path, params)
        request_attempts = self._REQUEST_ATTEMPTS if retry else 1
        last_error: Exception | None = None
        for attempt in range(1, request_attempts + 1):
            try:
                method = getattr(self.service, method_name)
                return self._json(method(url))
            except Exception as err:
                last_error = err
                if (
                    attempt >= request_attempts
                    or not self._is_retryable_error(err)
                ):
                    break
                logger.warning(
                    "[最近媒体维护] Jellyfin 请求失败，%s 秒后重试（%s/%s）：%s",
                    self._REQUEST_RETRY_DELAY_SECONDS,
                    attempt,
                    request_attempts,
                    err,
                )
                sleep(self._REQUEST_RETRY_DELAY_SECONDS)
        if last_error:
            raise last_error
        raise RuntimeError("媒体服务器无响应")

    @staticmethod
    def _is_retryable_error(error: Exception) -> bool:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code is None:
            status_code = getattr(error, "status_code", None)
        try:
            status = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            status = None
        if status is not None:
            return status == 429 or 500 <= status < 600

        error_name = type(error).__name__.casefold()
        if any(
            marker in error_name
            for marker in (
                "timeout",
                "connectionerror",
                "proxyerror",
                "sslerror",
            )
        ):
            return True
        return isinstance(error, RuntimeError) and "无响应" in str(error)

    def recent_added_episodes(
        self,
        days: int,
        library_ids: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[EpisodeItem]:
        return self.recent_added_media(
            media_type="tv",
            days=days,
            library_ids=library_ids,
            limit=limit,
        )

    def recent_added_media(
        self,
        media_type: str,
        days: int,
        library_ids: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[EpisodeItem]:
        since = datetime.now(timezone.utc) - timedelta(days=max(int(days or 1), 1))
        ids = [item.strip() for item in (library_ids or []) if item and item.strip()]
        parent_ids: list[str | None] = ids or [None]
        media_items: dict[str, EpisodeItem] = {}
        item_type = "Movie" if str(media_type).casefold() == "movie" else "Episode"

        for parent_id in parent_ids:
            params: dict[str, Any] = {
                "IncludeItemTypes": item_type,
                "Recursive": "true",
                "IsMissing": "false",
                "Fields": (
                    "Path,DateCreated,ProviderIds,SeriesName,SeasonName,"
                    "IndexNumber,ParentIndexNumber"
                ),
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "Limit": max(int(limit), 1),
                "EnableTotalRecordCount": "false",
            }
            if parent_id:
                params["ParentId"] = parent_id

            for item in self._get_json("Items", params).get("Items") or []:
                media_item = EpisodeItem.from_jellyfin(item)
                if (
                    media_item.item_id
                    and media_item.path
                    and self._created_after(media_item.date_created, since)
                ):
                    media_items[media_item.item_id] = media_item

        return sorted(
            media_items.values(),
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
        episode_targets = [
            EpisodeTarget(path=target.path, media_type="tv")
            for target in targets
        ]
        return self.match_recent_media(
            targets=episode_targets,
            days=days,
            library_ids=library_ids,
        )

    def match_recent_media(
        self,
        targets: Iterable[EpisodeTarget],
        days: int,
        library_ids: Iterable[str] | None = None,
    ) -> dict[str, list[EpisodeItem]]:
        """Match MoviePilot movie and episode targets to Jellyfin items."""
        target_map = {
            self.path_key(target.path): target
            for target in targets
            if target.path and self.path_key(target.path)
        }
        if not target_map:
            return {}

        matches: dict[str, list[EpisodeItem]] = {}
        for media_type in ("tv", "movie"):
            media_targets = {
                key: target
                for key, target in target_map.items()
                if str(target.media_type or "tv").casefold() == media_type
            }
            if not media_targets:
                continue
            candidate_limit = min(max(len(media_targets) * 10, 50), 500)
            candidates = self.recent_added_media(
                media_type=media_type,
                days=days,
                library_ids=library_ids,
                limit=candidate_limit,
            )
            matches.update(self._match_targets(media_targets, candidates))
            unmatched = {
                key: target
                for key, target in media_targets.items()
                if key not in matches
            }
            if not unmatched:
                continue
            if media_type == "tv":
                matches.update(self._match_by_series(unmatched.values(), library_ids))
            else:
                matches.update(
                    self._match_targets(
                        unmatched,
                        self._all_media_items("Movie", library_ids),
                    )
                )
        return matches

    def _match_targets(
        self,
        target_map: dict[str, EpisodeTarget],
        candidates: Iterable[EpisodeItem],
    ) -> dict[str, list[EpisodeItem]]:
        by_path: dict[str, list[EpisodeItem]] = {}
        by_name: dict[str, list[EpisodeItem]] = {}
        for media_item in candidates:
            key = self.path_key(media_item.path)
            if not key:
                continue
            by_path.setdefault(key, []).append(media_item)
            by_name.setdefault(Path(media_item.path).name.casefold(), []).append(media_item)

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
        return matches

    def _all_media_items(
        self,
        item_type: str,
        library_ids: Iterable[str] | None,
    ) -> list[EpisodeItem]:
        ids = [item.strip() for item in (library_ids or []) if item and item.strip()]
        parent_ids: list[str | None] = ids or [None]
        media_items: dict[str, EpisodeItem] = {}
        for parent_id in parent_ids:
            params: dict[str, Any] = {
                "IncludeItemTypes": item_type,
                "Recursive": "true",
                "IsMissing": "false",
                "Fields": "Path,DateCreated,ProviderIds",
            }
            if parent_id:
                params["ParentId"] = parent_id
            for item in self._paged_items(params):
                media_item = EpisodeItem.from_jellyfin(item)
                if media_item.item_id and media_item.path:
                    media_items[media_item.item_id] = media_item
        return list(media_items.values())

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

    def libraries(self, *, retry: bool = True) -> list[dict[str, str]]:
        items = self._get_json("Library/VirtualFolders", retry=retry) or []
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

    def danmu_settings(self) -> dict[str, Any]:
        plugins = self._get_json("Plugins")
        matches = [
            item for item in plugins or []
            if isinstance(item, dict) and str(item.get("Name", "")).casefold() == "danmu"
            and str(item.get("Status", "Active")).casefold() == "active"
        ]
        if len(matches) != 1 or not matches[0].get("Id"):
            raise RuntimeError("未找到唯一且已启用的 Jellyfin Danmu 插件")
        settings = self._get_json(f"Plugins/{matches[0]['Id']}/Configuration")
        if not isinstance(settings, dict):
            raise RuntimeError("无法读取 Danmu 插件配置")
        if not any(
            item.get("Name") == "弹幕API" and item.get("Enable") is True
            for item in settings.get("Scrapers") or [] if isinstance(item, dict)
        ):
            raise RuntimeError("请先在 Danmu 插件中启用弹幕 API 来源")
        return settings

    def danmu_item(self, item_id: str) -> dict[str, Any]:
        # Read the single-item DTO, then project its editable fields before posting.
        item = self._get_json(f"Items/{item_id}", {"userId": "[USER]"})
        if not isinstance(item, dict) or str(item.get("Id", "")) != item_id:
            raise RuntimeError("未获取到完整 Jellyfin 媒体条目")
        if not item.get("Name") or not item.get("Path") or not isinstance(item.get("ProviderIds"), dict):
            raise RuntimeError("Jellyfin 条目缺少名称、路径或媒体标识，拒绝修改")
        if not isinstance(item.get("LockData"), bool) or any(
            not isinstance(item.get(field), list)
            for field in ("Tags", "Genres", "LockedFields")
        ):
            raise RuntimeError("Jellyfin 条目缺少完整编辑字段，拒绝修改以保护原元数据")
        if item.get("Type") not in {"Episode", "Movie"}:
            raise RuntimeError("弹幕补全仅处理电影和单集剧集")
        supported = getattr(self, "_danmu_supported_types", set())
        if item["Type"] not in supported:
            editor = self._get_json(f"Items/{item_id}/MetadataEditor")
            if not isinstance(editor, dict) or not any(
                info.get("Key") == "DanmuID"
                for info in editor.get("ExternalIdInfos") or [] if isinstance(info, dict)
            ):
                raise RuntimeError("当前 Danmu 插件未提供该媒体的 DanmuID 编辑字段，请检查插件版本")
            self._danmu_supported_types = supported | {item["Type"]}
        return item

    @classmethod
    def _danmu_edit_payload(cls, item: dict[str, Any]) -> dict[str, Any]:
        payload = {key: deepcopy(value) for key, value in item.items() if key in cls._DANMU_EDIT_FIELDS}
        if isinstance(payload.get("People"), list):
            payload["People"] = [
                {key: value for key, value in person.items() if key in {"Name", "Role", "Type"}}
                for person in payload["People"]
            ]
        if isinstance(payload.get("Studios"), list):
            payload["Studios"] = [{"Name": studio["Name"]} for studio in payload["Studios"]]
        return payload

    def set_danmu_id(self, item: dict[str, Any], provider_value: str) -> None:
        try:
            current = self.danmu_item(str(item["Id"]))
            payload = self._danmu_edit_payload(current)
        except Exception as err:
            raise DanmuSubmissionError(f"弹幕关联写入前检查失败，尚未提交：{err}", uncertain=False, cause=err) from err
        if payload != self._danmu_edit_payload(item):
            raise DanmuSubmissionError("Jellyfin 可编辑元数据已发生变化，本轮未写入弹幕关联", uncertain=False)
        provider_id = f"danmuapi:{provider_value}"
        payload["ProviderIds"]["DanmuID"] = provider_id
        # Preserve editable metadata, never send display-only Trickplay/MediaSources/UserData.
        try:
            response = self.service.post_data(
                self._url(f"Items/{item['Id']}"),
                data=json.dumps(payload, ensure_ascii=False),
                headers={"Content-Type": "application/json"},
            )
            self._json(response)
        except Exception as err:
            status = getattr(getattr(err, "response", None), "status_code", None)
            if isinstance(status, int) and 400 <= status < 500 and status != 408:
                raise DanmuSubmissionError(f"写入弹幕关联被拒绝：{err}", uncertain=False, cause=err) from err
            # Server errors may occur before or after saving; one read avoids blind re-posting.
            try:
                saved = self._get_json(f"Items/{item['Id']}", {"userId": "[USER]"}, retry=False)
            except Exception:
                saved = None
            readable = isinstance(saved, dict) and str(saved.get("Id")) == str(item["Id"]) and isinstance(saved.get("ProviderIds"), dict)
            if readable and saved["ProviderIds"].get("DanmuID") != provider_id and isinstance(status, int) and status != 408:
                raise DanmuSubmissionError(f"写入弹幕关联失败，回读未发现目标 DanmuID：{err}", uncertain=False, cause=err) from err
            if readable and saved["ProviderIds"].get("DanmuID") == provider_id:
                detail = "回读已发现目标 DanmuID，下载结果仍待确认"
            elif readable:
                # A timed-out request can still be running after this read returns.
                detail = "回读暂未发现目标 DanmuID，请求是否完成仍待确认"
            else:
                detail = "无法回读确认写入结果"
            raise DanmuSubmissionError(f"写入弹幕关联请求异常，{detail}：{err}", uncertain=True, cause=err) from err

    def request_danmu(self, item_id: str) -> None:
        # Used when the correct ID already exists and no metadata change is necessary.
        try:
            response = self._get_json(f"api/danmu/{item_id}/refresh", retry=False)
        except Exception as err:
            status = getattr(getattr(err, "response", None), "status_code", None)
            rejected = isinstance(status, int) and 400 <= status < 500 and status != 408
            raise DanmuSubmissionError(f"单项弹幕刷新请求失败：{err}", uncertain=not rejected, cause=err) from err
        if isinstance(response, dict) and response.get("success") is False:
            raise DanmuSubmissionError("Danmu 插件未接受单项刷新请求", uncertain=False)
        if not isinstance(response, dict) or response.get("success") is not True:
            raise DanmuSubmissionError("Danmu 插件未返回有效的单项刷新队列确认", uncertain=True)
