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
        self._transfer_history_cls = _first_import([
            ("app.db.models.transferhistory", "TransferHistory"),
        ])
        self._manual_transfer_item_cls = _first_import([
            ("app.schemas", "ManualTransferItem"),
            ("app.schemas.transfer", "ManualTransferItem"),
        ])
        self._manual_transfer_endpoint = _first_import([
            ("app.api.endpoints.transfer", "manual_transfer"),
        ])
        self._get_db = _first_import([
            ("app.db", "get_db"),
        ])
        self._related_histories: dict[str, list[Any]] = {}

    def recent_histories(self, days: int) -> list[Any]:
        """Return the latest successful TV transfer record for each episode."""
        if not self._history_available():
            raise RuntimeError("当前 MoviePilot 未找到兼容的历史重新整理接口")

        cutoff = (datetime.now() - timedelta(days=max(int(days), 0))).strftime("%Y-%m-%d %H:%M:%S")
        history_cls = self._transfer_history_cls
        with self._db_session() as db:
            query = db.query(history_cls).filter(history_cls.date >= cutoff)
            if getattr(history_cls, "status", None) is not None:
                query = query.filter(history_cls.status.is_(True))
            if getattr(history_cls, "seasons", None) is not None:
                query = query.filter(history_cls.seasons.isnot(None))
            if getattr(history_cls, "episodes", None) is not None:
                query = query.filter(history_cls.episodes.isnot(None))
            if getattr(history_cls, "date", None) is not None:
                query = query.order_by(history_cls.date.desc())
            if getattr(history_cls, "id", None) is not None:
                query = query.order_by(history_cls.id.desc())
            candidates = query.all()

        return self._select_primary_histories(candidates)

    def _select_primary_histories(self, candidates: list[Any]) -> list[Any]:
        grouped: dict[tuple[str, str, str], list[Any]] = {}
        for history in candidates:
            grouped.setdefault(self._episode_key(history), []).append(history)

        histories: list[Any] = []
        self._related_histories = {}
        for episode_histories in grouped.values():
            primary = next(
                (history for history in episode_histories if self._is_video_history(history)),
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
        """Return attachment records that MoviePilot will sync with the selected video."""
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
                message="当前 MoviePilot 版本不支持整理预览，无法安全判断最新剧集标题",
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
                message="整理预览未返回目标文件，无法安全判断最新剧集标题",
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
        title = str(getattr(history, "title", None) or "未知剧集")
        season = str(getattr(history, "seasons", None) or "S??")
        episode = str(getattr(history, "episodes", None) or "E??")
        return f"{title} {season}{episode}"

    @staticmethod
    def target_path(history: Any) -> Path | None:
        return MoviePilotReorganizer._history_target(history)

    @classmethod
    def episode_target(cls, history: Any) -> EpisodeTarget | None:
        path = cls._history_target(history)
        if not path:
            return None
        return EpisodeTarget(path=str(path))

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
        return all([
            self._transfer_history_cls,
            self._get_db,
        ])

    def _reorganize_available(self) -> bool:
        return all([
            self._transfer_history_cls,
            self._manual_transfer_item_cls,
            self._manual_transfer_endpoint,
            self._get_db,
        ])

    @staticmethod
    def _episode_key(history: Any) -> tuple[str, str, str]:
        media_id = (
            getattr(history, "tmdbid", None)
            or getattr(history, "doubanid", None)
            or f"{getattr(history, 'title', '')}:{getattr(history, 'year', '')}"
        )
        return (
            str(media_id),
            str(getattr(history, "seasons", None) or ""),
            str(getattr(history, "episodes", None) or ""),
        )

    def _supports_preview(self) -> bool:
        fields = getattr(self._manual_transfer_item_cls, "model_fields", None)
        if fields is None:
            fields = getattr(self._manual_transfer_item_cls, "__fields__", {})
        return "preview" in fields

    def _call_manual_transfer(self, history: Any, preview: bool) -> Any:
        request = self._manual_transfer_request(history, preview)
        endpoint = self._manual_transfer_endpoint
        parameters = inspect.signature(endpoint).parameters
        if not parameters:
            raise RuntimeError("MoviePilot 重新整理接口参数不兼容")

        request_parameter = next(iter(parameters))
        kwargs: dict[str, Any] = {request_parameter: request}
        with self._db_session() as db:
            if "background" in parameters:
                kwargs["background"] = False
            if "db" in parameters:
                kwargs["db"] = db
            for name, parameter in parameters.items():
                if name in kwargs:
                    continue
                if parameter.default is inspect.Parameter.empty:
                    kwargs[name] = None
            response = endpoint(**kwargs)

        if inspect.isawaitable(response):
            raise RuntimeError("MoviePilot 重新整理接口变为异步接口，当前版本暂不兼容")
        return response

    def _manual_transfer_request(self, history: Any, preview: bool) -> Any:
        fields = getattr(self._manual_transfer_item_cls, "model_fields", None)
        if fields is None:
            fields = getattr(self._manual_transfer_item_cls, "__fields__", {})

        values = {
            "logid": int(history.id),
            "from_history": True,
            "transfer_type": getattr(history, "mode", None),
            "target_storage": getattr(history, "dest_storage", None),
            "scrape": True,
            "preview": preview,
        }
        if fields:
            values = {key: value for key, value in values.items() if key in fields}
        return self._manual_transfer_item_cls(**values)

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

    def _db_session(self):
        class _SessionContext:
            def __init__(self, get_db):
                self._get_db = get_db
                self._generator = None
                self.session = None

            def __enter__(self):
                self._generator = self._get_db()
                self.session = next(self._generator)
                return self.session

            def __exit__(self, exc_type, exc, tb):
                if self.session:
                    try:
                        self.session.close()
                    except Exception:
                        pass
                if self._generator:
                    try:
                        self._generator.close()
                    except Exception:
                        pass

        return _SessionContext(self._get_db)
