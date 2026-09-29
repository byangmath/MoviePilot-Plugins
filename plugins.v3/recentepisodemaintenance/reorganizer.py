from __future__ import annotations

from datetime import datetime, timedelta
import inspect
from pathlib import Path
from typing import Any

from .models import EpisodeTarget, OperationResult


def _first_import(candidates: list[tuple[str, str]]) -> Any | None:
    import importlib

    for module_name, attr_name in candidates:
        try:
            module = importlib.import_module(module_name)
            return getattr(module, attr_name)
        except Exception:
            continue
    return None


class MoviePilotReorganizer:
    _VIDEO_EXTENSIONS = {
        ".3gp", ".asf", ".avi", ".divx", ".flv", ".iso", ".m2ts", ".m4v",
        ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".rm", ".rmvb",
        ".strm", ".ts", ".vob", ".webm", ".wmv",
    }

    def __init__(self, logger: Any, dry_run: bool = True):
        self.logger = logger
        self.dry_run = dry_run
        self._list_transfer_history = _first_import([
            ("app.sdk.queries", "list_transfer_history"),
        ])
        self._transfer_chain_cls = _first_import([
            ("app.chain.transfer", "TransferChain"),
        ])
        self._file_item_cls = _first_import([
            ("app.schemas", "FileItem"),
            ("app.schemas.file", "FileItem"),
        ])
        self._episode_format_cls = _first_import([
            ("app.schemas", "EpisodeFormat"),
            ("app.schemas.transfer", "EpisodeFormat"),
        ])
        self._media_type_cls = _first_import([
            ("app.schemas.types", "MediaType"),
        ])
        self._related_histories: dict[str, list[Any]] = {}

    def recent_histories(
        self,
        days: int,
        tracked_history_ids: set[int] | None = None,
        preferred_history_ids: set[int] | None = None,
    ) -> list[Any]:
        """Return recent records plus unfinished records already admitted to the queue."""
        if not self._history_available():
            raise RuntimeError("当前 MoviePilot 未找到兼容的历史重新整理接口")

        cutoff = datetime.now() - timedelta(days=max(int(days), 0))
        tracked_ids = {
            int(history_id)
            for history_id in (tracked_history_ids or set())
            if history_id
        }
        candidates: list[Any] = []
        seen_ids: set[int] = set()

        page_number = 1
        while True:
            page = self._list_transfer_history(
                filters={
                    "status": True,
                    "media_types": ("电影", "电视剧"),
                    "require_media_identity": True,
                },
                page={
                    "page": page_number,
                    "count": 200,
                    "sort": {"field": "date", "direction": "desc"},
                },
            )
            items = self._page_items(page)
            parsed_dates = []
            for history in items:
                parsed_date = self._history_date(history)
                parsed_dates.append(parsed_date)
                if parsed_date is not None and parsed_date < cutoff:
                    continue
                self._append_history(candidates, seen_ids, history)

            if not self._page_has_next(page):
                break
            if items and all(
                parsed_date is not None and parsed_date < cutoff
                for parsed_date in parsed_dates
            ):
                break
            page_number += 1

        missing_tracked_ids = tracked_ids - seen_ids
        for start in range(0, len(missing_tracked_ids), 200):
            history_ids = tuple(sorted(missing_tracked_ids)[start:start + 200])
            if not history_ids:
                continue
            page = self._list_transfer_history(
                filters={
                    "ids": history_ids,
                    "status": True,
                    "media_types": ("电影", "电视剧"),
                    "require_media_identity": True,
                },
                page={
                    "page": 1,
                    "count": len(history_ids),
                    "sort": {"field": "date", "direction": "desc"},
                },
            )
            for history in self._page_items(page):
                self._append_history(candidates, seen_ids, history)

        return self._select_primary_histories(
            candidates,
            preferred_history_ids=preferred_history_ids,
        )

    def _select_primary_histories(
        self,
        candidates: list[Any],
        preferred_history_ids: set[int] | None = None,
    ) -> list[Any]:
        grouped: dict[tuple[str, str, str], list[Any]] = {}
        for history in candidates:
            if self.media_type(history) not in {"movie", "tv"}:
                continue
            grouped.setdefault(self._episode_key(history), []).append(history)

        preferred_ids = {
            int(history_id)
            for history_id in (preferred_history_ids or set())
            if history_id
        }
        histories: list[Any] = []
        self._related_histories = {}
        for episode_histories in grouped.values():
            video_histories = [
                history
                for history in episode_histories
                if self._is_video_history(history)
            ]
            primary = next(
                (
                    history
                    for history in video_histories
                    if int(getattr(history, "id", None) or 0) in preferred_ids
                ),
                None,
            ) or next(
                iter(video_histories),
                None,
            )
            if primary is None:
                continue
            histories.append(primary)
            self._related_histories[self.processing_key(primary)] = [
                history
                for history in episode_histories
                if history is not primary
                and not self._is_video_history(history)
                and self._same_transfer(primary, history)
            ]

        histories.sort(
            key=lambda history: (
                str(getattr(history, "date", None) or ""),
                int(getattr(history, "id", None) or 0),
            ),
            reverse=True,
        )
        return histories

    def related_history_count(self, history: Any) -> int:
        """Return related attachment histories detected for the same transfer."""
        return len(self._related_histories.get(self.processing_key(history)) or [])

    def reorganize(
        self,
        history: Any,
        skip_same_name: bool = True,
        preview_only: bool = False,
    ) -> OperationResult:
        if not self._reorganize_available():
            return OperationResult(
                success=False,
                message="当前 MoviePilot 未找到兼容的历史重新整理接口，已安全跳过",
            )

        history_id = getattr(history, "id", None)
        source = self._history_source(history)
        current_target = self._history_target(history)
        preview_target = None

        if self._supports_preview():
            try:
                preview_response = self._call_manual_transfer(history, preview=True)
            except Exception as err:
                return OperationResult(
                    success=False,
                    message=f"重新整理预览失败，未修改文件：{err}",
                    source=source,
                    target=current_target,
                )

            if not self._response_success(preview_response):
                return OperationResult(
                    success=False,
                    message=f"重新整理预览失败，未修改文件：{self._response_message(preview_response)}",
                    source=source,
                    target=current_target,
                )

            preview_target = self._preview_target(preview_response)
            if skip_same_name and preview_target and self._same_path(current_target, preview_target):
                return OperationResult(
                    success=False,
                    skipped=True,
                    message="按当前命名规则预览，路径未变化",
                    source=source,
                    target=current_target,
                )

        if self.dry_run or preview_only:
            if preview_target:
                message = f"试运行：整理记录 #{history_id} 预计重新命名"
            else:
                message = f"试运行：已匹配整理记录 #{history_id}，未修改文件"
            return OperationResult(
                success=True,
                message=message,
                source=source,
                target=preview_target or current_target,
            )

        try:
            response = self._call_manual_transfer(history, preview=False)
        except Exception as err:
            return OperationResult(
                success=False,
                message=f"MoviePilot 历史记录重新整理接口调用失败：{err}",
                source=source,
                target=preview_target or current_target,
            )

        if not self._response_success(response):
            return OperationResult(
                success=False,
                message=f"MoviePilot 历史记录重新整理失败：{self._response_message(response)}",
                source=source,
                target=preview_target or current_target,
            )

        return OperationResult(
            success=True,
            message=f"已按 MoviePilot 整理记录 #{history_id} 重新整理",
            source=source,
            target=preview_target or current_target,
        )

    def preview(self, history: Any) -> OperationResult:
        """Preview the current MoviePilot destination without changing files."""
        source = self._history_source(history)
        current_target = self._history_target(history)
        if not self._reorganize_available():
            return OperationResult(
                success=False,
                message="当前 MoviePilot 未找到兼容的整理预览接口",
                source=source,
                target=current_target,
            )
        if not self._supports_preview():
            return OperationResult(
                success=False,
                message="当前 MoviePilot 版本不支持整理预览，无法安全判断最新媒体标题",
                source=source,
                target=current_target,
            )

        try:
            response = self._call_manual_transfer(history, preview=True)
        except Exception as err:
            return OperationResult(
                success=False,
                message=f"整理预览失败，未修改文件：{err}",
                source=source,
                target=current_target,
            )

        if not self._response_success(response):
            return OperationResult(
                success=False,
                message=f"整理预览失败，未修改文件：{self._response_message(response)}",
                source=source,
                target=current_target,
            )

        preview_target = self._preview_target(response)
        if not preview_target:
            return OperationResult(
                success=False,
                message="整理预览未返回目标文件，无法安全判断最新媒体标题",
                source=source,
                target=current_target,
            )

        return OperationResult(
            success=True,
            message="已获取 MoviePilot 当前命名规则的整理预览",
            source=source,
            target=preview_target,
        )

    @staticmethod
    def display_name(history: Any) -> str:
        media_type = MoviePilotReorganizer.media_type(history)
        title = str(
            getattr(history, "title", None)
            or ("未知电影" if media_type == "movie" else "未知剧集")
        )
        if media_type == "movie":
            year = str(getattr(history, "year", None) or "").strip()
            return f"{title} ({year})" if year else title
        season = str(getattr(history, "seasons", None) or "S??")
        episode = str(getattr(history, "episodes", None) or "E??")
        return f"{title} {season}{episode}"

    @staticmethod
    def target_path(history: Any) -> Path | None:
        return MoviePilotReorganizer._history_target(history)

    @classmethod
    def episode_target(cls, history: Any) -> EpisodeTarget | None:
        return cls.media_target(history)

    @classmethod
    def media_target(cls, history: Any) -> EpisodeTarget | None:
        path = cls._history_target(history)
        if not path:
            return None
        media_type = cls.media_type(history)
        if media_type not in {"movie", "tv"}:
            return None
        return EpisodeTarget(path=str(path), media_type=media_type)

    @staticmethod
    def media_type(history: Any) -> str:
        raw_type = getattr(history, "type", None)
        value = getattr(raw_type, "value", raw_type)
        normalized = str(value or "").strip().casefold()
        if normalized in {"电影", "movie", "mediatype.movie"}:
            return "movie"
        if normalized in {"电视剧", "tv", "television", "mediatype.tv"}:
            return "tv"
        if (
            str(getattr(history, "seasons", None) or "").strip()
            and str(getattr(history, "episodes", None) or "").strip()
        ):
            return "tv"
        return "unknown"

    @classmethod
    def processing_key(cls, history: Any) -> str:
        """Identify one organized episode while keeping replacements independent."""
        media_id, season, episode = cls._episode_key(history)
        transfer_identity = (
            getattr(history, "download_hash", None)
            or getattr(history, "src", None)
            or f"history:{getattr(history, 'id', '')}"
        )
        return "|".join((media_id, season, episode, str(transfer_identity)))

    def _history_available(self) -> bool:
        return callable(self._list_transfer_history)

    def compatibility_error(self) -> str:
        if not self._history_available():
            return "当前 MoviePilot 未找到兼容的整理历史接口"
        if not self._reorganize_available():
            return "当前 MoviePilot 未找到兼容的历史重新整理接口"
        if not self._supports_preview():
            return "当前 MoviePilot 版本不支持整理预览"
        return ""

    def _reorganize_available(self) -> bool:
        return all([
            self._transfer_chain_cls,
            self._file_item_cls,
            self._episode_format_cls,
            self._media_type_cls,
        ])

    @staticmethod
    def _episode_key(history: Any) -> tuple[str, str, str]:
        media_source = getattr(history, "media_source", None)
        media_source = getattr(media_source, "value", media_source)
        media_id = getattr(history, "media_id", None)
        if media_source and media_id:
            identity = f"{media_source}:{media_id}"
        else:
            identity = f"{getattr(history, 'title', '')}:{getattr(history, 'year', '')}"
        return (
            str(identity),
            str(getattr(history, "seasons", None) or ""),
            str(getattr(history, "episodes", None) or ""),
        )

    def _supports_preview(self) -> bool:
        if not self._transfer_chain_cls:
            return False
        try:
            parameters = inspect.signature(
                self._transfer_chain_cls.manual_transfer
            ).parameters
        except (TypeError, ValueError, AttributeError):
            return False
        return "preview" in parameters

    def _call_manual_transfer(self, history: Any, preview: bool) -> Any:
        fileitem = self._history_fileitem(history)
        media_source = getattr(history, "media_source", None)
        media_id = getattr(history, "media_id", None)
        if not media_source or not media_id:
            media_source = None
            media_id = None

        media_type = self.media_type(history)
        mtype = (
            self._media_type_cls.MOVIE
            if media_type == "movie"
            else self._media_type_cls.TV
        )
        season = self._season_number(getattr(history, "seasons", None))
        episode_format = self._episode_format(history)
        state, result = self._transfer_chain_cls().manual_transfer(
            fileitem=fileitem,
            target_storage=getattr(history, "dest_storage", None),
            media_source=media_source,
            media_id=media_id,
            mtype=mtype,
            season=season,
            epformat=episode_format,
            episode_group=getattr(history, "episode_group", None),
            transfer_type=getattr(history, "mode", None),
            scrape=True,
            force=bool(getattr(history, "status", False)),
            background=False,
            downloader=getattr(history, "downloader", None),
            download_hash=getattr(history, "download_hash", None),
            preview=preview,
            sync_extra_files=True,
            reorganize=False,
        )
        data = result if isinstance(result, dict) else None
        message = result.get("message") if isinstance(result, dict) else result
        return {
            "success": bool(state),
            "message": str(message or ""),
            "data": data,
        }

    def _history_fileitem(self, history: Any) -> Any:
        """Rebuild the V3 FileItem used by the original successful transfer."""
        mode = str(getattr(history, "mode", None) or "")
        use_destination = bool(getattr(history, "status", False)) and "move" in mode
        if use_destination:
            data = getattr(history, "dest_fileitem", None)
            path = getattr(history, "dest", None)
            storage = getattr(history, "dest_storage", None)
        else:
            data = getattr(history, "src_fileitem", None)
            path = getattr(history, "src", None)
            storage = getattr(history, "src_storage", None)

        values = dict(data) if isinstance(data, dict) else {}
        if path:
            values.setdefault("path", str(path))
            values.setdefault("name", Path(str(path)).name)
        if storage:
            values.setdefault("storage", str(storage))
        values.setdefault("type", "file")
        if not values.get("path"):
            raise RuntimeError("整理历史缺少可用的源文件路径")
        return self._file_item_cls(**values)

    @staticmethod
    def _season_number(value: Any) -> int | None:
        """Convert an MP season label such as S01 to the V3 integer contract."""
        normalized = str(value or "").strip().upper()
        if normalized.startswith("S"):
            normalized = normalized[1:]
        try:
            return int(normalized) if normalized else None
        except ValueError:
            return None

    def _episode_format(self, history: Any) -> Any | None:
        """Restore the exact episode range carried by the V3 history snapshot."""
        value = str(getattr(history, "episodes", None) or "").strip().upper()
        if not value:
            return None
        if "-" in value:
            try:
                first, last = value.split("-", 1)
                start = int(first.removeprefix("E"))
                end = int(last.removeprefix("E"))
            except ValueError:
                return None
            if end < start:
                return None
            detail = ",".join(str(number) for number in range(start, end + 1))
        else:
            detail = value.removeprefix("E")
            if not detail.isdigit():
                return None
        return self._episode_format_cls(detail=detail)

    @staticmethod
    def _page_items(page: Any) -> list[Any]:
        """Read SDK page items while keeping test doubles lightweight."""
        if isinstance(page, dict):
            items = page.get("items") or []
        else:
            items = getattr(page, "items", None) or []
        return list(items)

    @staticmethod
    def _page_has_next(page: Any) -> bool:
        """Return whether a stable SDK query page has another page."""
        if isinstance(page, dict):
            if "has_next" in page:
                return bool(page.get("has_next"))
            page_number = int(page.get("page") or 1)
            count = int(page.get("count") or 0)
            total = int(page.get("total") or 0)
            return bool(count) and page_number * count < total
        return bool(getattr(page, "has_next", False))

    @staticmethod
    def _history_date(history: Any) -> datetime | None:
        """Parse an MP transfer timestamp for the configured recent window."""
        value = str(getattr(history, "date", None) or "").strip()
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed

    @staticmethod
    def _append_history(
        candidates: list[Any],
        seen_ids: set[int],
        history: Any,
    ) -> None:
        """Append one history snapshot without duplicating its stable ID."""
        try:
            history_id = int(getattr(history, "id", 0) or 0)
        except (TypeError, ValueError):
            history_id = 0
        if history_id and history_id in seen_ids:
            return
        candidates.append(history)
        if history_id:
            seen_ids.add(history_id)

    @staticmethod
    def _response_success(response: Any) -> bool:
        if isinstance(response, dict):
            return bool(response.get("success"))
        return bool(getattr(response, "success", False))

    @staticmethod
    def _response_message(response: Any) -> str:
        if isinstance(response, dict):
            message = response.get("message")
        else:
            message = getattr(response, "message", None)
        return str(message or "未知错误")

    @classmethod
    def _preview_target(cls, response: Any) -> Path | None:
        if isinstance(response, dict):
            data = response.get("data")
        else:
            data = getattr(response, "data", None)
        if not isinstance(data, dict):
            return None

        candidates = data.get("items") or []
        if isinstance(candidates, dict):
            candidates = [candidates]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            target = item.get("target") or item.get("target_path")
            if target:
                return Path(str(target))

        target = data.get("target") or data.get("target_path")
        return Path(str(target)) if target else None

    @staticmethod
    def _history_source(history: Any) -> Path | None:
        mode = str(getattr(history, "mode", None) or "")
        if bool(getattr(history, "status", False)) and "move" in mode:
            value = getattr(history, "dest", None)
        else:
            value = getattr(history, "src", None)
        return Path(value) if value else None

    @staticmethod
    def _history_target(history: Any) -> Path | None:
        dest = getattr(history, "dest", None)
        if dest:
            return Path(dest)
        dest_fileitem = getattr(history, "dest_fileitem", None) or {}
        if isinstance(dest_fileitem, dict) and dest_fileitem.get("path"):
            return Path(dest_fileitem["path"])
        return None

    @classmethod
    def _is_video_history(cls, history: Any) -> bool:
        target = cls._history_target(history)
        return bool(target and target.suffix.casefold() in cls._VIDEO_EXTENSIONS)

    @classmethod
    def _same_transfer(cls, primary: Any, candidate: Any) -> bool:
        primary_hash = str(getattr(primary, "download_hash", None) or "").strip()
        candidate_hash = str(getattr(candidate, "download_hash", None) or "").strip()
        if primary_hash and candidate_hash:
            return primary_hash == candidate_hash

        primary_source = cls._history_source(primary)
        candidate_source = cls._history_source(candidate)
        if primary_source and candidate_source:
            return cls._path_key(primary_source.parent) == cls._path_key(
                candidate_source.parent
            )

        primary_target = cls._history_target(primary)
        candidate_target = cls._history_target(candidate)
        return bool(
            primary_target
            and candidate_target
            and cls._path_key(primary_target.parent)
            == cls._path_key(candidate_target.parent)
        )

    @classmethod
    def _same_path(cls, left: Path | None, right: Path | None) -> bool:
        if not left or not right:
            return False
        return cls._path_key(left) == cls._path_key(right)

    @staticmethod
    def _path_key(path: Path | str | None) -> str:
        if path is None:
            return ""
        return str(path).strip().replace("\\", "/").rstrip("/")
