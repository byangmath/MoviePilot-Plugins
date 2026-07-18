from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import EpisodeItem, OperationResult


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
    def __init__(self, logger: Any, dry_run: bool = True):
        self.logger = logger
        self.dry_run = dry_run
        self._transfer_history_cls = _first_import([
            ("app.db.models.transferhistory", "TransferHistory"),
        ])
        self._file_item_cls = _first_import([
            ("app.schemas", "FileItem"),
            ("app.schemas.file", "FileItem"),
        ])
        self._media_type_cls = _first_import([
            ("app.schemas", "MediaType"),
            ("app.schemas.types", "MediaType"),
        ])
        self._episode_format_cls = _first_import([
            ("app.schemas", "EpisodeFormat"),
            ("app.schemas.transfer", "EpisodeFormat"),
        ])
        self._transfer_chain_cls = _first_import([
            ("app.chain.transfer", "TransferChain"),
            ("app.chain", "TransferChain"),
        ])
        self._storage_chain_cls = _first_import([
            ("app.chain.storage", "StorageChain"),
            ("app.chain", "StorageChain"),
        ])
        self._get_db = _first_import([
            ("app.db", "get_db"),
        ])

    def reorganize(self, episode: EpisodeItem, path: Path, skip_same_name: bool = True) -> OperationResult:
        if not self._available():
            return OperationResult(
                success=False,
                message="当前 MoviePilot 未找到兼容的历史整理接口，已安全跳过",
                source=path,
            )

        history = self._find_history(path)
        if not history:
            return OperationResult(
                success=False,
                skipped=True,
                message="未找到对应的媒体整理记录",
                source=path,
            )

        if self.dry_run:
            return OperationResult(
                success=True,
                message=f"试运行：已匹配整理记录 #{getattr(history, 'id', '')}，未执行重新整理",
                source=path,
                target=self._history_target(history),
            )

        try:
            state, message = self._manual_transfer_from_history(history)
            if not state:
                return OperationResult(
                    success=False,
                    message=f"历史记录重新整理失败：{message}",
                    source=path,
                    target=self._history_target(history),
                )
            return OperationResult(
                success=True,
                message=f"已按整理记录 #{getattr(history, 'id', '')} 重新整理",
                source=path,
                target=self._history_target(history),
            )
        except Exception as err:
            return OperationResult(
                success=False,
                message=f"历史记录重新整理接口调用失败：{err}",
                source=path,
                target=self._history_target(history),
            )

    def _available(self) -> bool:
        return all([
            self._transfer_history_cls,
            self._file_item_cls,
            self._media_type_cls,
            self._episode_format_cls,
            self._transfer_chain_cls,
            self._storage_chain_cls,
            self._get_db,
        ])

    def _find_history(self, path: Path) -> Any | None:
        dest = path.as_posix()
        with self._db_session() as db:
            history = self._transfer_history_cls.get_by_dest(db, dest)
            if history:
                return history
            history = self._transfer_history_cls.get_by_src(db, dest)
            if history:
                return history
            candidates = self._transfer_history_cls.list_by_title(db, dest, count=1, status=True, wildcard=False)
            return candidates[0] if candidates else None

    def _manual_transfer_from_history(self, history: Any) -> tuple[bool, Any]:
        if getattr(history, "status", False) and "move" in (getattr(history, "mode", "") or ""):
            fileitem_data = getattr(history, "dest_fileitem", None) or {}
        else:
            fileitem_data = getattr(history, "src_fileitem", None) or {}
        if not fileitem_data:
            raise RuntimeError("整理记录缺少文件项")

        dest_fileitem = None
        if getattr(history, "dest_fileitem", None):
            dest_fileitem = self._file_item_cls(**history.dest_fileitem)
            deleted = self._storage_chain_cls().delete_media_file(dest_fileitem)
            if not deleted:
                return False, f"{dest_fileitem.path} 删除失败"

        fileitem = self._file_item_cls(**fileitem_data)
        mtype = self._media_type(getattr(history, "type", None))
        epformat = self._episode_format(history)

        return self._transfer_chain_cls().manual_transfer(
            fileitem=fileitem,
            tmdbid=int(history.tmdbid) if getattr(history, "tmdbid", None) else None,
            doubanid=str(history.doubanid) if getattr(history, "doubanid", None) else None,
            mtype=mtype,
            season=self._season_number(getattr(history, "seasons", None)),
            episode_group=getattr(history, "episode_group", None),
            transfer_type=getattr(history, "mode", None),
            epformat=epformat,
            scrape=True,
            force=True,
            background=False,
            downloader=getattr(history, "downloader", None),
            download_hash=getattr(history, "download_hash", None),
            preview=False,
            sync_extra_files=True,
        )

    def _media_type(self, type_name: str | None) -> Any | None:
        if not type_name:
            return None
        try:
            return self._media_type_cls(type_name)
        except Exception:
            return None

    def _episode_format(self, history: Any) -> Any | None:
        episodes = getattr(history, "episodes", None)
        if not episodes:
            return None
        return self._episode_format_cls(
            detail=self._episode_detail(str(episodes)),
        )

    @staticmethod
    def _episode_detail(episodes: str) -> str:
        if "-" not in episodes:
            return episodes.replace("E", "")
        start, end = episodes.split("-", 1)
        start_number = int(start.replace("E", ""))
        end_number = int(end.replace("E", ""))
        return ",".join(str(index) for index in range(start_number, end_number + 1))

    @staticmethod
    def _season_number(season: str | None) -> int | None:
        if not season:
            return None
        try:
            return int(str(season).replace("S", ""))
        except ValueError:
            return None

    @staticmethod
    def _history_target(history: Any) -> Path | None:
        dest = getattr(history, "dest", None)
        if dest:
            return Path(dest)
        dest_fileitem = getattr(history, "dest_fileitem", None) or {}
        if isinstance(dest_fileitem, dict) and dest_fileitem.get("path"):
            return Path(dest_fileitem["path"])
        return None

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
