from __future__ import annotations

from typing import Any

try:
    from apscheduler.triggers.cron import CronTrigger
except Exception:
    CronTrigger = None

try:
    from app.log import logger
except Exception:
    import logging

    logger = logging.getLogger(__name__)

try:
    from app.plugins import _PluginBase
except Exception:
    class _PluginBase:  # type: ignore
        pass

try:
    from app.helper.mediaserver import MediaServerHelper
except Exception:
    MediaServerHelper = None

from .jellyfin_client import JellyfinServiceClient
from .models import RunResult
from .path_mapper import PathMapper
from .reorganizer import MoviePilotReorganizer


class RecentEpisodeMaintenance(_PluginBase):
    plugin_name = "最近剧集维护"
    plugin_desc = "定时刷新和重新整理最近发布的 Jellyfin 剧集"
    plugin_icon = "https://raw.githubusercontent.com/byangmath/RecentEpisodeMaintenance/main/icons/recentepisodemaintenance.png"
    plugin_version = "0.1.0"
    plugin_author = "byangmath"
    author_url = "https://github.com/byangmath"
    plugin_config_prefix = "recentepisodemaintenance_"
    plugin_order = 66
    auth_level = 1

    def init_plugin(self, config: dict[str, Any] | None = None):
        config = config or {}
        config_changed = False
        self._enabled = bool(config.get("enabled", False))
        self._cron = config.get("cron") or "0 4 * * *"
        self._days = int(config.get("days") or 7)
        self._max_items = int(config.get("max_items") or 20)
        self._dry_run = bool(config.get("dry_run", True))
        self._notify = bool(config.get("notify", False))
        self._enable_refresh = bool(config.get("enable_refresh", True))
        self._enable_reorganize = bool(config.get("enable_reorganize", False))
        self._scan_after_reorganize = bool(config.get("scan_after_reorganize", True))
        self._skip_same_name = bool(config.get("skip_same_name", True))
        self._transfer_type = "move"
        self._media_server_name = config.get("media_server_name") or self._first_jellyfin_service_name()
        library_ids_selection = self._library_ids_selection(config.get("library_ids"))
        self._library_ids = self._normalize_values(library_ids_selection)
        if config.get("library_ids") != library_ids_selection:
            config["library_ids"] = library_ids_selection
            config_changed = True
        self._path_mappings = config.get("path_mappings") or ""
        self._metadata_mode = config.get("metadata_mode") or "FullRefresh"
        self._image_mode = config.get("image_mode") or "Default"
        self._replace_metadata = bool(config.get("replace_metadata", False))
        self._replace_images = bool(config.get("replace_images", False))

        run_once = bool(config.get("run_once", False))
        if run_once:
            config["run_once"] = False
            config_changed = True

        if config_changed:
            self.__update_config(config)

        if run_once:
            self.run_once()

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> list[dict[str, Any]]:
        if not self._enabled or not self._cron or CronTrigger is None:
            return []
        return [{
            "id": "RecentEpisodeMaintenance",
            "name": "最近剧集维护",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.run_once,
            "kwargs": {},
        }]

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        return []

    def get_api(self) -> list[dict[str, Any]]:
        pass

    def get_form(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        jellyfin_services = self._jellyfin_service_options()
        library_options = self._library_options()
        default_media_server = jellyfin_services[0]["value"] if jellyfin_services else ""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self._switch("enabled", "启用插件", 6),
                            self._switch("run_once", "立即运行一次", 6),
                            self._text("cron", "执行周期", "APScheduler Cron，如 0 4 * * *", 6),
                            self._number("days", "最近 N 天", 6),
                            self._number("max_items", "单次最大处理数量", 6),
                            self._switch("dry_run", "试运行模式", 6),
                            self._switch("notify", "运行完成后发送通知", 6),
                            self._switch("enable_refresh", "刷新媒体库元数据", 6),
                            self._switch("enable_reorganize", "重新整理最近剧集文件", 6),
                            self._switch("scan_after_reorganize", "整理后扫描媒体库", 6),
                            self._switch("skip_same_name", "跳过名称未变化的文件", 6),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._select(
                                "media_server_name",
                                "媒体服务器",
                                jellyfin_services,
                                12,
                                clearable=False,
                                hint="用于查询和刷新最近发布剧集",
                            ),
                            self._select(
                                "library_ids",
                                "维护媒体库",
                                library_options,
                                12,
                                multiple=True,
                                hint="选择全部时处理所有剧集库",
                            ),
                            self._textarea("path_mappings", "路径映射", "/media/动画TV => /media\n/media/电视剧 => /tv", 12),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._text("metadata_mode", "元数据刷新模式", "默认 FullRefresh", 6),
                            self._text("image_mode", "图片刷新模式", "默认 Default；不刷新图片可保持 Default", 6),
                            self._switch("replace_metadata", "覆盖已有元数据", 6),
                            self._switch("replace_images", "覆盖已有图片", 6),
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "run_once": False,
            "cron": "0 4 * * *",
            "days": 7,
            "max_items": 20,
            "dry_run": True,
            "notify": False,
            "enable_refresh": True,
            "enable_reorganize": False,
            "scan_after_reorganize": True,
            "skip_same_name": True,
            "media_server_name": self._media_server_name or default_media_server,
            "library_ids": self._library_ids_selection(self._library_ids),
            "path_mappings": "",
            "metadata_mode": "FullRefresh",
            "image_mode": "Default",
            "replace_metadata": False,
            "replace_images": False,
        }

    def stop_service(self):
        pass

    def get_page(self) -> list[dict[str, Any]]:
        pass

    def run_once(self):
        if not self._enable_refresh and not self._enable_reorganize:
            logger.warning("[最近剧集维护] 未启用任何功能")
            return

        client = self._get_jellyfin_client()
        if not client:
            return

        result = RunResult()
        try:
            episodes = client.recent_episodes(days=self._days, library_ids=self._library_ids)
            episodes = episodes[:max(self._max_items, 1)]
            result.total = len(episodes)
            logger.info(f"[最近剧集维护] 查询最近 {self._days} 天剧集，共 {result.total} 集")
        except Exception as err:
            logger.error(f"[最近剧集维护] 查询媒体服务器失败：{err}")
            return

        mapper = PathMapper(self._path_mappings)
        reorganizer = MoviePilotReorganizer(
            logger=logger,
            dry_run=self._dry_run,
            transfer_type=self._transfer_type,
        )
        any_reorganized = False

        for episode in episodes:
            try:
                if self._enable_reorganize:
                    mapped_path = mapper.map(episode.path)
                    if not mapped_path:
                        result.skipped += 1
                        logger.warning(f"[最近剧集维护] 跳过 {episode.display_name}：路径未匹配 {episode.path}")
                    else:
                        reorganize_result = reorganizer.reorganize(
                            episode=episode,
                            path=mapped_path,
                            skip_same_name=self._skip_same_name,
                        )
                        if reorganize_result.success:
                            result.reorganized += 1
                            any_reorganized = True
                            logger.info(f"[最近剧集维护] 整理成功 {episode.display_name}：{reorganize_result.message}")
                        elif reorganize_result.skipped:
                            result.skipped += 1
                            logger.info(f"[最近剧集维护] 跳过 {episode.display_name}：{reorganize_result.message}")
                        else:
                            result.add_error(f"{episode.display_name}：{reorganize_result.message}")
                            logger.error(f"[最近剧集维护] 整理失败 {episode.display_name}：{reorganize_result.message}")
                            continue

                if self._enable_refresh:
                    client.refresh_episode(
                        item_id=episode.item_id,
                        metadata_mode=self._metadata_mode,
                        image_mode=self._image_mode,
                        replace_metadata=self._replace_metadata,
                        replace_images=self._replace_images,
                    )
                    result.refreshed += 1
                    logger.info(f"[最近剧集维护] 元数据刷新成功 {episode.display_name}")
            except Exception as err:
                result.add_error(f"{episode.display_name}：{err}")
                logger.error(f"[最近剧集维护] 处理失败 {episode.display_name}：{err}")

        if any_reorganized and self._scan_after_reorganize and not self._dry_run:
            try:
                client.scan_library()
                logger.info("[最近剧集维护] 已触发媒体库扫描")
            except Exception as err:
                result.add_error(f"媒体库扫描失败：{err}")

        logger.info("[最近剧集维护] 运行完成\n" + result.summary())
        if self._notify:
            self._post_message("最近剧集维护完成", result.summary())

    def _post_message(self, title: str, text: str) -> None:
        post_message = getattr(self, "post_message", None)
        if callable(post_message):
            try:
                post_message(title=title, text=text)
            except TypeError:
                post_message(mtype=None, title=title, text=text)

    def _get_jellyfin_client(self) -> JellyfinServiceClient | None:
        service_items = self._jellyfin_services()
        if service_items is None:
            return None

        if not service_items:
            logger.error("[最近剧集维护] 未找到已配置的 Jellyfin 媒体服务器")
            return None

        selected_service = None
        selected_name = ""
        expected_name = self._media_server_name.strip()
        for name, service in service_items:
            candidate_name = self._service_name(service, str(name))
            if not expected_name or candidate_name == expected_name or str(name) == expected_name:
                selected_service = service
                selected_name = candidate_name
                break

        if not selected_service:
            available = "、".join(self._service_name(service, str(name)) for name, service in service_items)
            logger.error(f"[最近剧集维护] 未找到 Jellyfin 服务：{expected_name}；可用服务：{available}")
            return None

        instance = getattr(selected_service, "instance", selected_service)
        if not instance or not hasattr(instance, "get_data") or not hasattr(instance, "post_data"):
            logger.error(f"[最近剧集维护] Jellyfin 服务不可用：{selected_name}")
            return None

        logger.info(f"[最近剧集维护] 使用 Jellyfin 服务：{selected_name}")
        return JellyfinServiceClient(instance)

    def _jellyfin_services(self, quiet: bool = False) -> list[tuple[str, Any]] | None:
        if MediaServerHelper is None:
            if not quiet:
                logger.error("[最近剧集维护] 当前环境无法读取 MoviePilot 媒体服务器配置")
            return None

        try:
            services = MediaServerHelper().get_services(type_filter="jellyfin") or {}
        except Exception as err:
            if not quiet:
                logger.error(f"[最近剧集维护] 读取 Jellyfin 服务失败：{err}")
            return None

        return list(services.items()) if isinstance(services, dict) else [
            (self._service_name(service, str(index)), service)
            for index, service in enumerate(services)
        ]

    def _jellyfin_service_options(self) -> list[dict[str, str]]:
        options: list[dict[str, str]] = []
        service_items = self._jellyfin_services(quiet=True) or []
        for name, service in service_items:
            service_name = self._service_name(service, str(name))
            options.append({"title": service_name, "value": service_name})
        return options

    def _first_jellyfin_service_name(self) -> str:
        service_items = self._jellyfin_services(quiet=True) or []
        if not service_items:
            return ""
        name, service = service_items[0]
        return self._service_name(service, str(name))

    def _library_options(self) -> list[dict[str, str]]:
        options = [{"title": "全部", "value": "__all__"}]
        client = self._get_jellyfin_client()
        if not client:
            return options
        try:
            return options + client.libraries()
        except Exception as err:
            logger.warning(f"[最近剧集维护] 读取 Jellyfin 媒体库列表失败：{err}")
            return options

    @staticmethod
    def _service_name(service: Any, fallback: str) -> str:
        for obj in (service, getattr(service, "instance", None), getattr(service, "config", None)):
            if not obj:
                continue
            if isinstance(obj, dict):
                for key in ("name", "server_name", "server", "id"):
                    value = obj.get(key)
                    if value:
                        return str(value)
                continue
            for attr in ("name", "server_name", "server", "id"):
                value = getattr(obj, attr, None)
                if value:
                    return str(value)
        return fallback

    @staticmethod
    def _normalize_values(value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, str):
            items = [item.strip() for item in value.splitlines() if item.strip()]
            return [] if "__all__" in items else items
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            return [] if "__all__" in items else items
        item = str(value).strip()
        return [] if item == "__all__" else [item]

    @staticmethod
    def _library_ids_selection(value: Any) -> list[str]:
        if not value:
            return ["__all__"]
        if isinstance(value, str):
            items = [item.strip() for item in value.splitlines() if item.strip()]
            return items or ["__all__"]
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            return items or ["__all__"]
        return [str(value).strip()]

    def __update_config(self, config: dict[str, Any]) -> None:
        update_config = getattr(self, "update_config", None)
        if callable(update_config):
            update_config(config)

    @staticmethod
    def _switch(model: str, label: str, cols: int) -> dict[str, Any]:
        return {
            "component": "VCol",
            "props": {"cols": cols},
            "content": [{
                "component": "VSwitch",
                "props": {"model": model, "label": label},
            }],
        }

    @staticmethod
    def _text(model: str, label: str, placeholder: str, cols: int) -> dict[str, Any]:
        return {
            "component": "VCol",
            "props": {"cols": cols},
            "content": [{
                "component": "VTextField",
                "props": {"model": model, "label": label, "placeholder": placeholder},
            }],
        }

    @staticmethod
    def _select(
        model: str,
        label: str,
        items: list[dict[str, str]],
        cols: int,
        multiple: bool = False,
        placeholder: str = "",
        clearable: bool = True,
        hint: str = "",
    ) -> dict[str, Any]:
        props: dict[str, Any] = {
            "model": model,
            "label": label,
            "items": items,
            "item-title": "title",
            "item-value": "value",
            "clearable": clearable,
        }
        if placeholder:
            props["placeholder"] = placeholder
        if hint:
            props["hint"] = hint
            props["persistent-hint"] = True
        if multiple:
            props.update({
                "multiple": True,
                "chips": True,
                "closable-chips": True,
            })
        return {
            "component": "VCol",
            "props": {"cols": cols},
            "content": [{
                "component": "VSelect",
                "props": props,
            }],
        }

    @staticmethod
    def _number(model: str, label: str, cols: int) -> dict[str, Any]:
        return {
            "component": "VCol",
            "props": {"cols": cols},
            "content": [{
                "component": "VTextField",
                "props": {"model": model, "label": label, "type": "number"},
            }],
        }

    @staticmethod
    def _textarea(model: str, label: str, placeholder: str, cols: int) -> dict[str, Any]:
        return {
            "component": "VCol",
            "props": {"cols": cols},
            "content": [{
                "component": "VTextarea",
                "props": {"model": model, "label": label, "placeholder": placeholder, "rows": 4},
            }],
        }
