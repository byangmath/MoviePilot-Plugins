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
    """Thin adapter around MoviePilot internals.

    This class deliberately avoids raw renames. If the current MoviePilot build
    does not expose a compatible transfer entrypoint, the episode is failed
    safely and the original file is left untouched.
    """

    def __init__(self, logger: Any, dry_run: bool = True, transfer_type: str = "move"):
        self.logger = logger
        self.dry_run = dry_run
        self.transfer_type = transfer_type or "move"

    def reorganize(self, episode: EpisodeItem, path: Path, skip_same_name: bool = True) -> OperationResult:
        if not path.exists():
            return OperationResult(success=False, message="映射后文件不存在", source=path)

        metainfo_cls = _first_import([
            ("app.core.metainfo", "MetaInfoPath"),
            ("app.core.meta", "MetaInfoPath"),
            ("app.schemas.types", "MetaInfoPath"),
        ])
        media_chain_cls = _first_import([
            ("app.chain.media", "MediaChain"),
            ("app.chain", "MediaChain"),
        ])
        transfer_chain_cls = _first_import([
            ("app.chain.transfer", "TransferChain"),
            ("app.chain", "TransferChain"),
        ])

        if not metainfo_cls or not media_chain_cls or not transfer_chain_cls:
            return OperationResult(
                success=False,
                message="当前 MoviePilot 未找到兼容的识别/整理接口，已安全跳过实际整理",
                source=path,
            )

        try:
            meta = metainfo_cls(path)
            media_chain = media_chain_cls()
            transfer_chain = transfer_chain_cls()
            mediainfo = self._recognize(media_chain, meta, path)
            if not mediainfo:
                return OperationResult(success=False, message="MoviePilot 媒体识别失败", source=path)

            preview_path = self._preview_target(transfer_chain, path, meta, mediainfo)
            if preview_path and skip_same_name and Path(preview_path) == path:
                return OperationResult(success=False, skipped=True, message="文件名无需更新", source=path, target=path)
            if preview_path and Path(preview_path).exists() and Path(preview_path) != path:
                return OperationResult(success=False, skipped=True, message="目标文件已存在", source=path, target=Path(preview_path))
            if self.dry_run:
                return OperationResult(
                    success=True,
                    message="试运行：已完成识别，未执行整理",
                    source=path,
                    target=Path(preview_path) if preview_path else None,
                )

            transfer_result = self._transfer(transfer_chain, path, meta, mediainfo)
            target = self._extract_target(transfer_result)
            return OperationResult(
                success=True,
                message="整理完成",
                source=path,
                target=target,
            )
        except Exception as err:
            return OperationResult(success=False, message=f"MoviePilot 整理接口调用失败：{err}", source=path)

    @staticmethod
    def _recognize(media_chain: Any, meta: Any, path: Path) -> Any | None:
        for method_name, kwargs in [
            ("recognize_media", {"meta": meta}),
            ("recognize_media", {"meta": meta, "path": path}),
            ("recognize_by_meta", {"meta": meta}),
            ("recognize_by_path", {"path": path}),
        ]:
            method = getattr(media_chain, method_name, None)
            if not method:
                continue
            try:
                return method(**kwargs)
            except TypeError:
                continue
        return None

    @staticmethod
    def _preview_target(transfer_chain: Any, path: Path, meta: Any, mediainfo: Any) -> Path | None:
        for method_name, kwargs in [
            ("preview", {"path": path, "meta": meta, "mediainfo": mediainfo}),
            ("preview_transfer", {"path": path, "meta": meta, "mediainfo": mediainfo}),
            ("transfer_preview", {"path": path, "meta": meta, "mediainfo": mediainfo}),
        ]:
            method = getattr(transfer_chain, method_name, None)
            if not method:
                continue
            try:
                result = method(**kwargs)
                return MoviePilotReorganizer._extract_target(result)
            except TypeError:
                continue
        return None

    def _transfer(self, transfer_chain: Any, path: Path, meta: Any, mediainfo: Any) -> Any:
        method = getattr(transfer_chain, "transfer", None)
        if not method:
            raise RuntimeError("TransferChain.transfer 不存在")
        attempts = [
            {"path": path, "meta": meta, "mediainfo": mediainfo, "transfer_type": self.transfer_type},
            {"path": path, "meta": meta, "mediainfo": mediainfo},
            {"in_path": path, "meta": meta, "mediainfo": mediainfo, "transfer_type": self.transfer_type},
        ]
        last_error: Exception | None = None
        for kwargs in attempts:
            try:
                return method(**kwargs)
            except TypeError as err:
                last_error = err
                continue
        if last_error:
            raise last_error
        raise RuntimeError("未能调用整理接口")

    @staticmethod
    def _extract_target(result: Any) -> Path | None:
        if result is None:
            return None
        if isinstance(result, (str, Path)):
            return Path(result)
        for attr in ("target_path", "target", "dest", "dest_path", "path"):
            value = getattr(result, attr, None)
            if value:
                return Path(value)
        if isinstance(result, dict):
            for key in ("target_path", "target", "dest", "dest_path", "path"):
                if result.get(key):
                    return Path(result[key])
        return None
