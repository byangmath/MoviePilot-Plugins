from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
from os import scandir
from pathlib import Path
from shutil import rmtree
from threading import Lock
from time import monotonic, sleep
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
from .models import EpisodeItem, RunResult
from .reorganizer import MoviePilotReorganizer


_RUN_STATE_LOCK = Lock()
_RUN_ACTIVE = False
_PENDING_RUNNER: Any | None = None


class RecentEpisodeMaintenance(_PluginBase):
    _PROCESSING_STATE_KEY = "processing_state_v1"
    _STATE_PENDING_REFRESH = "pending_refresh"
    _STATE_PENDING_REORGANIZE = "pending_reorganize"
    _STATE_MONITORING = "monitoring"
    _STATE_COMPLETE = "complete"
    _STATE_ATTENTION = "attention"
    _MAX_REORGANIZE_ATTEMPTS = 2
    _MAX_SIDECAR_ATTEMPTS = 2
    _SIDECAR_WAIT_SECONDS = 60
    _SIDECAR_POLL_SECONDS = 5
    _SIDECAR_RECHECK_MINUTES = 5
    _OLD_SIDECAR_CLEANUP_MINUTES = 10
    _OLD_SIDECAR_VERIFY_MINUTES = 10
    _OLD_SIDECAR_MAX_PASSES = 2
    _REFRESH_RECHECK_MINUTES = 30
    _MONITOR_INTERVAL_HOURS = (24, 48, 72)
    _INSPECTION_MULTIPLIER = 5
    _STATE_SAVE_ATTEMPTS = 3
    _STATE_SAVE_RETRY_SECONDS = 2
    _IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
    _SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt", ".sup"}
    _CRON_HINT_EXPRESSION = (
        "{{ (() => {"
        "const value = String(cron || '').trim();"
        "const prefix = '依次填写分、时、日、月、星期：';"
        "const parts = value.split(/\\s+/);"
        "if (parts.length !== 5) return prefix + '请输入有效的 5 位 Cron 表达式';"
        "const [minute, hour, day, month, weekday] = parts;"
        "const integer = item => /^\\d+$/.test(item);"
        "const minuteOk = integer(minute) && Number(minute) <= 59;"
        "const hourOk = integer(hour) && Number(hour) <= 23;"
        "const time = minuteOk && hourOk"
        " ? `${String(Number(hour)).padStart(2, '0')}:${String(Number(minute)).padStart(2, '0')}`"
        " : '';"
        "if (minuteOk && hourOk && day === '*' && month === '*' && weekday === '*')"
        " return prefix + `每天 ${time} 运行`;"
        "const minuteInterval = minute.match(/^\\*\\/([1-9]|[1-5]\\d)$/);"
        "if (minuteInterval && hour === '*' && day === '*' && month === '*' && weekday === '*')"
        " return prefix + `每 ${minuteInterval[1]} 分钟运行`;"
        "const hourInterval = hour.match(/^\\*\\/([1-9]|1\\d|2[0-3])$/);"
        "if (minuteOk && hourInterval && day === '*' && month === '*' && weekday === '*')"
        " return prefix + (Number(minute) === 0"
        " ? `每 ${hourInterval[1]} 小时运行`"
        " : `每 ${hourInterval[1]} 小时的第 ${Number(minute)} 分钟运行`);"
        "if (minuteOk && hourOk && integer(day) && Number(day) >= 1 && Number(day) <= 31"
        " && month === '*' && weekday === '*')"
        " return prefix + `每月 ${Number(day)} 日 ${time} 运行`;"
        "if (minuteOk && hourOk && integer(day) && Number(day) >= 1 && Number(day) <= 31"
        " && integer(month) && Number(month) >= 1 && Number(month) <= 12 && weekday === '*')"
        " return prefix + `每年 ${Number(month)} 月 ${Number(day)} 日 ${time} 运行`;"
        "const weekdays = {mon: '星期一', tue: '星期二', wed: '星期三', thu: '星期四',"
        " fri: '星期五', sat: '星期六', sun: '星期日'};"
        "if (minuteOk && hourOk && day === '*' && month === '*' && weekdays[weekday.toLowerCase()])"
        " return prefix + `每周${weekdays[weekday.toLowerCase()]} ${time} 运行`;"
        "return prefix + `自定义计划（${value}）`;"
        "})() }}"
    )
    _REFRESH_MODE_OPTIONS = [
        {"title": "扫描新的和有修改的文件", "value": "scan"},
        {"title": "搜索缺少的元数据", "value": "missing"},
        {"title": "覆盖所有元数据", "value": "all"},
    ]
    plugin_name = "最近剧集维护"
    plugin_desc = "维护 MoviePilot 最近整理入库的 Jellyfin 剧集"
    plugin_icon = "https://raw.githubusercontent.com/byangmath/MoviePilot-Plugins/main/icons/recentepisodemaintenance.png"
    plugin_version = "0.2.3"
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
        self._cleanup_old_sidecars = bool(config.get("cleanup_old_sidecars", True))
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
        retry_attention = bool(config.get("retry_attention", False))
        if retry_attention:
            config["retry_attention"] = False
            config_changed = True

        if config_changed:
            self.__update_config(config)

        if retry_attention:
            run_once = self._retry_attention_records() > 0 or run_once

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
                            self._switch(
                                "retry_attention",
                                "重新处理需人工检查记录",
                                6,
                                "重置全部人工检查记录的对应重试次数并立即重新入队，保存后自动关闭",
                            ),
                            self._text(
                                "cron",
                                "执行周期",
                                "APScheduler Cron，如 0 4 * * *",
                                6,
                                self._CRON_HINT_EXPRESSION,
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
                                "限制每轮刷新和重新整理的合计操作数；最多检查该数量五倍的视频整理记录",
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
                                "仅在发生刷新、重新整理或失败时通过 MoviePilot 消息渠道发送结果",
                            ),
                            self._switch(
                                "enable_refresh",
                                "刷新最近整理剧集元数据",
                                6,
                                "Jellyfin 标题与 MoviePilot 当前整理预览中的剧集标题不一致时刷新元数据",
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
                                "cleanup_old_sidecars",
                                "清理旧名称附件",
                                6,
                                "确认新视频、NFO 和图片就绪后，延迟清理本次重命名留下的旧名称附件",
                            ),
                            self._switch(
                                "skip_same_name",
                                "跳过命名未变化的文件",
                                6,
                                "按当前命名规则预览，若生成的文件路径与上次整理结果相同，则跳过。",
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
            "retry_attention": False,
            "cron": "0 4 * * *",
            "days": 7,
            "max_items": 20,
            "dry_run": True,
            "notify": False,
            "enable_refresh": True,
            "enable_reorganize": False,
            "scan_after_reorganize": True,
            "cleanup_old_sidecars": True,
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
        global _PENDING_RUNNER, _RUN_ACTIVE

        with _RUN_STATE_LOCK:
            if _RUN_ACTIVE:
                _PENDING_RUNNER = self
                logger.info(
                    "[最近剧集维护] 已有任务正在运行，本次触发已排队，"
                    "将在本轮结束后运行"
                )
                return
            _RUN_ACTIVE = True

        runner: RecentEpisodeMaintenance | None = self
        while runner is not None:
            try:
                runner._run_once()
            except Exception as err:
                logger.exception(f"[最近剧集维护] 运行异常：{err}")

            with _RUN_STATE_LOCK:
                runner = _PENDING_RUNNER
                _PENDING_RUNNER = None
                if runner is None:
                    _RUN_ACTIVE = False
                    return

            logger.info("[最近剧集维护] 上一轮已结束，开始执行排队任务")

    def _run_once(self):
        if not self._enable_refresh and not self._enable_reorganize:
            logger.warning("[最近剧集维护] 未启用任何功能")
            return

        operation_limit = max(int(self._max_items), 1)
        result = RunResult(operation_limit=operation_limit)
        reorganizer = MoviePilotReorganizer(logger=logger, dry_run=self._dry_run)
        compatibility_check = getattr(reorganizer, "compatibility_error", None)
        compatibility_error = (
            str(compatibility_check() or "")
            if callable(compatibility_check)
            else ""
        )
        if compatibility_error:
            result.add_error(f"MoviePilot 兼容性检查失败：{compatibility_error}")
            logger.error(
                f"[最近剧集维护] MoviePilot 兼容性检查失败："
                f"{compatibility_error}"
            )
            self._finish_run(result)
            return
        try:
            stored_state = self._load_processing_state()
            history_pool = reorganizer.recent_histories(
                days=self._days,
                tracked_history_ids=self._tracked_history_ids(stored_state),
                preferred_history_ids=self._protected_history_ids(stored_state),
            )
            histories, processing_state, selection = self._select_histories(
                histories=history_pool,
                reorganizer=reorganizer,
                state=stored_state,
            )
            result.reorganize_candidates = len(histories)
            result.queue_counts = {
                key: int(selection.get(key) or 0)
                for key in (
                    "pending",
                    "new",
                    "monitoring",
                    "pending_queued",
                    "new_queued",
                    "monitoring_queued",
                    "scan_waiting",
                    "refresh_waiting",
                    "monitoring_waiting",
                    "sidecar_waiting",
                    "cleanup_waiting",
                    "complete",
                    "attention",
                )
            }
            logger.info(
                f"[最近剧集维护] 查询 MP 最近 {self._days} 天成功视频整理记录，"
                f"共 {len(history_pool)} 条；本轮最多执行 {operation_limit} 次操作，"
                f"检查 {len(histories)} 条视频记录"
                f"（待复查 {selection['pending']} 条，新记录 {selection['new']} 条，"
                f"到期复查 {selection['monitoring']} 条）；"
                f"当前状态：待后续检查 "
                f"{selection['pending_queued'] + selection['new_queued'] + selection['monitoring_queued']} 条，"
                f"等待扫描 {selection['scan_waiting']} 条，"
                f"等待刷新确认 {selection['refresh_waiting']} 条，"
                f"等待复查 {selection['monitoring_waiting']} 条，"
                f"等待附件 {selection['sidecar_waiting']} 条，"
                f"等待清理 {selection['cleanup_waiting']} 条，"
                f"已完成 {selection['complete']} 条，需人工检查 {selection['attention']} 条"
            )
            if selection["attention_items"]:
                logger.warning(
                    "[最近剧集维护] 需人工检查记录：\n"
                    + "\n".join(f"- {item}" for item in selection["attention_items"])
                )
        except Exception as err:
            result.add_error(f"读取队列或查询 MP 整理历史失败：{err}")
            logger.error(f"[最近剧集维护] 读取队列或查询 MP 整理历史失败：{err}")
            self._finish_run(result)
            return

        if not histories:
            waiting_text = self._waiting_records_text(selection)
            logger.info(
                "[最近剧集维护] 当前时间范围内没有到期待处理的整理记录"
                f"{waiting_text}"
            )
            self._finish_run(result, processing_state)
            return

        client: JellyfinServiceClient | None = None
        needs_jellyfin = self._enable_refresh or (
            self._enable_reorganize and self._scan_after_reorganize
        )
        if needs_jellyfin:
            client = self._get_jellyfin_client()
            if client:
                try:
                    libraries = client.libraries()
                    available_library_ids = {
                        str(item.get("value") or "")
                        for item in libraries
                        if item.get("value")
                    }
                    missing_library_ids = set(self._library_ids) - available_library_ids
                    if missing_library_ids:
                        raise RuntimeError(
                            "配置的 Jellyfin 媒体库已不存在："
                            + "、".join(sorted(missing_library_ids))
                        )
                except Exception as err:
                    result.add_error(f"Jellyfin 兼容性检查失败：{err}")
                    logger.error(
                        f"[最近剧集维护] Jellyfin 兼容性检查失败：{err}"
                    )
                    self._finish_run(result, processing_state)
                    return
            else:
                result.add_error("Jellyfin 兼容性检查失败：未找到可用服务")
                self._finish_run(result, processing_state)
                return

        scan_pending_keys = {
            reorganizer.processing_key(history)
            for history in histories
            if bool(
                (processing_state.get(reorganizer.processing_key(history)) or {}).get(
                    "scan_pending"
                )
            )
        }
        if scan_pending_keys and client:
            snapshots = self._state_item_snapshots(
                processing_state,
                scan_pending_keys,
            )
            self._set_operation_intent(
                processing_state,
                scan_pending_keys,
                "scan",
            )
            if not self._checkpoint_processing_state(processing_state):
                self._restore_state_items(processing_state, snapshots)
                result.add_error("保存媒体库扫描意图失败，本轮未执行扫描")
                self._finish_run(result, processing_state)
                return
            try:
                client.scan_library()
                for key in scan_pending_keys:
                    item = dict(processing_state.get(key) or {})
                    item.pop("scan_pending", None)
                    processing_state[key] = item
                self._clear_operation_intent(
                    processing_state,
                    scan_pending_keys,
                )
                if not self._checkpoint_processing_state(processing_state):
                    result.add_error("媒体库扫描已提交，但结果状态保存失败")
                    self._finish_run(result, processing_state)
                    return
                logger.info(
                    f"[最近剧集维护] 已重试 Jellyfin 媒体库扫描，"
                    f"涉及 {len(scan_pending_keys)} 条记录"
                )
                histories = [
                    history
                    for history in histories
                    if reorganizer.processing_key(history) not in scan_pending_keys
                ]
            except Exception as err:
                self._clear_operation_intent(
                    processing_state,
                    scan_pending_keys,
                )
                if not self._checkpoint_processing_state(processing_state):
                    result.add_error(
                        "媒体库扫描重试失败且待重试状态保存失败"
                    )
                result.add_error(f"媒体库扫描重试失败：{err}")
                self._finish_run(result, processing_state)
                return
            if not histories:
                self._finish_run(result, processing_state)
                return

        deferred_reorganize_targets: set[str] = set()
        deferred_reorganize_reasons: dict[str, str] = {}
        refresh_first_targets: set[str] = set()
        history_keys_by_target: dict[str, set[str]] = {}
        expected_paths: dict[str, Path] = {}
        preview_failed_keys: set[str] = set()
        state_persistence_failed = False
        for history in histories:
            processing_key = reorganizer.processing_key(history)
            target_key = JellyfinServiceClient.path_key(reorganizer.target_path(history))
            history_keys_by_target.setdefault(target_key, set()).add(processing_key)

            state_item = processing_state.get(processing_key) or {}
            status = state_item.get("status")
            cached_path = state_item.get("expected_path")
            placeholder_cache_expired = (
                status == self._STATE_PENDING_REFRESH
                and EpisodeItem.path_title_is_placeholder(cached_path)
                and self._history_window_expired(state_item)
            )
            if (
                cached_path
                and not placeholder_cache_expired
                and status in {
                    self._STATE_PENDING_REFRESH,
                    self._STATE_PENDING_REORGANIZE,
                }
            ):
                expected_paths[processing_key] = Path(str(cached_path))
                continue

            preview = reorganizer.preview(history)
            if preview.success and preview.target:
                expected_paths[processing_key] = preview.target
                if not self._dry_run:
                    self._cache_processing_preview(
                        processing_state,
                        {processing_key},
                        preview.target,
                    )
                continue

            preview_failed_keys.add(processing_key)
            deferred_reorganize_targets.add(target_key)
            deferred_reorganize_reasons[target_key] = "MP 整理预览失败"
            message = f"{reorganizer.display_name(history)}：{preview.message}"
            result.add_error(message, reorganizer.target_path(history))
            logger.error(
                f"[最近剧集维护] 预览失败 {reorganizer.display_name(history)}："
                f"{preview.message}｜文件：{reorganizer.target_path(history) or '未知文件'}"
            )
            if not self._dry_run:
                self._mark_processing_state(
                    processing_state,
                    {processing_key},
                    self._STATE_PENDING_REFRESH
                    if self._enable_refresh
                    else self._STATE_PENDING_REORGANIZE,
                )

        if not self._enable_refresh:
            for history in histories:
                processing_key = reorganizer.processing_key(history)
                expected_path = expected_paths.get(processing_key)
                if not EpisodeItem.path_title_is_placeholder(expected_path):
                    continue
                target_key = JellyfinServiceClient.path_key(
                    reorganizer.target_path(history)
                )
                placeholder_completed = self._history_window_expired(
                    processing_state.get(processing_key) or {}
                )
                if not self._dry_run:
                    placeholder_completed = (
                        self._mark_processing_waiting_for_preview_title(
                            processing_state,
                            {processing_key},
                            placeholder_refresh_done=(
                                self._placeholder_refresh_completed(
                                    processing_state,
                                    {processing_key},
                                )
                            ),
                        )
                    )
                deferred_reorganize_targets.add(target_key)
                deferred_reorganize_reasons[target_key] = (
                    "MP 在最近 N 天内始终为占位标题，记录已完成"
                    if placeholder_completed
                    else "MP 整理预览仍为占位标题，等待 MP 数据更新"
                )

        if self._enable_refresh:
            if client:
                targets = [
                    target
                    for history in histories
                    if reorganizer.processing_key(history) not in preview_failed_keys
                    and (target := reorganizer.episode_target(history)) is not None
                ]
                matching_failed = False
                try:
                    matches = client.match_recent_episodes(
                        targets=targets,
                        days=self._days,
                        library_ids=self._library_ids,
                    )
                except Exception as err:
                    matching_failed = True
                    matches = {}
                    result.add_error(f"匹配 Jellyfin 剧集失败：{err}")
                    logger.error(f"[最近剧集维护] 匹配 Jellyfin 剧集失败：{err}")

                matched_episodes = {}
                episode_target_keys: dict[str, set[str]] = {}
                unmatched_histories: list[str] = []
                for history in histories:
                    if reorganizer.processing_key(history) in preview_failed_keys:
                        continue
                    target = reorganizer.target_path(history)
                    target_key = client.path_key(target)
                    episodes = matches.get(target_key) or []
                    if not episodes:
                        result.add_skipped(target_key)
                        unmatched_histories.append(reorganizer.display_name(history))
                        deferred_reorganize_targets.add(target_key)
                        deferred_reorganize_reasons[target_key] = (
                            "Jellyfin 剧集匹配失败"
                            if matching_failed
                            else "未在 Jellyfin 中匹配到视频文件"
                        )
                        if not self._dry_run:
                            self._mark_processing_state(
                                processing_state,
                                history_keys_by_target.get(target_key) or set(),
                                self._STATE_PENDING_REFRESH,
                            )
                        continue
                    for episode in episodes:
                        matched_episodes[episode.item_id] = episode
                        episode_target_keys.setdefault(episode.item_id, set()).add(target_key)

                if unmatched_histories:
                    examples = "、".join(unmatched_histories[:3])
                    omitted = len(unmatched_histories) - 3
                    suffix = f" 等，另有 {omitted} 条" if omitted > 0 else ""
                    logger.warning(
                        f"[最近剧集维护] 共 {len(unmatched_histories)} 条 MP 整理记录未在 Jellyfin 中匹配到剧集："
                        f"{examples}{suffix}"
                    )

                result.refresh_candidates = len(matched_episodes)
                metadata_mode, image_mode, replace_metadata, replace_images = self._refresh_options()
                for episode in matched_episodes.values():
                    episode_history_keys = self._history_keys_for_episode(
                        episode.item_id,
                        episode_target_keys,
                        history_keys_by_target,
                    )
                    expected_path = self._expected_path_for_keys(
                        episode_history_keys,
                        expected_paths,
                    )
                    episode_label = episode.episode_label
                    episode_file = expected_path or episode.path or "未知文件"
                    if not expected_path:
                        result.add_error(f"{episode_label}：缺少 MP 整理预览", episode_file)
                        missing_preview_targets = (
                            episode_target_keys.get(episode.item_id) or set()
                        )
                        deferred_reorganize_targets.update(missing_preview_targets)
                        for missing_target in missing_preview_targets:
                            deferred_reorganize_reasons[missing_target] = "缺少 MP 整理预览"
                        if not self._dry_run:
                            self._mark_processing_state(
                                processing_state,
                                episode_history_keys,
                                self._STATE_PENDING_REFRESH,
                            )
                        logger.error(
                            f"[最近剧集维护] 刷新失败 {episode_label}："
                            f"缺少 MP 整理预览｜文件：{episode_file}"
                        )
                        continue

                    placeholder_preview = EpisodeItem.path_title_is_placeholder(
                        expected_path
                    )
                    placeholder_refresh_done = (
                        self._placeholder_refresh_completed(
                            processing_state,
                            episode_history_keys,
                        )
                    )
                    placeholder_refresh_needed = (
                        placeholder_preview
                        and episode.title_is_unreliable()
                        and not placeholder_refresh_done
                    )
                    if placeholder_preview and not placeholder_refresh_needed:
                        placeholder_targets = (
                            episode_target_keys.get(episode.item_id)
                            or {episode.item_id}
                        )
                        result.add_skipped(*placeholder_targets)
                        deferred_reorganize_targets.update(placeholder_targets)
                        placeholder_completed = all(
                            self._history_window_expired(
                                processing_state.get(key) or {}
                            )
                            for key in episode_history_keys
                        )
                        if not self._dry_run:
                            placeholder_completed = (
                                self._mark_processing_waiting_for_preview_title(
                                    processing_state,
                                    episode_history_keys,
                                    placeholder_refresh_done=(
                                        placeholder_refresh_done
                                    ),
                                )
                            )
                        if placeholder_completed:
                            placeholder_reason = (
                                "MP 在最近 N 天内始终为占位标题，记录已完成"
                            )
                        elif episode.title_is_unreliable():
                            placeholder_reason = (
                                "MP 仍为占位标题，Jellyfin 刷新后标题仍不可靠，"
                                "等待 MP 数据更新"
                            )
                        else:
                            placeholder_reason = (
                                "MP 仍为占位标题，暂时保留 Jellyfin 当前标题，"
                                "等待 MP 数据更新"
                            )
                        for placeholder_target in placeholder_targets:
                            deferred_reorganize_reasons[placeholder_target] = (
                                placeholder_reason
                            )
                        prefix = "试运行" if self._dry_run else ""
                        if placeholder_completed:
                            logger.info(
                                f"[最近剧集维护] {prefix}完成占位标题监测 "
                                f"{episode_label}：MP 在最近 {self._days} 天内"
                                "始终未取得非占位标题，保留 Jellyfin 当前标题，"
                                f"不再刷新或重新整理｜文件：{episode_file}"
                            )
                        else:
                            current_title = episode.name or "空标题"
                            logger.info(
                                f"[最近剧集维护] {prefix}等待 MP 标题更新 "
                                f"{episode_label}：Jellyfin 当前标题"
                                f"“{current_title}”，MP 预览仍为占位标题｜"
                                f"预览文件：{Path(expected_path).name}"
                            )
                        continue

                    if (
                        not placeholder_refresh_needed
                        and episode.title_matches_path(expected_path)
                    ):
                        result.add_skipped(
                            *(episode_target_keys.get(episode.item_id) or {episode.item_id})
                        )
                        prefix = "试运行" if self._dry_run else ""
                        logger.info(
                            f"[最近剧集维护] {prefix}跳过 {episode_label}："
                            f"标题一致，无需刷新元数据｜文件：{episode_file}"
                        )
                        if not self._enable_reorganize and not self._dry_run:
                            self._mark_processing_verified(
                                processing_state,
                                episode_history_keys,
                            )
                        continue

                    refresh_targets = episode_target_keys.get(episode.item_id) or set()
                    deferred_reorganize_targets.update(refresh_targets)
                    refresh_first_targets.update(refresh_targets)
                    for refresh_target in refresh_targets:
                        deferred_reorganize_reasons[refresh_target] = (
                            "MP 和 Jellyfin 标题均不可靠，本轮尝试一次元数据刷新"
                            if placeholder_refresh_needed
                            else "标题不一致，本轮先刷新元数据"
                        )
                    if result.operations_used >= result.operation_limit:
                        result.add_skipped(*refresh_targets)
                        for refresh_target in refresh_targets:
                            deferred_reorganize_reasons[refresh_target] = (
                                "标题不一致，已达到操作上限，元数据刷新尚未执行"
                            )
                        if not self._dry_run:
                            self._mark_processing_state(
                                processing_state,
                                episode_history_keys,
                                self._STATE_PENDING_REFRESH,
                            )
                        logger.info(
                            f"[最近剧集维护] 暂缓刷新 {episode_label}："
                            f"已达到单次操作上限｜文件：{episode_file}"
                        )
                        continue
                    if self._dry_run:
                        result.operations_used += 1
                        result.refresh_previewed += 1
                        for refresh_target in refresh_targets:
                            deferred_reorganize_reasons[refresh_target] = (
                                "标题不一致，试运行仅预览元数据刷新"
                            )
                        logger.info(
                            f"[最近剧集维护] 试运行刷新 {episode_label}："
                            + (
                                "MP 和 Jellyfin 标题均不可靠，将尝试一次完整刷新"
                                if placeholder_refresh_needed
                                else "标题不一致，将完整刷新元数据和图片"
                            )
                            + f"｜文件：{episode_file}"
                        )
                        continue
                    refresh_state_snapshot = self._state_item_snapshots(
                        processing_state,
                        episode_history_keys,
                    )
                    self._set_operation_intent(
                        processing_state,
                        episode_history_keys,
                        "refresh",
                        jellyfin_item_id=episode.item_id,
                    )
                    if not self._checkpoint_processing_state(processing_state):
                        self._restore_state_items(
                            processing_state,
                            refresh_state_snapshot,
                        )
                        result.add_error(
                            f"{episode_label}：保存刷新意图失败，本轮未提交刷新",
                            episode_file,
                        )
                        state_persistence_failed = True
                        break
                    result.operations_used += 1
                    try:
                        client.refresh_episode(
                            item_id=episode.item_id,
                            metadata_mode=metadata_mode,
                            image_mode=image_mode,
                            replace_metadata=replace_metadata,
                            replace_images=replace_images,
                        )
                        result.actions_submitted += 1
                        result.refreshed += 1
                        result.add_refreshed_title(str(episode_file))
                        refresh_reason = (
                            "MP 和 Jellyfin 标题均不可靠，已提交唯一一次"
                            "元数据和图片刷新"
                            if placeholder_refresh_needed
                            else "标题不一致，已提交元数据和图片刷新"
                        )
                        logger.info(
                            f"[最近剧集维护] 刷新 {episode_label}："
                            f"{refresh_reason}｜文件：{episode_file}"
                        )
                        self._mark_processing_state(
                            processing_state,
                            episode_history_keys,
                            self._STATE_PENDING_REFRESH,
                            had_action=True,
                            refresh_check_after=self._refresh_recheck_at(),
                            placeholder_refresh_done=(
                                True if placeholder_refresh_needed else None
                            ),
                        )
                        self._clear_operation_intent(
                            processing_state,
                            episode_history_keys,
                        )
                        if not self._checkpoint_processing_state(processing_state):
                            result.add_error(
                                f"{episode_label}：刷新已提交，但结果状态保存失败",
                                episode_file,
                            )
                            state_persistence_failed = True
                        for refresh_target in refresh_targets:
                            deferred_reorganize_reasons[refresh_target] = (
                                "占位标题已提交唯一一次元数据刷新，等待确认"
                                if placeholder_refresh_needed
                                else "标题不一致，本轮已提交元数据刷新，等待确认"
                            )
                        if state_persistence_failed:
                            break
                    except Exception as err:
                        self._restore_state_items(
                            processing_state,
                            refresh_state_snapshot,
                        )
                        self._mark_processing_state(
                            processing_state,
                            episode_history_keys,
                            self._STATE_PENDING_REFRESH,
                        )
                        self._clear_operation_intent(
                            processing_state,
                            episode_history_keys,
                        )
                        if not self._checkpoint_processing_state(processing_state):
                            result.add_error(
                                f"{episode_label}：刷新失败且状态保存失败",
                                episode_file,
                            )
                            state_persistence_failed = True
                        result.add_error(f"{episode_label}：{err}", episode_file)
                        for refresh_target in refresh_targets:
                            deferred_reorganize_reasons[refresh_target] = (
                                "标题不一致，元数据刷新提交失败"
                            )
                        logger.error(
                            f"[最近剧集维护] 刷新失败 {episode_label}："
                            f"标题不一致；{err}｜文件：{episode_file}"
                        )
                        if state_persistence_failed:
                            break
            else:
                result.add_error("未找到可用的 Jellyfin 媒体服务器")
                for history in histories:
                    target_key = JellyfinServiceClient.path_key(reorganizer.target_path(history))
                    deferred_reorganize_targets.add(target_key)
                    deferred_reorganize_reasons[target_key] = (
                        "未找到可用的 Jellyfin 媒体服务器"
                    )
                    if not self._dry_run:
                        self._mark_processing_state(
                            processing_state,
                            {reorganizer.processing_key(history)},
                            self._STATE_PENDING_REFRESH,
                        )

        reorganized_sidecars: list[dict[str, Any]] = []
        sidecars_ready_for_scan: set[str] = set()
        sidecar_directory_cache: dict[str, set[str] | None] = {}
        if self._enable_reorganize and not state_persistence_failed:
            for history in histories:
                label = reorganizer.display_name(history)
                processing_key = reorganizer.processing_key(history)
                related_history_count = reorganizer.related_history_count(history)
                current_file = reorganizer.target_path(history)
                target_key = JellyfinServiceClient.path_key(current_file)
                expected_path = expected_paths.get(processing_key)
                state_hint = processing_state.get(processing_key) or {}
                sidecar_pending_hint = bool(state_hint.get("sidecar_pending"))
                cleanup_pending_hint = bool(state_hint.get("cleanup_pending"))
                if (
                    not sidecar_pending_hint
                    and expected_path
                    and state_hint.get("status") == self._STATE_PENDING_REORGANIZE
                    and bool(state_hint.get("had_action"))
                    and self._same_path(current_file, expected_path)
                    and self._missing_reorganized_sidecars(
                        expected_path,
                        sidecar_directory_cache,
                    )
                ):
                    sidecar_pending_hint = True
                    if not self._dry_run:
                        self._mark_processing_state(
                            processing_state,
                            {processing_key},
                            self._STATE_PENDING_REORGANIZE,
                            sidecar_pending=True,
                            sidecar_attempts=0,
                            sidecar_check_after=self._sidecar_recheck_at(),
                        )
                        logger.info(
                            f"[最近剧集维护] 补充刮削 {label}："
                            f"既有重新整理记录附件不完整｜文件：{Path(expected_path).name}"
                        )
                if (
                    target_key in deferred_reorganize_targets
                    and not (
                        (sidecar_pending_hint or cleanup_pending_hint)
                        and target_key not in refresh_first_targets
                    )
                ):
                    result.add_skipped(target_key)
                    prefix = "试运行" if self._dry_run else ""
                    defer_reason = deferred_reorganize_reasons.get(
                        target_key,
                        "等待后续确认",
                    )
                    logger.info(
                        f"[最近剧集维护] {prefix}暂缓重命名 {label}："
                        f"{defer_reason}｜文件：{current_file or '未知文件'}"
                    )
                    continue
                if not expected_path:
                    result.add_error(f"{label}：缺少 MP 整理预览", current_file)
                    if not self._dry_run:
                        self._mark_processing_state(
                            processing_state,
                            {processing_key},
                            self._STATE_PENDING_REORGANIZE,
                        )
                    logger.error(
                        f"[最近剧集维护] 重命名失败 {label}："
                        f"缺少 MP 整理预览｜文件：{current_file or '未知文件'}"
                    )
                    continue
                protected_danmu: list[str] = []
                danmu_settle_issues: list[str] = []
                reorganization_state_snapshot = None
                try:
                    state_item = processing_state.get(processing_key) or {}
                    rename_attempts = int(state_item.get("rename_attempts") or 0)
                    sidecar_pending = bool(state_item.get("sidecar_pending"))
                    sidecar_attempts = int(state_item.get("sidecar_attempts") or 0)
                    sidecar_check_after = state_item.get("sidecar_check_after")
                    cleanup_pending = bool(state_item.get("cleanup_pending"))
                    old_sidecars = [
                        str(path)
                        for path in state_item.get("old_sidecars") or []
                        if path
                    ]
                    cleanup_old_media_path = state_item.get("cleanup_old_media_path")
                    cleanup_check_after = state_item.get("cleanup_check_after")
                    cleanup_passes = int(state_item.get("cleanup_passes") or 0)
                    same_path = self._same_path(current_file, expected_path)
                    if sidecar_pending and not self._missing_reorganized_sidecars(
                        expected_path,
                        sidecar_directory_cache,
                    ):
                        sidecar_pending = False
                        if not cleanup_pending or not self._cleanup_old_sidecars:
                            sidecars_ready_for_scan.add(processing_key)
                        self._mark_processing_state(
                            processing_state,
                            {processing_key},
                            self._STATE_PENDING_REORGANIZE,
                            sidecar_pending=False,
                            sidecar_attempts=sidecar_attempts,
                        )
                        logger.info(
                            f"[最近剧集维护] 刮削完成 {label}：重新整理附件已补齐｜"
                            f"文件：{Path(expected_path).name}"
                        )
                    elif (
                        not sidecar_pending
                        and state_item.get("status") == self._STATE_PENDING_REORGANIZE
                        and bool(state_item.get("had_action"))
                        and same_path
                        and self._missing_reorganized_sidecars(
                            expected_path,
                            sidecar_directory_cache,
                        )
                    ):
                        sidecar_pending = True
                        sidecar_attempts = 0
                        logger.info(
                            f"[最近剧集维护] 补充刮削 {label}："
                            f"既有重新整理记录附件不完整｜文件：{Path(expected_path).name}"
                        )
                    if (
                        sidecar_pending
                        and not self._timestamp_is_due(sidecar_check_after)
                    ):
                        result.add_skipped(target_key)
                        logger.info(
                            f"[最近剧集维护] 等待附件 {label}：NFO 或图片仍在生成，"
                            f"冷却期内不重复整理｜文件：{expected_path}"
                        )
                        continue
                    if sidecar_pending and sidecar_attempts >= self._MAX_SIDECAR_ATTEMPTS:
                        message = (
                            f"{label} 已连续触发刮削 {sidecar_attempts} 次，"
                            "NFO 或图片仍未生成，已停止自动重试；请检查 MoviePilot 刮削日志"
                        )
                        result.add_error(message, expected_path)
                        if not self._dry_run:
                            self._mark_processing_state(
                                processing_state,
                                {processing_key},
                                self._STATE_ATTENTION,
                                attention_stage="sidecar",
                            )
                        logger.error(
                            f"[最近剧集维护] 停止刮削 {label}：连续重试 "
                            f"{sidecar_attempts} 次仍缺少 NFO 或图片｜"
                            f"文件：{Path(expected_path).name}"
                        )
                        continue

                    if cleanup_pending and not sidecar_pending:
                        if not self._cleanup_old_sidecars:
                            self._mark_processing_state(
                                processing_state,
                                {processing_key},
                                self._STATE_PENDING_REORGANIZE,
                                cleanup_pending=False,
                            )
                            cleanup_pending = False
                            sidecars_ready_for_scan.add(processing_key)
                            logger.info(
                                f"[最近剧集维护] 跳过旧附件清理 {label}："
                                "配置已关闭"
                            )
                        elif not self._timestamp_is_due(cleanup_check_after):
                            result.add_skipped(target_key)
                            logger.info(
                                f"[最近剧集维护] 等待清理 {label}："
                                "旧名称附件尚在安全等待期｜"
                                f"{self._cleanup_waiting_file_details(cleanup_old_media_path, old_sidecars)}"
                            )
                            continue
                        elif not old_sidecars or not cleanup_old_media_path:
                            self._mark_processing_state(
                                processing_state,
                                {processing_key},
                                self._STATE_PENDING_REORGANIZE,
                                cleanup_pending=False,
                            )
                            cleanup_pending = False
                            sidecars_ready_for_scan.add(processing_key)
                        else:
                            cleanup_state_snapshot = self._state_item_snapshots(
                                processing_state,
                                {processing_key},
                            )
                            self._set_operation_intent(
                                processing_state,
                                {processing_key},
                                "cleanup",
                            )
                            if not self._checkpoint_processing_state(
                                processing_state
                            ):
                                self._restore_state_items(
                                    processing_state,
                                    cleanup_state_snapshot,
                                )
                                result.add_error(
                                    f"{label}：保存旧附件清理意图失败，本轮未执行清理",
                                    expected_path,
                                )
                                state_persistence_failed = True
                                break
                            deleted, renamed, cleanup_issues = self._cleanup_old_sidecar_snapshot(
                                old_sidecars,
                                cleanup_old_media_path,
                                expected_path,
                            )
                            next_cleanup_pass = cleanup_passes + 1
                            if next_cleanup_pass < self._OLD_SIDECAR_MAX_PASSES:
                                self._mark_processing_state(
                                    processing_state,
                                    {processing_key},
                                    self._STATE_PENDING_REORGANIZE,
                                    cleanup_pending=True,
                                    old_sidecars=old_sidecars,
                                    cleanup_old_media_path=cleanup_old_media_path,
                                    cleanup_check_after=self._old_sidecar_cleanup_at(
                                        verify=True
                                    ),
                                    cleanup_passes=next_cleanup_pass,
                                )
                                self._clear_operation_intent(
                                    processing_state,
                                    {processing_key},
                                )
                                if not self._checkpoint_processing_state(
                                    processing_state
                                ):
                                    result.add_error(
                                        f"{label}：旧附件已清理，但结果状态保存失败",
                                        expected_path,
                                    )
                                    state_persistence_failed = True
                                    break
                                issue_text = (
                                    f"，{len(cleanup_issues)} 个附件待复查"
                                    if cleanup_issues
                                    else ""
                                )
                                sidecars_ready_for_scan.add(processing_key)
                                logger.info(
                                    f"[最近剧集维护] 清理旧附件 {label}："
                                    f"删除 {len(deleted)} 个、改名 {len(renamed)} 个，"
                                    f"扫描后复查是否重新生成{issue_text}"
                                )
                                continue
                            if cleanup_issues:
                                message = (
                                    f"{label}：旧名称附件清理失败；"
                                    + "；".join(cleanup_issues)
                                )
                                result.add_error(message, expected_path)
                                self._mark_processing_state(
                                    processing_state,
                                    {processing_key},
                                    self._STATE_ATTENTION,
                                    cleanup_pending=True,
                                    old_sidecars=old_sidecars,
                                    cleanup_old_media_path=cleanup_old_media_path,
                                    cleanup_passes=next_cleanup_pass,
                                    attention_stage="cleanup",
                                )
                                self._clear_operation_intent(
                                    processing_state,
                                    {processing_key},
                                )
                                if not self._checkpoint_processing_state(
                                    processing_state
                                ):
                                    result.add_error(
                                        f"{label}：附件清理结果状态保存失败",
                                        expected_path,
                                    )
                                    state_persistence_failed = True
                                    break
                                logger.error(
                                    f"[最近剧集维护] 清理失败 {label}："
                                    f"{len(cleanup_issues)} 个旧名称附件仍需人工检查"
                                )
                                continue
                            self._mark_processing_state(
                                processing_state,
                                {processing_key},
                                self._STATE_PENDING_REORGANIZE,
                                cleanup_pending=False,
                            )
                            self._clear_operation_intent(
                                processing_state,
                                {processing_key},
                            )
                            if not self._checkpoint_processing_state(
                                processing_state
                            ):
                                result.add_error(
                                    f"{label}：旧附件已清理，但完成状态保存失败",
                                    expected_path,
                                )
                                state_persistence_failed = True
                                break
                            cleanup_pending = False
                            logger.info(
                                f"[最近剧集维护] 清理完成 {label}："
                                f"复查删除 {len(deleted)} 个、改名 {len(renamed)} 个旧名称附件"
                            )
                    if (
                        self._skip_same_name
                        and not sidecar_pending
                        and same_path
                    ):
                        result.add_skipped(target_key)
                        if not self._dry_run:
                            if (
                                self._scan_after_reorganize
                                and processing_key in sidecars_ready_for_scan
                            ):
                                self._mark_processing_state(
                                    processing_state,
                                    {processing_key},
                                    self._STATE_PENDING_REORGANIZE,
                                )
                            else:
                                self._mark_processing_verified(
                                    processing_state,
                                    {processing_key},
                                )
                        prefix = "试运行" if self._dry_run else ""
                        logger.info(
                            f"[最近剧集维护] {prefix}跳过 {label}：按当前命名规则预览，"
                            f"路径未变化｜{self._file_change_details(current_file, expected_path)}"
                        )
                        continue

                    if self._dry_run:
                        if result.operations_used >= result.operation_limit:
                            result.add_skipped(target_key)
                            logger.info(
                                f"[最近剧集维护] 试运行暂缓重命名 {label}："
                                f"已达到单次操作上限｜"
                                f"{self._file_change_details(current_file, expected_path)}"
                            )
                            continue
                        result.operations_used += 1
                        result.previewed += 1
                        if sidecar_pending:
                            logger.info(
                                f"[最近剧集维护] 试运行重命名 {label}："
                                f"将重试重新整理并补齐刮削附件｜文件：{Path(expected_path).name}"
                            )
                        else:
                            logger.info(
                                f"[最近剧集维护] 试运行重命名 {label}：预计重新命名，"
                                f"检测到同批 {related_history_count} 条附件历史记录｜"
                                f"{self._file_change_details(current_file, expected_path)}"
                            )
                        continue

                    if (
                        not self._dry_run
                        and not sidecar_pending
                        and rename_attempts >= self._MAX_REORGANIZE_ATTEMPTS
                    ):
                        preview = reorganizer.reorganize(
                            history=history,
                            skip_same_name=self._skip_same_name,
                            preview_only=True,
                        )
                        if preview.skipped:
                            result.add_skipped(target_key)
                            self._mark_processing_verified(
                                processing_state,
                                {processing_key},
                            )
                            logger.info(
                                f"[最近剧集维护] 跳过 {label}：{preview.message}｜"
                                f"{self._file_change_details(current_file, preview.target)}"
                            )
                        elif preview.success:
                            message = (
                                f"{label} 已连续重新整理 {rename_attempts} 次，"
                                "按当前规则预览文件名仍会变化，已停止自动处理；"
                                "请检查 MoviePilot 剧集数据和命名规则｜"
                                f"{self._file_change_details(current_file, preview.target)}"
                            )
                            self._mark_processing_state(
                                processing_state,
                                {processing_key},
                                self._STATE_ATTENTION,
                                attention_stage="rename",
                            )
                            result.add_error(message, preview.target or current_file)
                            logger.error(
                                f"[最近剧集维护] 停止重命名 {label}：连续重新整理 "
                                f"{rename_attempts} 次后路径仍会变化｜"
                                f"{self._file_change_details(current_file, preview.target)}"
                            )
                        else:
                            self._mark_processing_state(
                                processing_state,
                                {processing_key},
                                self._STATE_PENDING_REORGANIZE,
                            )
                            result.add_error(f"{label}：{preview.message}", preview.target or current_file)
                            logger.error(
                                f"[最近剧集维护] 预览失败 {label}：{preview.message}｜"
                                f"{self._file_change_details(current_file, preview.target)}"
                            )
                        continue
                    if result.operations_used >= result.operation_limit:
                        result.add_skipped(target_key)
                        self._mark_processing_state(
                            processing_state,
                            {processing_key},
                            self._STATE_PENDING_REORGANIZE,
                        )
                        logger.info(
                            f"[最近剧集维护] 暂缓重命名 {label}：已达到单次操作上限｜"
                            f"{self._file_change_details(current_file, expected_path)}"
                        )
                        continue
                    cleanup_snapshot = []
                    protected_danmu = []
                    if (
                        self._cleanup_old_sidecars
                        and not sidecar_pending
                        and not same_path
                    ):
                        cleanup_snapshot = self._snapshot_old_sidecars(
                            current_file,
                            expected_path,
                        )
                    next_sidecar_attempts = (
                        sidecar_attempts + 1 if sidecar_pending else 1
                    )
                    reorganization_state_snapshot = self._state_item_snapshots(
                        processing_state,
                        {processing_key},
                    )
                    self._set_operation_intent(
                        processing_state,
                        {processing_key},
                        "reorganize",
                        expected_path=str(expected_path),
                        sidecar_attempts=next_sidecar_attempts,
                        old_sidecars=cleanup_snapshot,
                        cleanup_old_media_path=(
                            str(current_file) if cleanup_snapshot else None
                        ),
                        cleanup_passes=0,
                    )
                    if not self._checkpoint_processing_state(processing_state):
                        self._restore_state_items(
                            processing_state,
                            reorganization_state_snapshot,
                        )
                        result.add_error(
                            f"{label}：保存重新整理意图失败，本轮未执行整理",
                            current_file,
                        )
                        state_persistence_failed = True
                        break
                    if cleanup_snapshot and not self._dry_run:
                        protected_danmu, protection_issues = self._protect_danmu_sidecars(
                            cleanup_snapshot,
                            current_file,
                        )
                        if protection_issues:
                            _, _, restore_issues = self._cleanup_old_sidecar_snapshot(
                                protected_danmu,
                                current_file,
                                current_file,
                            )
                            issue_text = "；".join(protection_issues + restore_issues)
                            self._restore_state_items(
                                processing_state,
                                reorganization_state_snapshot,
                            )
                            self._mark_processing_state(
                                processing_state,
                                {processing_key},
                                self._STATE_PENDING_REORGANIZE,
                            )
                            self._clear_operation_intent(
                                processing_state,
                                {processing_key},
                            )
                            if not self._checkpoint_processing_state(
                                processing_state
                            ):
                                result.add_error(
                                    f"{label}：弹幕保护失败且状态保存失败",
                                    current_file,
                                )
                                state_persistence_failed = True
                            result.add_error(
                                f"{label}：弹幕保护失败；{issue_text}",
                                current_file,
                            )
                            logger.error(
                                f"[最近剧集维护] 重命名失败 {label}："
                                f"弹幕保护失败；{issue_text}｜"
                                f"文件：{current_file or '未知文件'}"
                            )
                            if state_persistence_failed:
                                break
                            continue
                    result.operations_used += 1
                    operation = None
                    danmu_settle_issues = []
                    try:
                        operation = reorganizer.reorganize(
                            history=history,
                            skip_same_name=(
                                False if sidecar_pending else self._skip_same_name
                            ),
                        )
                    finally:
                        if protected_danmu:
                            settle_target = self._danmu_settle_target(
                                operation,
                                current_file,
                                expected_path,
                            )
                            if settle_target:
                                _, _, danmu_settle_issues = (
                                    self._cleanup_old_sidecar_snapshot(
                                        protected_danmu,
                                        current_file,
                                        settle_target,
                                    )
                                )
                    if danmu_settle_issues:
                        result.add_error(
                            f"{label}：弹幕归位失败；"
                            + "；".join(danmu_settle_issues),
                            operation.target or expected_path or current_file,
                        )
                        logger.error(
                            f"[最近剧集维护] 弹幕归位失败 {label}："
                            f"{len(danmu_settle_issues)} 个文件仍需后续复查"
                        )
                    if operation.success:
                        if self._dry_run:
                            result.previewed += 1
                        else:
                            sidecar_directory_cache.clear()
                            target = operation.target or expected_path or current_file
                            cleanup_updates = {}
                            if not sidecar_pending:
                                cleanup_updates = {
                                    "cleanup_pending": bool(cleanup_snapshot),
                                    "old_sidecars": cleanup_snapshot,
                                    "cleanup_old_media_path": (
                                        current_file if cleanup_snapshot else None
                                    ),
                                    "cleanup_check_after": (
                                        self._old_sidecar_cleanup_at()
                                        if cleanup_snapshot
                                        else None
                                    ),
                                    "cleanup_passes": 0,
                                }
                            self._mark_processing_state(
                                processing_state,
                                {processing_key},
                                self._STATE_PENDING_REORGANIZE,
                                rename_attempts=(
                                    rename_attempts if sidecar_pending else rename_attempts + 1
                                ),
                                had_action=True,
                                expected_path=target,
                                sidecar_pending=True,
                                sidecar_attempts=next_sidecar_attempts,
                                **cleanup_updates,
                            )
                            self._clear_operation_intent(
                                processing_state,
                                {processing_key},
                            )
                            result.actions_submitted += 1
                            reorganized_sidecars.append({
                                "key": processing_key,
                                "label": label,
                                "target": Path(target) if target else None,
                                "attempts": next_sidecar_attempts,
                            })
                            if not self._checkpoint_processing_state(
                                processing_state
                            ):
                                result.add_error(
                                    f"{label}：重新整理已提交，但结果状态保存失败",
                                    target,
                                )
                                state_persistence_failed = True
                            logger.info(
                                f"[最近剧集维护] 重命名 {label}：已提交重新整理，等待刮削附件；"
                                f"检测到同批 {related_history_count} 条附件历史记录｜"
                                f"{self._file_change_details(current_file, target)}"
                            )
                            if state_persistence_failed:
                                break
                    elif operation.skipped:
                        result.add_skipped(target_key)
                        if not self._dry_run:
                            if danmu_settle_issues:
                                self._mark_processing_state(
                                    processing_state,
                                    {processing_key},
                                    self._STATE_PENDING_REORGANIZE,
                                )
                            else:
                                self._mark_processing_verified(
                                    processing_state,
                                    {processing_key},
                                )
                            self._clear_operation_intent(
                                processing_state,
                                {processing_key},
                            )
                            if not self._checkpoint_processing_state(
                                processing_state
                            ):
                                result.add_error(
                                    f"{label}：整理跳过结果状态保存失败",
                                    operation.target or current_file,
                                )
                                state_persistence_failed = True
                        logger.info(
                            f"[最近剧集维护] 跳过 {label}：{operation.message}｜"
                            f"{self._file_change_details(current_file, operation.target)}"
                        )
                        if state_persistence_failed:
                            break
                    else:
                        if not self._dry_run:
                            self._restore_state_items(
                                processing_state,
                                reorganization_state_snapshot,
                            )
                            self._mark_processing_state(
                                processing_state,
                                {processing_key},
                                self._STATE_PENDING_REORGANIZE,
                            )
                            self._clear_operation_intent(
                                processing_state,
                                {processing_key},
                            )
                            if not self._checkpoint_processing_state(
                                processing_state
                            ):
                                result.add_error(
                                    f"{label}：整理失败且状态保存失败",
                                    operation.target or current_file,
                                )
                                state_persistence_failed = True
                        result.add_error(f"{label}：{operation.message}", operation.target or current_file)
                        logger.error(
                            f"[最近剧集维护] 重命名失败 {label}：{operation.message}｜"
                            f"{self._file_change_details(current_file, operation.target)}"
                        )
                        if state_persistence_failed:
                            break
                except Exception as err:
                    if not self._dry_run:
                        if reorganization_state_snapshot is not None:
                            self._restore_state_items(
                                processing_state,
                                reorganization_state_snapshot,
                            )
                        self._mark_processing_state(
                            processing_state,
                            {processing_key},
                            self._STATE_PENDING_REORGANIZE,
                        )
                        self._clear_operation_intent(
                            processing_state,
                            {processing_key},
                        )
                        if not self._checkpoint_processing_state(
                            processing_state
                        ):
                            result.add_error(
                                f"{label}：整理异常且状态保存失败",
                                current_file,
                            )
                            state_persistence_failed = True
                    settle_text = (
                        "；弹幕恢复失败：" + "；".join(danmu_settle_issues)
                        if danmu_settle_issues
                        else ""
                    )
                    result.add_error(f"{label}：{err}{settle_text}", current_file)
                    logger.error(
                        f"[最近剧集维护] 重命名失败 {label}：{err}{settle_text}｜"
                        f"文件：{current_file or '未知文件'}"
                    )
                    if state_persistence_failed:
                        break

        missing_sidecars: dict[str, list[str]] = {}
        if reorganized_sidecars and not self._dry_run:
            logger.info(
                f"[最近剧集维护] 等待 {len(reorganized_sidecars)} 个重新整理文件生成 NFO 和图片，"
                f"最长 {self._SIDECAR_WAIT_SECONDS} 秒"
            )
            missing_sidecars = self._wait_for_reorganized_sidecars(reorganized_sidecars)
            for item in reorganized_sidecars:
                processing_key = str(item["key"])
                label = str(item["label"])
                target = item.get("target")
                attempts = int(item.get("attempts") or 1)
                missing = missing_sidecars.get(processing_key)
                if not missing:
                    result.reorganized += 1
                    result.add_reorganized_title(str(target or "未知文件"))
                    self._mark_processing_state(
                        processing_state,
                        {processing_key},
                        self._STATE_PENDING_REORGANIZE,
                        sidecar_pending=False,
                        sidecar_attempts=attempts,
                    )
                    sidecars_ready_for_scan.add(processing_key)
                    logger.info(
                        f"[最近剧集维护] 重命名完成 {label}：刮削附件已补齐｜"
                        f"文件：{Path(target).name if target else '未知文件'}"
                    )
                    continue

                missing_text = "、".join(missing)
                exhausted = attempts >= self._MAX_SIDECAR_ATTEMPTS
                status = self._STATE_ATTENTION if exhausted else self._STATE_PENDING_REORGANIZE
                self._mark_processing_state(
                    processing_state,
                    {processing_key},
                    status,
                    sidecar_pending=True,
                    sidecar_attempts=attempts,
                    sidecar_check_after=(
                        None if exhausted else self._sidecar_recheck_at()
                    ),
                    attention_stage="sidecar" if exhausted else None,
                )
                if exhausted:
                    message = (
                        f"{label}：重新整理后仍缺少{missing_text}，已连续尝试 {attempts} 次，"
                        "请检查 MoviePilot 刮削日志"
                    )
                    result.add_error(message, target)
                    logger.error(
                        f"[最近剧集维护] 停止刮削 {label}：重新整理后缺少{missing_text}，"
                        f"已尝试 {attempts}/{self._MAX_SIDECAR_ATTEMPTS} 次｜"
                        f"文件：{Path(target).name if target else '未知文件'}"
                    )
                else:
                    logger.warning(
                        f"[最近剧集维护] 等待附件 {label}：重新整理后仍缺少{missing_text}，"
                        f"最早在 {self._SIDECAR_RECHECK_MINUTES} 分钟后的下一轮复查｜"
                        f"文件：{Path(target).name if target else '未知文件'}"
                    )

        cleanup_blocks_scan = any(
            bool(item.get("cleanup_pending"))
            and int(item.get("cleanup_passes") or 0) < 1
            for item in processing_state.values()
        )
        sidecars_complete = (
            bool(reorganized_sidecars) or sidecars_ready_for_scan
        ) and not missing_sidecars and not cleanup_blocks_scan
        scan_keys = set(sidecars_ready_for_scan)
        if (
            sidecars_complete
            and scan_keys
            and self._scan_after_reorganize
            and not self._dry_run
            and not state_persistence_failed
        ):
            scan_state_snapshot = self._state_item_snapshots(
                processing_state,
                scan_keys,
            )
            for key in scan_keys:
                item = dict(processing_state.get(key) or {})
                item["scan_pending"] = True
                processing_state[key] = item
            self._set_operation_intent(
                processing_state,
                scan_keys,
                "scan",
            )
            if not self._checkpoint_processing_state(processing_state):
                self._restore_state_items(
                    processing_state,
                    scan_state_snapshot,
                )
                result.add_error("保存媒体库扫描意图失败，本轮未执行扫描")
                state_persistence_failed = True
            else:
                client = client or self._get_jellyfin_client()
            try:
                if client and not state_persistence_failed:
                    client.scan_library()
                    for key in scan_keys:
                        item = dict(processing_state.get(key) or {})
                        item.pop("scan_pending", None)
                        processing_state[key] = item
                    self._clear_operation_intent(
                        processing_state,
                        scan_keys,
                    )
                    if not self._checkpoint_processing_state(processing_state):
                        result.add_error(
                            "Jellyfin 媒体库扫描已提交，但结果状态保存失败"
                        )
                        state_persistence_failed = True
                    logger.info("[最近剧集维护] 已触发 Jellyfin 媒体库扫描")
                elif not state_persistence_failed:
                    result.add_error("重新整理后无法触发 Jellyfin 媒体库扫描")
            except Exception as err:
                self._clear_operation_intent(
                    processing_state,
                    scan_keys,
                )
                if not self._checkpoint_processing_state(processing_state):
                    result.add_error("媒体库扫描失败且待重试状态保存失败")
                    state_persistence_failed = True
                result.add_error(f"媒体库扫描失败：{err}")
        elif missing_sidecars and self._scan_after_reorganize and not self._dry_run:
            logger.warning(
                "[最近剧集维护] 部分重新整理文件的 NFO 或图片尚未生成，"
                "本轮不触发 Jellyfin 媒体库扫描"
            )
        elif (
            cleanup_blocks_scan
            and (reorganized_sidecars or sidecars_ready_for_scan)
            and self._scan_after_reorganize
            and not self._dry_run
        ):
            logger.info(
                "[最近剧集维护] 旧名称附件仍在安全等待或复查，"
                "完成清理前不触发 Jellyfin 媒体库扫描"
            )

        self._finish_run(result, processing_state)

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

    @classmethod
    def _wait_for_reorganized_sidecars(
        cls,
        items: list[dict[str, Any]],
    ) -> dict[str, list[str]]:
        pending = {
            str(item["key"]): item
            for item in items
        }
        deadline = monotonic() + cls._SIDECAR_WAIT_SECONDS
        missing_by_key: dict[str, list[str]] = {}
        while pending:
            directory_cache: dict[str, set[str] | None] = {}
            for key, item in list(pending.items()):
                missing = cls._missing_reorganized_sidecars(
                    item.get("target"),
                    directory_cache,
                )
                if missing:
                    missing_by_key[key] = missing
                    continue
                missing_by_key.pop(key, None)
                pending.pop(key, None)

            remaining = deadline - monotonic()
            if not pending or remaining <= 0:
                break
            sleep(min(cls._SIDECAR_POLL_SECONDS, remaining))

        final_cache: dict[str, set[str] | None] = {}
        final_missing: dict[str, list[str]] = {}
        for key, item in pending.items():
            missing = missing_by_key.get(key)
            if missing is None:
                missing = cls._missing_reorganized_sidecars(
                    item.get("target"),
                    final_cache,
                )
            final_missing[key] = missing
        return final_missing

    @classmethod
    def _missing_reorganized_sidecars(
        cls,
        target: Any,
        directory_cache: dict[str, set[str] | None] | None = None,
    ) -> list[str]:
        if not target:
            return ["目标文件", "NFO", "图片"]

        media_path = Path(target)
        filenames = cls._directory_file_names(media_path.parent, directory_cache)
        if filenames is None:
            return ["目标文件", "NFO", "图片"]

        missing: list[str] = []
        if not cls._cached_path_is_file(media_path, filenames):
            missing.append("目标文件")
        if not cls._cached_path_is_file(media_path.with_suffix(".nfo"), filenames):
            missing.append("NFO")

        image_extensions = (".jpg", ".jpeg", ".png", ".webp", ".avif")
        image_paths = [
            media_path.with_suffix(extension)
            for extension in image_extensions
        ]
        image_paths.extend(
            media_path.parent / f"{media_path.stem}-thumb{extension}"
            for extension in image_extensions
        )
        if not any(cls._cached_path_is_file(path, filenames) for path in image_paths):
            missing.append("图片")
        return missing

    @staticmethod
    def _cached_path_is_file(path: Path, filenames: set[str]) -> bool:
        """Check only matching directory entries while preserving exact path semantics."""
        return path.name in filenames and path.is_file()

    @staticmethod
    def _directory_file_names(
        directory: Path,
        cache: dict[str, set[str] | None] | None = None,
    ) -> set[str] | None:
        directory_key = str(directory).strip().replace("\\", "/")
        if cache is not None and directory_key in cache:
            return cache[directory_key]

        try:
            with scandir(directory) as entries:
                filenames = {entry.name for entry in entries}
        except OSError:
            filenames = None

        if cache is not None:
            cache[directory_key] = filenames
        return filenames

    @classmethod
    def _snapshot_old_sidecars(
        cls,
        old_target: Any,
        new_target: Any,
    ) -> list[str]:
        if not old_target or not new_target or cls._same_path(old_target, new_target):
            return []

        old_media = Path(old_target)
        try:
            with scandir(old_media.parent) as entries:
                candidates = [
                    Path(entry.path)
                    for entry in entries
                    if cls._old_sidecar_kind(Path(entry.path), old_media)
                    and (
                        entry.is_file()
                        or entry.is_dir()
                        or entry.is_symlink()
                    )
                ]
        except OSError as err:
            logger.warning(
                f"[最近剧集维护] 无法记录旧名称附件：{err}｜"
                f"文件：{old_media.name}"
            )
            return []

        protected_danmu = [
            old_media.with_suffix(".xml"),
            old_media.parent / f"{old_media.stem}.danmu.ass",
        ]
        for candidate in protected_danmu:
            hold_path = cls._danmu_protection_path(candidate, old_media)
            if (
                (hold_path.exists() or hold_path.is_symlink())
                and candidate not in candidates
            ):
                candidates.append(candidate)
        return sorted(str(path) for path in candidates)

    @classmethod
    def _old_sidecar_kind(
        cls,
        candidate: Path,
        old_media: Path,
    ) -> str | None:
        if JellyfinServiceClient.path_key(candidate.parent) != JellyfinServiceClient.path_key(
            old_media.parent
        ):
            return None

        name = candidate.name
        old_stem = old_media.stem
        suffix = candidate.suffix.lower()
        if name in {f"{old_stem}.trickplay", f"{old_stem}-trickplay"}:
            return "trickplay"
        if name == f"{old_stem}.nfo":
            return "metadata"
        image_stems = {
            old_stem,
            *(f"{old_stem}-{kind}" for kind in (
                "thumb",
                "poster",
                "fanart",
                "landscape",
                "banner",
                "clearlogo",
                "clearart",
                "disc",
                "logo",
            )),
        }
        if suffix in cls._IMAGE_EXTENSIONS and candidate.stem in image_stems:
            return "image"
        if name in {f"{old_stem}.xml", f"{old_stem}.danmu.ass"}:
            return "danmu"
        if suffix in cls._SUBTITLE_EXTENSIONS and name.startswith(f"{old_stem}."):
            return "subtitle"
        return None

    @staticmethod
    def _danmu_settle_target(
        operation: Any,
        current_target: Any,
        expected_target: Any,
    ) -> Any:
        new_target = expected_target or getattr(operation, "target", None)
        if new_target and Path(new_target).is_file():
            return new_target
        if bool(getattr(operation, "success", False)):
            return None
        return current_target

    @classmethod
    def _danmu_protection_path(
        cls,
        candidate: Path,
        old_media: Path,
    ) -> Path:
        identity = (
            f"{JellyfinServiceClient.path_key(old_media)}"
            f"\0{candidate.name}"
        )
        token = sha256(identity.encode("utf-8")).hexdigest()[:16]
        return candidate.parent / (
            f".recentepisodemaintenance-{token}.danmu-hold"
        )

    @classmethod
    def _protect_danmu_sidecars(
        cls,
        paths: list[str],
        old_target: Any,
    ) -> tuple[list[str], list[str]]:
        old_media = Path(old_target)
        protected: list[str] = []
        issues: list[str] = []

        for raw_path in paths:
            candidate = Path(raw_path)
            if cls._old_sidecar_kind(candidate, old_media) != "danmu":
                continue
            hold_path = cls._danmu_protection_path(candidate, old_media)
            if hold_path.exists() or hold_path.is_symlink():
                protected.append(str(candidate))
                if candidate.exists() or candidate.is_symlink():
                    issues.append(f"{candidate.name} 与保护副本同时存在")
                continue
            if not candidate.exists() and not candidate.is_symlink():
                continue
            if not candidate.is_file() and not candidate.is_symlink():
                issues.append(f"{candidate.name} 不是可保护文件")
                continue
            try:
                candidate.rename(hold_path)
                protected.append(str(candidate))
            except OSError as err:
                issues.append(f"{candidate.name} 保护失败：{err}")
        return protected, issues

    @classmethod
    def _cleanup_old_sidecar_snapshot(
        cls,
        paths: list[str],
        old_target: Any,
        new_target: Any,
    ) -> tuple[list[str], list[str], list[str]]:
        old_media = Path(old_target)
        new_media = Path(new_target)
        deleted: list[str] = []
        renamed: list[str] = []
        issues: list[str] = []

        for raw_path in paths:
            original_candidate = Path(raw_path)
            kind = cls._old_sidecar_kind(original_candidate, old_media)
            if kind is None:
                issues.append(f"{original_candidate.name} 未通过路径校验")
                continue
            candidate = original_candidate
            if kind == "danmu":
                hold_path = cls._danmu_protection_path(
                    original_candidate,
                    old_media,
                )
                if hold_path.exists() or hold_path.is_symlink():
                    candidate = hold_path
            if not candidate.exists() and not candidate.is_symlink():
                continue
            tail = original_candidate.name[len(old_media.stem):]
            counterpart = new_media.parent / f"{new_media.stem}{tail}"
            if kind == "subtitle" and not counterpart.is_file():
                issues.append(f"{candidate.name} 缺少新名称对应文件")
                continue
            if kind == "danmu" and not counterpart.is_file():
                try:
                    candidate.rename(counterpart)
                    renamed.append(str(counterpart))
                except OSError as err:
                    issues.append(f"{candidate.name} 改名失败：{err}")
                continue
            try:
                if candidate.is_symlink() or not candidate.is_dir():
                    candidate.unlink()
                elif kind == "trickplay":
                    rmtree(candidate)
                else:
                    issues.append(f"{candidate.name} 不是可清理附件")
                    continue
                deleted.append(str(candidate))
            except OSError as err:
                issues.append(f"{candidate.name} 删除失败：{err}")
        return deleted, renamed, issues

    @classmethod
    def _old_sidecar_cleanup_at(cls, *, verify: bool = False) -> str:
        minutes = (
            cls._OLD_SIDECAR_VERIFY_MINUTES
            if verify
            else cls._OLD_SIDECAR_CLEANUP_MINUTES
        )
        return (
            datetime.now() + timedelta(minutes=minutes)
        ).isoformat(timespec="seconds")

    @staticmethod
    def _file_change_details(current_file: Any, new_file: Any) -> str:
        current = str(current_file or "未知文件")
        target = str(new_file or current_file or "未知文件")
        if JellyfinServiceClient.path_key(current) == JellyfinServiceClient.path_key(target):
            return f"文件：{current}"
        return f"原文件：{current}｜新文件：{target}"

    def _select_histories(
        self,
        histories: list[Any],
        reorganizer: MoviePilotReorganizer,
        state: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[list[Any], dict[str, dict[str, Any]], dict[str, Any]]:
        state = dict(state) if isinstance(state, dict) else self._load_processing_state()
        keyed_histories = [
            (history, reorganizer.processing_key(history))
            for history in histories
        ]
        active_keys = {key for _, key in keyed_histories}
        state = {
            key: value
            for key, value in state.items()
            if key in active_keys and isinstance(value, dict)
        }
        for history, key in keyed_histories:
            state_item = dict(state.get(key) or {})
            history_id = getattr(history, "id", None)
            if history_id is not None:
                state_item["history_id"] = int(history_id)
            history_date = getattr(history, "date", None)
            if history_date:
                state_item["history_date"] = str(history_date)
            state[key] = state_item
        recovered_intents = self._recover_operation_intents(state)
        if recovered_intents:
            logger.warning(
                f"[最近剧集维护] 检测到 {recovered_intents} 条上次运行未确认的"
                "外部操作，已按实际状态重新排队"
            )

        pending_statuses = {
            self._STATE_PENDING_REFRESH,
            self._STATE_PENDING_REORGANIZE,
        }
        known_statuses = {
            *pending_statuses,
            self._STATE_MONITORING,
            self._STATE_COMPLETE,
            self._STATE_ATTENTION,
        }
        pending_all = [
            (history, key)
            for history, key in keyed_histories
            if (state.get(key) or {}).get("status") in pending_statuses
        ]
        cleanup_enabled = bool(getattr(self, "_cleanup_old_sidecars", True))
        for _, key in pending_all:
            state_item = dict(state.get(key) or {})
            if state_item.get("sidecar_pending") and not state_item.get(
                "sidecar_check_after"
            ):
                state_item["sidecar_check_after"] = self._sidecar_recheck_at()
            if (
                cleanup_enabled
                and state_item.get("cleanup_pending")
                and not state_item.get("cleanup_check_after")
            ):
                state_item["cleanup_check_after"] = self._old_sidecar_cleanup_at()
            state[key] = state_item
        refresh_waiting = [
            (history, key)
            for history, key in pending_all
            if (state.get(key) or {}).get("status") == self._STATE_PENDING_REFRESH
            and not self._timestamp_is_due(
                (state.get(key) or {}).get("refresh_check_after")
            )
        ]
        sidecar_waiting = [
            (history, key)
            for history, key in pending_all
            if (state.get(key) or {}).get("sidecar_pending")
            and not self._timestamp_is_due(
                (state.get(key) or {}).get("sidecar_check_after")
            )
        ]
        cleanup_waiting = [
            (history, key)
            for history, key in pending_all
            if cleanup_enabled
            and (state.get(key) or {}).get("cleanup_pending")
            and not (state.get(key) or {}).get("sidecar_pending")
            and not self._timestamp_is_due(
                (state.get(key) or {}).get("cleanup_check_after")
            )
        ]
        waiting_keys = {
            key
            for _, key in refresh_waiting + sidecar_waiting + cleanup_waiting
        }
        pending = [
            (history, key)
            for history, key in pending_all
            if key not in waiting_keys
        ]
        pending.sort(
            key=lambda item: (
                not bool((state.get(item[1]) or {}).get("scan_pending")),
                str((state.get(item[1]) or {}).get("updated_at") or ""),
            )
        )
        new_items = [
            (history, key)
            for history, key in keyed_histories
            if key not in state
            or (state.get(key) or {}).get("status") not in known_statuses
        ]
        monitoring_all = [
            (history, key)
            for history, key in keyed_histories
            if (state.get(key) or {}).get("status") == self._STATE_MONITORING
        ]
        monitoring = [
            (history, key)
            for history, key in monitoring_all
            if self._timestamp_is_due((state.get(key) or {}).get("next_preview_at"))
        ]
        monitoring.sort(
            key=lambda item: str((state.get(item[1]) or {}).get("next_preview_at") or "")
        )

        operation_limit = max(int(self._max_items), 1)
        limit = max(operation_limit, operation_limit * self._INSPECTION_MULTIPLIER)
        selected: list[tuple[Any, str]] = []
        other_items = self._interleave_queue_items(monitoring, new_items)
        if pending and other_items:
            other_reserve = min(len(other_items), max(1, limit // 4)) if limit > 1 else 0
            selected.extend(pending[:max(limit - other_reserve, 0)])
            selected.extend(other_items[:max(limit - len(selected), 0)])
            if len(selected) < limit:
                selected_pending = sum(1 for _, key in selected if key in {item[1] for item in pending})
                selected.extend(pending[selected_pending:selected_pending + limit - len(selected)])
        else:
            selected.extend((pending + other_items)[:limit])

        pending_keys = {key for _, key in pending}
        new_keys = {key for _, key in new_items}
        monitoring_keys = {key for _, key in monitoring}
        selected_keys = {key for _, key in selected}
        attention_items = []
        for history, key in keyed_histories:
            state_item = state.get(key) or {}
            if state_item.get("status") != self._STATE_ATTENTION:
                continue
            attention_stage = self._attention_stage(state_item)
            if attention_stage == "cleanup":
                reason = "旧名称附件清理失败"
            elif attention_stage == "sidecar":
                reason = "刮削附件多次补齐失败"
            else:
                reason = "多次重命名后路径仍变化"
            target = (
                state_item.get("expected_path")
                or reorganizer.target_path(history)
                or "未知文件"
            )
            attention_items.append(
                f"{reorganizer.display_name(history)}：{reason}｜文件：{target}"
            )
        return (
            [history for history, _ in selected],
            state,
            {
                "pending": sum(1 for _, key in selected if key in pending_keys),
                "new": sum(1 for _, key in selected if key in new_keys),
                "monitoring": sum(1 for _, key in selected if key in monitoring_keys),
                "pending_queued": sum(
                    1 for _, key in pending if key not in selected_keys
                ),
                "new_queued": sum(
                    1 for _, key in new_items if key not in selected_keys
                ),
                "monitoring_queued": sum(
                    1 for _, key in monitoring if key not in selected_keys
                ),
                "scan_waiting": sum(
                    1
                    for _, key in keyed_histories
                    if bool((state.get(key) or {}).get("scan_pending"))
                ),
                "refresh_waiting": len(refresh_waiting),
                "monitoring_waiting": len(monitoring_all) - len(monitoring),
                "sidecar_waiting": len(sidecar_waiting),
                "cleanup_waiting": len(cleanup_waiting),
                "sidecar_waiting_items": [
                    self._waiting_record_detail(
                        history,
                        reorganizer,
                        state.get(key) or {},
                        "sidecar",
                    )
                    for history, key in sidecar_waiting
                ],
                "cleanup_waiting_items": [
                    self._waiting_record_detail(
                        history,
                        reorganizer,
                        state.get(key) or {},
                        "cleanup",
                    )
                    for history, key in cleanup_waiting
                ],
                "complete": sum(
                    1
                    for _, key in keyed_histories
                    if (state.get(key) or {}).get("status") == self._STATE_COMPLETE
                ),
                "attention": len(attention_items),
                "attention_items": attention_items,
            },
        )

    @classmethod
    def _waiting_records_text(cls, selection: dict[str, Any]) -> str:
        waiting_items = []
        if selection.get("refresh_waiting"):
            waiting_items.append(
                f"{selection['refresh_waiting']} 条记录等待刷新确认"
            )
        if selection.get("sidecar_waiting"):
            text = f"{selection['sidecar_waiting']} 条记录等待附件生成"
            details = cls._limited_waiting_details(
                selection.get("sidecar_waiting_items") or []
            )
            waiting_items.append(f"{text}：{details}" if details else text)
        if selection.get("cleanup_waiting"):
            text = f"{selection['cleanup_waiting']} 条记录等待旧附件清理"
            details = cls._limited_waiting_details(
                selection.get("cleanup_waiting_items") or []
            )
            waiting_items.append(f"{text}：{details}" if details else text)
        return f"，另有 {'、'.join(waiting_items)}" if waiting_items else ""

    @classmethod
    def _limited_waiting_details(cls, items: list[str], limit: int = 3) -> str:
        details = [str(item).strip() for item in items if str(item).strip()]
        if not details:
            return ""
        text = "；".join(details[:limit])
        omitted = len(details) - limit
        if omitted > 0:
            text += f"；另有 {omitted} 条"
        return text

    @classmethod
    def _waiting_record_detail(
        cls,
        history: Any,
        reorganizer: MoviePilotReorganizer,
        state_item: dict[str, Any],
        waiting_type: str,
    ) -> str:
        label = reorganizer.display_name(history)
        if waiting_type == "cleanup":
            details = cls._cleanup_waiting_file_details(
                state_item.get("cleanup_old_media_path"),
                state_item.get("old_sidecars") or [],
            )
            return f"{label}｜{details}"
        target = (
            state_item.get("expected_path")
            or reorganizer.target_path(history)
            or "未知文件"
        )
        return f"{label}｜文件：{target}"

    @staticmethod
    def _interleave_queue_items(
        first: list[tuple[Any, str]],
        second: list[tuple[Any, str]],
    ) -> list[tuple[Any, str]]:
        interleaved: list[tuple[Any, str]] = []
        length = max(len(first), len(second))
        for index in range(length):
            if index < len(first):
                interleaved.append(first[index])
            if index < len(second):
                interleaved.append(second[index])
        return interleaved

    def _recover_operation_intents(
        self,
        state: dict[str, dict[str, Any]],
    ) -> int:
        recovered = 0
        for key, value in list(state.items()):
            item = dict(value) if isinstance(value, dict) else {}
            intent = item.get("operation_intent")
            if not isinstance(intent, dict):
                continue
            operation = str(intent.get("operation") or "")
            if operation == "refresh":
                item["status"] = self._STATE_PENDING_REFRESH
                item["had_action"] = True
                item["refresh_check_after"] = self._intent_refresh_check_after(
                    intent.get("prepared_at")
                )
            elif operation == "reorganize":
                item["status"] = self._STATE_PENDING_REORGANIZE
                item["had_action"] = True
                if intent.get("expected_path"):
                    item["expected_path"] = str(intent["expected_path"])
                item["sidecar_pending"] = True
                item["sidecar_attempts"] = int(
                    intent.get("sidecar_attempts") or 1
                )
                item.pop("sidecar_check_after", None)
                if intent.get("cleanup_old_media_path"):
                    item["cleanup_pending"] = True
                    item["cleanup_old_media_path"] = str(
                        intent["cleanup_old_media_path"]
                    )
                    item["old_sidecars"] = [
                        str(path)
                        for path in intent.get("old_sidecars") or []
                    ]
                    item["cleanup_passes"] = int(
                        intent.get("cleanup_passes") or 0
                    )
            elif operation == "cleanup":
                item["status"] = self._STATE_PENDING_REORGANIZE
                item["cleanup_pending"] = True
                item.pop("cleanup_check_after", None)
            elif operation == "scan":
                item["status"] = self._STATE_PENDING_REORGANIZE
                item["scan_pending"] = True
            else:
                item.pop("operation_intent", None)
                state[key] = item
                continue
            item.pop("operation_intent", None)
            item["updated_at"] = datetime.now().isoformat(timespec="seconds")
            state[key] = item
            recovered += 1
        return recovered

    @classmethod
    def _intent_refresh_check_after(cls, prepared_at: Any) -> str:
        try:
            prepared = datetime.fromisoformat(str(prepared_at))
        except (TypeError, ValueError):
            prepared = datetime.now()
        return (
            prepared + timedelta(minutes=cls._REFRESH_RECHECK_MINUTES)
        ).isoformat(timespec="seconds")

    @classmethod
    def _cleanup_waiting_file_details(
        cls,
        old_media_path: Any,
        old_sidecars: list[Any],
    ) -> str:
        sidecars = [
            str(path).strip()
            for path in old_sidecars
            if str(path).strip()
        ]
        text = f"旧文件：{old_media_path or '未知旧文件'}"
        if sidecars:
            text += f"｜旧附件：{cls._limited_waiting_details(sidecars)}"
        return text

    @classmethod
    def _sidecar_recheck_at(cls) -> str:
        return (
            datetime.now() + timedelta(minutes=cls._SIDECAR_RECHECK_MINUTES)
        ).isoformat(timespec="seconds")

    @classmethod
    def _refresh_recheck_at(cls) -> str:
        return (
            datetime.now() + timedelta(minutes=cls._REFRESH_RECHECK_MINUTES)
        ).isoformat(timespec="seconds")

    @staticmethod
    def _timestamp_is_due(value: Any) -> bool:
        if not value:
            return True
        try:
            return datetime.fromisoformat(str(value)) <= datetime.now()
        except (TypeError, ValueError):
            return True

    def _load_processing_state(self) -> dict[str, dict[str, Any]]:
        get_data = getattr(self, "get_data", None)
        if not callable(get_data):
            return {}
        try:
            value = get_data(self._PROCESSING_STATE_KEY)
        except Exception as err:
            raise RuntimeError(f"读取处理状态失败，已停止本轮运行：{err}") from err
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise RuntimeError(
                f"读取处理状态失败，数据格式应为对象，实际为 {type(value).__name__}"
            )
        return value

    @classmethod
    def _tracked_history_ids(
        cls,
        state: dict[str, dict[str, Any]],
    ) -> set[int]:
        tracked: set[int] = set()
        for item in state.values():
            if not isinstance(item, dict) or item.get("status") == cls._STATE_COMPLETE:
                continue
            try:
                history_id = int(item.get("history_id") or 0)
            except (TypeError, ValueError):
                continue
            if history_id > 0:
                tracked.add(history_id)
        return tracked

    def _save_processing_state(self, state: dict[str, dict[str, Any]]) -> bool:
        save_data = getattr(self, "save_data", None)
        if not callable(save_data):
            logger.warning("[最近剧集维护] 当前环境不支持保存处理状态")
            return False
        last_error: Exception | None = None
        for attempt in range(1, self._STATE_SAVE_ATTEMPTS + 1):
            try:
                save_data(self._PROCESSING_STATE_KEY, state)
                return True
            except Exception as err:
                last_error = err
                if attempt >= self._STATE_SAVE_ATTEMPTS:
                    break
                logger.warning(
                    f"[最近剧集维护] 保存处理状态失败，"
                    f"{self._STATE_SAVE_RETRY_SECONDS} 秒后重试"
                    f"（{attempt}/{self._STATE_SAVE_ATTEMPTS}）：{err}"
                )
                sleep(self._STATE_SAVE_RETRY_SECONDS)
        logger.error(f"[最近剧集维护] 保存处理状态失败：{last_error}")
        return False

    def _checkpoint_processing_state(
        self,
        state: dict[str, dict[str, Any]],
    ) -> bool:
        return self._dry_run or self._save_processing_state(state)

    @staticmethod
    def _state_item_snapshots(
        state: dict[str, dict[str, Any]],
        keys: set[str],
    ) -> dict[str, dict[str, Any] | None]:
        return {
            key: dict(state[key]) if isinstance(state.get(key), dict) else None
            for key in keys
        }

    @staticmethod
    def _restore_state_items(
        state: dict[str, dict[str, Any]],
        snapshots: dict[str, dict[str, Any] | None],
    ) -> None:
        for key, item in snapshots.items():
            if item is None:
                state.pop(key, None)
            else:
                state[key] = dict(item)

    @staticmethod
    def _set_operation_intent(
        state: dict[str, dict[str, Any]],
        keys: set[str],
        operation: str,
        **details: Any,
    ) -> None:
        prepared_at = datetime.now().isoformat(timespec="seconds")
        for key in keys:
            item = dict(state.get(key) or {})
            item["operation_intent"] = {
                "operation": operation,
                "prepared_at": prepared_at,
                **{
                    name: value
                    for name, value in details.items()
                    if value is not None
                },
            }
            state[key] = item

    @staticmethod
    def _clear_operation_intent(
        state: dict[str, dict[str, Any]],
        keys: set[str],
    ) -> None:
        for key in keys:
            item = dict(state.get(key) or {})
            item.pop("operation_intent", None)
            state[key] = item

    @classmethod
    def _protected_history_ids(
        cls,
        state: dict[str, dict[str, Any]],
    ) -> set[int]:
        protected: set[int] = set()
        for item in state.values():
            if not isinstance(item, dict):
                continue
            if not any(
                (
                    item.get("sidecar_pending"),
                    item.get("cleanup_pending"),
                    item.get("scan_pending"),
                    item.get("operation_intent"),
                )
            ):
                continue
            try:
                history_id = int(item.get("history_id") or 0)
            except (TypeError, ValueError):
                continue
            if history_id > 0:
                protected.add(history_id)
        return protected

    def _retry_attention_records(self) -> int:
        try:
            state = self._load_processing_state()
        except Exception as err:
            logger.error(f"[最近剧集维护] 无法重新处理人工检查记录：{err}")
            return 0
        retried = 0
        for key, value in list(state.items()):
            item = dict(value) if isinstance(value, dict) else {}
            if item.get("status") != self._STATE_ATTENTION:
                continue
            attention_stage = self._attention_stage(item)
            if attention_stage == "cleanup":
                item["status"] = self._STATE_PENDING_REORGANIZE
                item["cleanup_passes"] = 0
                item.pop("cleanup_check_after", None)
            elif attention_stage == "sidecar":
                item["status"] = self._STATE_PENDING_REORGANIZE
                item["sidecar_attempts"] = 0
                item.pop("sidecar_check_after", None)
            else:
                item["status"] = (
                    self._STATE_PENDING_REFRESH
                    if self._enable_refresh
                    else self._STATE_PENDING_REORGANIZE
                )
                item["rename_attempts"] = 0
            item["updated_at"] = datetime.now().isoformat(timespec="seconds")
            item.pop("operation_intent", None)
            item.pop("attention_stage", None)
            state[key] = item
            retried += 1
        if not retried:
            logger.info("[最近剧集维护] 当前没有需人工检查的记录")
            return 0
        if not self._save_processing_state(state):
            logger.error("[最近剧集维护] 人工检查记录重新入队失败，未执行立即运行")
            return 0
        logger.info(
            f"[最近剧集维护] 已将 {retried} 条需人工检查记录重新加入处理队列"
        )
        return retried

    @staticmethod
    def _attention_stage(item: dict[str, Any]) -> str:
        stage = str(item.get("attention_stage") or "")
        if stage in {"sidecar", "cleanup", "rename"}:
            return stage
        if item.get("sidecar_pending"):
            return "sidecar"
        if item.get("cleanup_pending"):
            return "cleanup"
        return "rename"

    def _finish_run(
        self,
        result: RunResult,
        processing_state: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        if processing_state is not None:
            self._refresh_result_queue_counts(result, processing_state)
        if (
            processing_state is not None
            and not self._dry_run
            and not self._save_processing_state(processing_state)
        ):
            result.add_error("保存处理状态失败，本轮后续运行可能需要恢复")
        logger.info("[最近剧集维护] 运行完成\n" + result.summary())
        if self._notify and result.should_notify():
            self._post_message("最近剧集维护完成", result.summary())

    def _refresh_result_queue_counts(
        self,
        result: RunResult,
        state: dict[str, dict[str, Any]],
    ) -> None:
        if not result.queue_counts:
            return
        counts = {
            "pending_queued": 0,
            "new_queued": 0,
            "monitoring_queued": 0,
            "scan_waiting": 0,
            "refresh_waiting": 0,
            "monitoring_waiting": 0,
            "sidecar_waiting": 0,
            "cleanup_waiting": 0,
            "complete": 0,
            "attention": 0,
        }
        pending_statuses = {
            self._STATE_PENDING_REFRESH,
            self._STATE_PENDING_REORGANIZE,
        }
        for value in state.values():
            item = value if isinstance(value, dict) else {}
            status = item.get("status")
            if status == self._STATE_COMPLETE:
                counts["complete"] += 1
            elif status == self._STATE_ATTENTION:
                counts["attention"] += 1
            elif item.get("scan_pending"):
                counts["scan_waiting"] += 1
            elif item.get("sidecar_pending"):
                counts["sidecar_waiting"] += 1
            elif item.get("cleanup_pending"):
                counts["cleanup_waiting"] += 1
            elif (
                status == self._STATE_PENDING_REFRESH
                and not self._timestamp_is_due(item.get("refresh_check_after"))
            ):
                counts["refresh_waiting"] += 1
            elif (
                status == self._STATE_MONITORING
                and not self._timestamp_is_due(item.get("next_preview_at"))
            ):
                counts["monitoring_waiting"] += 1
            elif status in pending_statuses:
                counts["pending_queued"] += 1
            elif status == self._STATE_MONITORING:
                counts["monitoring_queued"] += 1
            else:
                counts["new_queued"] += 1
        result.queue_counts.update(counts)

    @staticmethod
    def _history_keys_for_episode(
        item_id: str,
        episode_target_keys: dict[str, set[str]],
        history_keys_by_target: dict[str, set[str]],
    ) -> set[str]:
        return {
            history_key
            for target_key in episode_target_keys.get(item_id) or set()
            for history_key in history_keys_by_target.get(target_key) or set()
        }

    @staticmethod
    def _expected_path_for_keys(
        keys: set[str],
        expected_paths: dict[str, Path],
    ) -> Path | None:
        return next(
            (expected_paths[key] for key in sorted(keys) if key in expected_paths),
            None,
        )

    @staticmethod
    def _same_path(left: Any, right: Any) -> bool:
        return JellyfinServiceClient.path_key(left) == JellyfinServiceClient.path_key(right)

    @staticmethod
    def _cache_processing_preview(
        state: dict[str, dict[str, Any]],
        keys: set[str],
        expected_path: Any,
    ) -> None:
        previewed_at = datetime.now().isoformat(timespec="seconds")
        for key in keys:
            item = dict(state.get(key) or {})
            item["expected_path"] = str(expected_path)
            item["previewed_at"] = previewed_at
            if not EpisodeItem.path_title_is_placeholder(expected_path):
                item.pop("placeholder_refresh_done", None)
            state[key] = item

    @classmethod
    def _placeholder_refresh_completed(
        cls,
        state: dict[str, dict[str, Any]],
        keys: set[str],
    ) -> bool:
        for key in keys:
            item = state.get(key) or {}
            if item.get("placeholder_refresh_done"):
                return True
            if (
                item.get("status") == cls._STATE_PENDING_REFRESH
                and item.get("had_action")
                and EpisodeItem.path_title_is_placeholder(
                    item.get("expected_path")
                )
            ):
                return True
        return False

    def _mark_processing_verified(
        self,
        state: dict[str, dict[str, Any]],
        keys: set[str],
    ) -> None:
        for key in keys:
            state_item = state.get(key) or {}
            if bool(state_item.get("scan_pending")):
                self._mark_processing_state(
                    state,
                    {key},
                    self._STATE_PENDING_REORGANIZE,
                )
            elif bool(state_item.get("cleanup_pending")):
                self._mark_processing_state(
                    state,
                    {key},
                    self._STATE_PENDING_REORGANIZE,
                )
            elif bool(state_item.get("had_action")):
                self._mark_processing_state(state, {key}, self._STATE_COMPLETE)
            elif self._history_window_expired(state_item):
                self._mark_processing_state(state, {key}, self._STATE_COMPLETE)
            else:
                monitoring_checks = int(state_item.get("monitoring_checks") or 0) + 1
                interval_index = min(
                    monitoring_checks - 1,
                    len(self._MONITOR_INTERVAL_HOURS) - 1,
                )
                next_preview_at = (
                    datetime.now()
                    + timedelta(hours=self._MONITOR_INTERVAL_HOURS[interval_index])
                ).isoformat(timespec="seconds")
                self._mark_processing_state(
                    state,
                    {key},
                    self._STATE_MONITORING,
                    next_preview_at=next_preview_at,
                    monitoring_checks=monitoring_checks,
                )

    def _mark_processing_waiting_for_preview_title(
        self,
        state: dict[str, dict[str, Any]],
        keys: set[str],
        *,
        placeholder_refresh_done: bool,
    ) -> bool:
        completed = bool(keys)
        for key in keys:
            state_item = state.get(key) or {}
            if self._history_window_expired(state_item):
                self._mark_processing_state(
                    state,
                    {key},
                    self._STATE_COMPLETE,
                    placeholder_refresh_done=placeholder_refresh_done,
                    completion_reason="placeholder_title_expired",
                )
                continue
            completed = False
            monitoring_checks = int(state_item.get("monitoring_checks") or 0) + 1
            interval_index = min(
                monitoring_checks - 1,
                len(self._MONITOR_INTERVAL_HOURS) - 1,
            )
            next_preview_at = (
                datetime.now()
                + timedelta(hours=self._MONITOR_INTERVAL_HOURS[interval_index])
            ).isoformat(timespec="seconds")
            self._mark_processing_state(
                state,
                {key},
                self._STATE_MONITORING,
                next_preview_at=next_preview_at,
                monitoring_checks=monitoring_checks,
                placeholder_refresh_done=placeholder_refresh_done,
            )
        return completed

    def _history_window_expired(self, state_item: dict[str, Any]) -> bool:
        history_date = state_item.get("history_date")
        if not history_date:
            return False
        try:
            organized_at = datetime.fromisoformat(str(history_date))
            now = datetime.now(tz=organized_at.tzinfo)
        except (TypeError, ValueError):
            return False
        return organized_at + timedelta(days=max(int(self._days), 0)) <= now

    @staticmethod
    def _mark_processing_state(
        state: dict[str, dict[str, Any]],
        keys: set[str],
        status: str,
        rename_attempts: int | None = None,
        had_action: bool | None = None,
        expected_path: Any = None,
        next_preview_at: str | None = None,
        monitoring_checks: int | None = None,
        refresh_check_after: str | None = None,
        sidecar_pending: bool | None = None,
        sidecar_attempts: int | None = None,
        sidecar_check_after: str | None = None,
        cleanup_pending: bool | None = None,
        old_sidecars: list[str] | None = None,
        cleanup_old_media_path: Any = None,
        cleanup_check_after: str | None = None,
        cleanup_passes: int | None = None,
        attention_stage: str | None = None,
        placeholder_refresh_done: bool | None = None,
        completion_reason: str | None = None,
    ) -> None:
        updated_at = datetime.now().isoformat(timespec="seconds")
        for key in keys:
            item = dict(state.get(key) or {})
            item["status"] = status
            item["updated_at"] = updated_at
            if rename_attempts is not None:
                item["rename_attempts"] = rename_attempts
            if had_action is not None:
                item["had_action"] = had_action
            if expected_path is not None:
                item["expected_path"] = str(expected_path)
                item["previewed_at"] = updated_at
            if next_preview_at is not None:
                item["next_preview_at"] = next_preview_at
            elif status != RecentEpisodeMaintenance._STATE_MONITORING:
                item.pop("next_preview_at", None)
            if monitoring_checks is not None:
                item["monitoring_checks"] = monitoring_checks
            if had_action is True:
                item.pop("monitoring_checks", None)
            if refresh_check_after is not None:
                item["refresh_check_after"] = refresh_check_after
            elif status != RecentEpisodeMaintenance._STATE_PENDING_REFRESH:
                item.pop("refresh_check_after", None)
            if sidecar_pending is not None:
                item["sidecar_pending"] = sidecar_pending
            if sidecar_attempts is not None:
                item["sidecar_attempts"] = sidecar_attempts
            if sidecar_check_after is not None:
                item["sidecar_check_after"] = sidecar_check_after
            if sidecar_pending is False:
                item.pop("sidecar_check_after", None)
            if cleanup_pending is True:
                item["cleanup_pending"] = True
            if old_sidecars is not None:
                item["old_sidecars"] = [str(path) for path in old_sidecars]
            if cleanup_old_media_path is not None:
                item["cleanup_old_media_path"] = str(cleanup_old_media_path)
            if cleanup_check_after is not None:
                item["cleanup_check_after"] = cleanup_check_after
            if cleanup_passes is not None:
                item["cleanup_passes"] = cleanup_passes
            if placeholder_refresh_done is not None:
                item["placeholder_refresh_done"] = placeholder_refresh_done
            if status == RecentEpisodeMaintenance._STATE_COMPLETE:
                if completion_reason:
                    item["completion_reason"] = completion_reason
                else:
                    item.pop("completion_reason", None)
            else:
                item.pop("completion_reason", None)
            if status == RecentEpisodeMaintenance._STATE_ATTENTION:
                item["attention_stage"] = (
                    attention_stage
                    or RecentEpisodeMaintenance._attention_stage(item)
                )
            else:
                item.pop("attention_stage", None)
            if cleanup_pending is False:
                for cleanup_key in (
                    "cleanup_pending",
                    "old_sidecars",
                    "cleanup_old_media_path",
                    "cleanup_check_after",
                    "cleanup_passes",
                    "scan_pending",
                ):
                    item.pop(cleanup_key, None)
            if status in {
                RecentEpisodeMaintenance._STATE_COMPLETE,
                RecentEpisodeMaintenance._STATE_MONITORING,
            }:
                for finished_key in (
                    "sidecar_pending",
                    "sidecar_attempts",
                    "sidecar_check_after",
                    "cleanup_pending",
                    "old_sidecars",
                    "cleanup_old_media_path",
                    "cleanup_check_after",
                    "cleanup_passes",
                ):
                    item.pop(finished_key, None)
                if status == RecentEpisodeMaintenance._STATE_COMPLETE:
                    item.pop("monitoring_checks", None)
            elif status == RecentEpisodeMaintenance._STATE_ATTENTION:
                item.pop("sidecar_check_after", None)
                item.pop("cleanup_check_after", None)
            state[key] = item

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
            return options + client.libraries(retry=False)
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
