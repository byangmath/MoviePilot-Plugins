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
from .reorganizer import MoviePilotReorganizer


class RecentEpisodeMaintenance(_PluginBase):
    _REFRESH_MODE_OPTIONS = [
        {"title": "扫描新的和有修改的文件", "value": "scan"},
        {"title": "搜索缺少的元数据", "value": "missing"},
        {"title": "覆盖所有元数据", "value": "all"},
    ]
    plugin_name = "最近剧集维护"
    plugin_desc = "维护 MoviePilot 最近整理入库的 Jellyfin 剧集"
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
        self._refresh_mode = str(config.get("refresh_mode") or "all")
        if self._refresh_mode not in {"scan", "missing", "all"}:
            self._refresh_mode = "all"
        self._replace_images = bool(config.get("replace_images", True))
        refresh_defaults = {
            "refresh_mode": self._refresh_mode,
            "replace_images": self._replace_images,
        }
        for key, value in refresh_defaults.items():
            if key not in config or config.get(key) is None or (key == "refresh_mode" and config.get(key) != value):
                config[key] = value
                config_changed = True
        self._media_server_name = config.get("media_server_name") or self._first_jellyfin_service_name()
        library_ids_selection = self._library_ids_selection(config.get("library_ids"))
        self._library_ids = self._normalize_values(library_ids_selection)
        if config.get("library_ids") != library_ids_selection:
            config["library_ids"] = library_ids_selection
            config_changed = True

        for obsolete_key in (
            "path_mappings",
            "metadata_mode",
            "image_mode",
            "replace_metadata",
        ):
            if obsolete_key in config:
                config.pop(obsolete_key)
                config_changed = True

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
                            self._switch("enabled", "启用插件", 6, "开启后按执行周期自动运行"),
                            self._switch("run_once", "立即运行一次", 6, "保存配置后执行一次，随后自动关闭"),
                            self._text(
                                "cron",
                                "执行周期",
                                "APScheduler Cron，如 0 4 * * *",
                                6,
                                "0 4 * * * 表示每天 04:00 运行；依次填写分、时、日、月、星期",
                            ),
                            self._number(
                                "days",
                                "最近 N 天",
                                6,
                                "按 MoviePilot 整理时间筛选最近 N 天的成功剧集记录",
                            ),
                            self._number(
                                "max_items",
                                "单次最大处理数量",
                                6,
                                "限制每次最多处理的 MoviePilot 整理记录数",
                            ),
                            self._switch(
                                "dry_run",
                                "试运行模式",
                                6,
                                "只查询、匹配和预览，不刷新元数据或修改文件",
                            ),
                            self._switch(
                                "notify",
                                "运行完成后发送通知",
                                6,
                                "通过 MoviePilot 消息渠道发送本次执行结果",
                            ),
                            self._switch(
                                "enable_refresh",
                                "刷新最近整理剧集元数据",
                                6,
                                "在 Jellyfin 中定位最近整理入库的剧集并刷新元数据",
                            ),
                            self._switch(
                                "enable_reorganize",
                                "重命名最近整理剧集文件",
                                6,
                                "按原整理记录重新整理，使当前命名规则生效",
                            ),
                            self._switch(
                                "scan_after_reorganize",
                                "整理后扫描媒体库",
                                6,
                                "重新整理成功后通知 Jellyfin 扫描媒体库",
                            ),
                            self._switch(
                                "skip_same_name",
                                "跳过名称未变化的文件",
                                6,
                                "预览目标路径与当前路径相同时不重新整理",
                            ),
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
                                hint="选择需要刷新剧集元数据的 Jellyfin 服务",
                            ),
                            self._select(
                                "library_ids",
                                "维护媒体库",
                                library_options,
                                12,
                                multiple=True,
                                hint="限制 Jellyfin 条目匹配范围；选择全部则包含所有剧集库",
                            ),
                            self._select(
                                "refresh_mode",
                                "刷新模式",
                                self._REFRESH_MODE_OPTIONS,
                                12,
                                clearable=False,
                                hint="控制 Jellyfin 如何查找或替换元数据",
                            ),
                            self._switch(
                                "replace_images",
                                "替换现有图片",
                                12,
                                "完整刷新时重新下载并替换现有图片",
                            ),
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
            "refresh_mode": "all",
            "replace_images": True,
        }

    def stop_service(self):
        pass

    def get_page(self) -> list[dict[str, Any]]:
        pass

    def run_once(self):
        if not self._enable_refresh and not self._enable_reorganize:
            logger.warning("[最近剧集维护] 未启用任何功能")
            return

        result = RunResult()
        reorganizer = MoviePilotReorganizer(logger=logger, dry_run=self._dry_run)
        try:
            histories = reorganizer.recent_histories(
                days=self._days,
                max_items=self._max_items,
            )
            result.reorganize_candidates = len(histories)
            logger.info(
                f"[最近剧集维护] 查询 MP 最近 {self._days} 天成功整理记录，"
                f"共 {len(histories)} 条"
            )
        except Exception as err:
            logger.error(f"[最近剧集维护] 查询 MP 整理历史失败：{err}")
            return

        client: JellyfinServiceClient | None = None
        if self._enable_refresh:
            client = self._get_jellyfin_client()
            if client:
                targets = [
                    target
                    for history in histories
                    if (target := reorganizer.target_path(history)) is not None
                ]
                try:
                    matches = client.match_recent_episodes(
                        target_paths=targets,
                        days=self._days,
                        library_ids=self._library_ids,
                    )
                except Exception as err:
                    matches = {}
                    result.add_error(f"匹配 Jellyfin 剧集失败：{err}")
                    logger.error(f"[最近剧集维护] 匹配 Jellyfin 剧集失败：{err}")

                matched_episodes = {}
                for history in histories:
                    target = reorganizer.target_path(history)
                    target_key = client.path_key(target)
                    episodes = matches.get(target_key) or []
                    if not episodes:
                        result.skipped += 1
                        logger.warning(
                            f"[最近剧集维护] Jellyfin 中未找到整理记录对应条目："
                            f"{reorganizer.display_name(history)}，目标路径 {target or '未知'}"
                        )
                        continue
                    for episode in episodes:
                        matched_episodes[episode.item_id] = episode

                result.refresh_candidates = len(matched_episodes)
                metadata_mode, image_mode, replace_metadata, replace_images = self._refresh_options()
                for episode in matched_episodes.values():
                    if self._dry_run:
                        result.refresh_previewed += 1
                        logger.info(f"[最近剧集维护] 试运行：将完整刷新 {episode.display_name}")
                        continue
                    try:
                        client.refresh_episode(
                            item_id=episode.item_id,
                            metadata_mode=metadata_mode,
                            image_mode=image_mode,
                            replace_metadata=replace_metadata,
                            replace_images=replace_images,
                        )
                        result.refreshed += 1
                        logger.info(f"[最近剧集维护] 元数据和图片刷新成功 {episode.display_name}")
                    except Exception as err:
                        result.add_error(f"{episode.display_name}：{err}")
                        logger.error(f"[最近剧集维护] 元数据刷新失败 {episode.display_name}：{err}")
            else:
                result.add_error("未找到可用的 Jellyfin 媒体服务器")

        any_reorganized = False
        if self._enable_reorganize:
            for history in histories:
                label = reorganizer.display_name(history)
                try:
                    operation = reorganizer.reorganize(
                        history=history,
                        skip_same_name=self._skip_same_name,
                    )
                    if operation.success:
                        if self._dry_run:
                            result.previewed += 1
                        else:
                            result.reorganized += 1
                            any_reorganized = True
                        logger.info(f"[最近剧集维护] {label}：{operation.message}")
                    elif operation.skipped:
                        result.skipped += 1
                        logger.info(f"[最近剧集维护] 跳过 {label}：{operation.message}")
                    else:
                        result.add_error(f"{label}：{operation.message}")
                        logger.error(f"[最近剧集维护] 重新整理失败 {label}：{operation.message}")
                except Exception as err:
                    result.add_error(f"{label}：{err}")
                    logger.error(f"[最近剧集维护] 重新整理失败 {label}：{err}")

        if any_reorganized and self._scan_after_reorganize and not self._dry_run:
            client = client or self._get_jellyfin_client()
            try:
                if client:
                    client.scan_library()
                    logger.info("[最近剧集维护] 已触发 Jellyfin 媒体库扫描")
                else:
                    result.add_error("重新整理后无法触发 Jellyfin 媒体库扫描")
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

    def _refresh_options(self) -> tuple[str, str, bool, bool]:
        if self._refresh_mode == "scan":
            return "Default", "Default", False, False
        return (
            "FullRefresh",
            "FullRefresh",
            self._refresh_mode == "all",
            self._replace_images,
        )

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
    def _switch(model: str, label: str, cols: int, hint: str = "") -> dict[str, Any]:
        props: dict[str, Any] = {"model": model, "label": label}
        if hint:
            props["hint"] = hint
            props["persistent-hint"] = True
        return {
            "component": "VCol",
            "props": {"cols": cols},
            "content": [{
                "component": "VSwitch",
                "props": props,
            }],
        }

    @staticmethod
    def _text(model: str, label: str, placeholder: str, cols: int, hint: str = "") -> dict[str, Any]:
        props: dict[str, Any] = {
            "model": model,
            "label": label,
            "placeholder": placeholder,
        }
        if hint:
            props["hint"] = hint
            props["persistent-hint"] = True
        return {
            "component": "VCol",
            "props": {"cols": cols},
            "content": [{
                "component": "VTextField",
                "props": props,
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
    def _number(model: str, label: str, cols: int, hint: str = "") -> dict[str, Any]:
        props: dict[str, Any] = {"model": model, "label": label, "type": "number"}
        if hint:
            props["hint"] = hint
            props["persistent-hint"] = True
        return {
            "component": "VCol",
            "props": {"cols": cols},
            "content": [{
                "component": "VTextField",
                "props": props,
            }],
        }
