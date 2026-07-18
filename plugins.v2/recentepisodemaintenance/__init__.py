from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
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
from .models import RunResult
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
    _SIDECAR_POLL_SECONDS = 3
    _PREVIEW_CACHE_HOURS = 24
    _INSPECTION_MULTIPLIER = 5
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
    plugin_icon = "https://raw.githubusercontent.com/byangmath/RecentEpisodeMaintenance/main/icons/recentepisodemaintenance.png"
    plugin_version = "0.1.1"
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
                                "限制每轮刷新和重新整理的合计操作数；最多检查该数量五倍的记录",
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
        try:
            history_pool = reorganizer.recent_histories(days=self._days)
            histories, processing_state, selection = self._select_histories(
                histories=history_pool,
                reorganizer=reorganizer,
            )
            result.reorganize_candidates = len(histories)
            logger.info(
                f"[最近剧集维护] 查询 MP 最近 {self._days} 天成功整理记录，"
                f"共 {len(history_pool)} 条；本轮最多执行 {operation_limit} 次操作，"
                f"检查 {len(histories)} 条记录"
                f"（待复查 {selection['pending']} 条，新记录 {selection['new']} 条，"
                f"到期监测 {selection['monitoring']} 条，"
                f"已完成 {selection['complete']} 条，需人工检查 {selection['attention']} 条）"
            )
        except Exception as err:
            logger.error(f"[最近剧集维护] 查询 MP 整理历史失败：{err}")
            return

        if not histories:
            if not self._dry_run:
                self._save_processing_state(processing_state)
            logger.info("[最近剧集维护] 当前时间范围内没有待处理的整理记录")
            return

        client: JellyfinServiceClient | None = None
        deferred_reorganize_targets: set[str] = set()
        refresh_first_targets: set[str] = set()
        history_keys_by_target: dict[str, set[str]] = {}
        expected_paths: dict[str, Path] = {}
        preview_failed_keys: set[str] = set()
        for history in histories:
            processing_key = reorganizer.processing_key(history)
            target_key = JellyfinServiceClient.path_key(reorganizer.target_path(history))
            history_keys_by_target.setdefault(target_key, set()).add(processing_key)

            state_item = processing_state.get(processing_key) or {}
            status = state_item.get("status")
            cached_path = state_item.get("expected_path")
            if cached_path and status in {
                self._STATE_PENDING_REFRESH,
                self._STATE_PENDING_REORGANIZE,
            }:
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
            message = f"{reorganizer.display_name(history)}：{preview.message}"
            result.add_error(message)
            logger.error(
                f"[最近剧集维护] 获取 MP 整理预览失败 {message}｜"
                f"文件：{reorganizer.target_path(history) or '未知文件'}"
            )
            if not self._dry_run:
                self._mark_processing_state(
                    processing_state,
                    {processing_key},
                    self._STATE_PENDING_REFRESH
                    if self._enable_refresh
                    else self._STATE_PENDING_REORGANIZE,
                )

        if self._enable_refresh:
            client = self._get_jellyfin_client()
            if client:
                targets = [
                    target
                    for history in histories
                    if reorganizer.processing_key(history) not in preview_failed_keys
                    and (target := reorganizer.episode_target(history)) is not None
                ]
                try:
                    matches = client.match_recent_episodes(
                        targets=targets,
                        days=self._days,
                        library_ids=self._library_ids,
                    )
                except Exception as err:
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
                        result.skipped += 1
                        unmatched_histories.append(reorganizer.display_name(history))
                        deferred_reorganize_targets.add(target_key)
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
                    comparison_details = (
                        f"{episode.display_name}｜MP 预览文件："
                        f"{expected_path.name if expected_path else '未知文件'}"
                    )
                    if not expected_path:
                        result.add_error(f"{episode.display_name}：缺少 MP 整理预览")
                        deferred_reorganize_targets.update(
                            episode_target_keys.get(episode.item_id) or set()
                        )
                        if not self._dry_run:
                            self._mark_processing_state(
                                processing_state,
                                episode_history_keys,
                                self._STATE_PENDING_REFRESH,
                            )
                        logger.error(
                            f"[最近剧集维护] 缺少 MP 整理预览，无法判断是否刷新 "
                            f"{comparison_details}"
                        )
                        continue

                    if episode.title_matches_path(expected_path):
                        result.skipped += 1
                        prefix = "试运行：" if self._dry_run else ""
                        logger.info(
                            f"[最近剧集维护] {prefix}标题一致，跳过元数据刷新 "
                            f"{comparison_details}"
                        )
                        if not self._enable_reorganize and not self._dry_run:
                            self._mark_processing_verified(
                                processing_state,
                                episode_history_keys,
                            )
                        continue
                    deferred_reorganize_targets.update(
                        episode_target_keys.get(episode.item_id) or set()
                    )
                    refresh_first_targets.update(
                        episode_target_keys.get(episode.item_id) or set()
                    )
                    if result.operations_used >= result.operation_limit:
                        result.skipped += 1
                        if not self._dry_run:
                            self._mark_processing_state(
                                processing_state,
                                episode_history_keys,
                                self._STATE_PENDING_REFRESH,
                            )
                        logger.info(
                            f"[最近剧集维护] 已达到单次操作上限，延后元数据刷新 "
                            f"{comparison_details}"
                        )
                        continue
                    result.operations_used += 1
                    if self._dry_run:
                        result.refresh_previewed += 1
                        logger.info(
                            f"[最近剧集维护] 试运行：标题不一致，将完整刷新 "
                            f"{comparison_details}"
                        )
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
                        result.add_refreshed_title(
                            self._result_title(episode.display_name, expected_path)
                        )
                        logger.info(
                            f"[最近剧集维护] 标题不一致，已提交元数据和图片刷新 "
                            f"{comparison_details}"
                        )
                        self._mark_processing_state(
                            processing_state,
                            episode_history_keys,
                            self._STATE_PENDING_REFRESH,
                            had_action=True,
                        )
                    except Exception as err:
                        self._mark_processing_state(
                            processing_state,
                            episode_history_keys,
                            self._STATE_PENDING_REFRESH,
                        )
                        result.add_error(f"{episode.display_name}：{err}")
                        logger.error(
                            f"[最近剧集维护] 标题不一致，提交元数据刷新失败 "
                            f"{comparison_details}：{err}"
                        )
            else:
                result.add_error("未找到可用的 Jellyfin 媒体服务器")
                for history in histories:
                    target_key = JellyfinServiceClient.path_key(reorganizer.target_path(history))
                    deferred_reorganize_targets.add(target_key)
                    if not self._dry_run:
                        self._mark_processing_state(
                            processing_state,
                            {reorganizer.processing_key(history)},
                            self._STATE_PENDING_REFRESH,
                        )

        reorganized_sidecars: list[dict[str, Any]] = []
        sidecars_ready_for_scan = False
        if self._enable_reorganize:
            for history in histories:
                label = reorganizer.display_name(history)
                processing_key = reorganizer.processing_key(history)
                current_file = reorganizer.target_path(history)
                target_key = JellyfinServiceClient.path_key(current_file)
                expected_path = expected_paths.get(processing_key)
                state_hint = processing_state.get(processing_key) or {}
                sidecar_pending_hint = bool(state_hint.get("sidecar_pending"))
                if (
                    not sidecar_pending_hint
                    and expected_path
                    and state_hint.get("status") == self._STATE_PENDING_REORGANIZE
                    and bool(state_hint.get("had_action"))
                    and self._same_path(current_file, expected_path)
                    and self._missing_reorganized_sidecars(expected_path)
                ):
                    sidecar_pending_hint = True
                    if not self._dry_run:
                        self._mark_processing_state(
                            processing_state,
                            {processing_key},
                            self._STATE_PENDING_REORGANIZE,
                            sidecar_pending=True,
                            sidecar_attempts=0,
                        )
                        logger.info(
                            f"[最近剧集维护] 检测到既有重新整理记录附件不完整，"
                            f"将补充刮削 {label}｜文件：{Path(expected_path).name}"
                        )
                if (
                    target_key in deferred_reorganize_targets
                    and not (
                        sidecar_pending_hint
                        and target_key not in refresh_first_targets
                    )
                ):
                    result.skipped += 1
                    prefix = "试运行：" if self._dry_run else ""
                    logger.info(
                        f"[最近剧集维护] {prefix}本轮暂缓重新整理 {label}，"
                        f"待后续运行确认元数据刷新或媒体匹配结果｜"
                        f"文件：{current_file or '未知文件'}"
                    )
                    continue
                if not expected_path:
                    result.add_error(f"{label}：缺少 MP 整理预览")
                    if not self._dry_run:
                        self._mark_processing_state(
                            processing_state,
                            {processing_key},
                            self._STATE_PENDING_REORGANIZE,
                        )
                    logger.error(
                        f"[最近剧集维护] 缺少 MP 整理预览，无法安全重新整理 {label}｜"
                        f"文件：{current_file or '未知文件'}"
                    )
                    continue
                try:
                    state_item = processing_state.get(processing_key) or {}
                    rename_attempts = int(state_item.get("rename_attempts") or 0)
                    sidecar_pending = bool(state_item.get("sidecar_pending"))
                    sidecar_attempts = int(state_item.get("sidecar_attempts") or 0)
                    same_path = self._same_path(current_file, expected_path)
                    if sidecar_pending and not self._missing_reorganized_sidecars(expected_path):
                        sidecar_pending = False
                        sidecars_ready_for_scan = True
                        self._mark_processing_state(
                            processing_state,
                            {processing_key},
                            self._STATE_PENDING_REORGANIZE,
                            sidecar_pending=False,
                            sidecar_attempts=sidecar_attempts,
                        )
                        logger.info(
                            f"[最近剧集维护] 重新整理附件已补齐 {label}｜"
                            f"文件：{Path(expected_path).name}"
                        )
                    elif (
                        not sidecar_pending
                        and state_item.get("status") == self._STATE_PENDING_REORGANIZE
                        and bool(state_item.get("had_action"))
                        and same_path
                        and self._missing_reorganized_sidecars(expected_path)
                    ):
                        sidecar_pending = True
                        sidecar_attempts = 0
                        logger.info(
                            f"[最近剧集维护] 检测到既有重新整理记录附件不完整，"
                            f"将补充刮削 {label}｜文件：{Path(expected_path).name}"
                        )
                    if sidecar_pending and sidecar_attempts >= self._MAX_SIDECAR_ATTEMPTS:
                        message = (
                            f"{label} 已连续触发刮削 {sidecar_attempts} 次，"
                            "NFO 或图片仍未生成，已停止自动重试；请检查 MoviePilot 刮削日志"
                        )
                        result.add_error(message)
                        if not self._dry_run:
                            self._mark_processing_state(
                                processing_state,
                                {processing_key},
                                self._STATE_ATTENTION,
                            )
                        logger.error(f"[最近剧集维护] {message}")
                        continue

                    if (
                        self._skip_same_name
                        and not sidecar_pending
                        and same_path
                    ):
                        result.skipped += 1
                        if not self._dry_run:
                            self._mark_processing_verified(
                                processing_state,
                                {processing_key},
                            )
                        prefix = "试运行：" if self._dry_run else ""
                        logger.info(
                            f"[最近剧集维护] {prefix}跳过 {label}：按当前命名规则预览，"
                            f"路径未变化｜{self._file_change_details(current_file, expected_path)}"
                        )
                        continue

                    if self._dry_run:
                        if result.operations_used >= result.operation_limit:
                            result.skipped += 1
                            logger.info(
                                f"[最近剧集维护] 试运行：已达到单次操作上限，"
                                f"延后重新整理 {label}｜"
                                f"{self._file_change_details(current_file, expected_path)}"
                            )
                            continue
                        result.operations_used += 1
                        result.previewed += 1
                        if sidecar_pending:
                            logger.info(
                                f"[最近剧集维护] 试运行：{label} 将重试重新整理并补齐刮削附件｜"
                                f"文件：{Path(expected_path).name}"
                            )
                        else:
                            logger.info(
                                f"[最近剧集维护] 试运行：{label} 预计重新命名｜"
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
                            result.skipped += 1
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
                            )
                            result.add_error(message)
                            logger.error(f"[最近剧集维护] {message}")
                        else:
                            self._mark_processing_state(
                                processing_state,
                                {processing_key},
                                self._STATE_PENDING_REORGANIZE,
                            )
                            result.add_error(f"{label}：{preview.message}")
                            logger.error(
                                f"[最近剧集维护] 重新整理预览失败 {label}：{preview.message}｜"
                                f"{self._file_change_details(current_file, preview.target)}"
                            )
                        continue
                    if result.operations_used >= result.operation_limit:
                        result.skipped += 1
                        self._mark_processing_state(
                            processing_state,
                            {processing_key},
                            self._STATE_PENDING_REORGANIZE,
                        )
                        logger.info(
                            f"[最近剧集维护] 已达到单次操作上限，延后重新整理 {label}｜"
                            f"{self._file_change_details(current_file, expected_path)}"
                        )
                        continue
                    result.operations_used += 1
                    operation = reorganizer.reorganize(
                        history=history,
                        skip_same_name=False if sidecar_pending else self._skip_same_name,
                    )
                    if operation.success:
                        if self._dry_run:
                            result.previewed += 1
                        else:
                            target = operation.target or expected_path or current_file
                            next_sidecar_attempts = sidecar_attempts + 1 if sidecar_pending else 1
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
                            )
                            reorganized_sidecars.append({
                                "key": processing_key,
                                "label": label,
                                "target": Path(target) if target else None,
                                "attempts": next_sidecar_attempts,
                            })
                            logger.info(
                                f"[最近剧集维护] 已提交重新整理并等待刮削附件 {label}｜"
                                f"{self._file_change_details(current_file, target)}"
                            )
                    elif operation.skipped:
                        result.skipped += 1
                        if not self._dry_run:
                            self._mark_processing_verified(
                                processing_state,
                                {processing_key},
                            )
                        logger.info(
                            f"[最近剧集维护] 跳过 {label}：{operation.message}｜"
                            f"{self._file_change_details(current_file, operation.target)}"
                        )
                    else:
                        if not self._dry_run:
                            self._mark_processing_state(
                                processing_state,
                                {processing_key},
                                self._STATE_PENDING_REORGANIZE,
                            )
                        result.add_error(f"{label}：{operation.message}")
                        logger.error(
                            f"[最近剧集维护] 重新整理失败 {label}：{operation.message}｜"
                            f"{self._file_change_details(current_file, operation.target)}"
                        )
                except Exception as err:
                    if not self._dry_run:
                        self._mark_processing_state(
                            processing_state,
                            {processing_key},
                            self._STATE_PENDING_REORGANIZE,
                        )
                    result.add_error(f"{label}：{err}")
                    logger.error(
                        f"[最近剧集维护] 重新整理失败 {label}：{err}｜"
                        f"文件：{current_file or '未知文件'}"
                    )

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
                    result.add_reorganized_title(self._result_title(label, target))
                    self._mark_processing_state(
                        processing_state,
                        {processing_key},
                        self._STATE_PENDING_REORGANIZE,
                        sidecar_pending=False,
                        sidecar_attempts=attempts,
                    )
                    logger.info(
                        f"[最近剧集维护] 重新整理及刮削附件已完成 {label}｜"
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
                )
                if exhausted:
                    message = (
                        f"{label}：重新整理后仍缺少{missing_text}，已连续尝试 {attempts} 次，"
                        "请检查 MoviePilot 刮削日志"
                    )
                else:
                    message = (
                        f"{label}：重新整理后缺少{missing_text}，"
                        f"下次运行将重试（{attempts}/{self._MAX_SIDECAR_ATTEMPTS}）"
                    )
                result.add_error(message)
                logger.error(f"[最近剧集维护] {message}")

        sidecars_complete = (
            bool(reorganized_sidecars) or sidecars_ready_for_scan
        ) and not missing_sidecars
        if sidecars_complete and self._scan_after_reorganize and not self._dry_run:
            client = client or self._get_jellyfin_client()
            try:
                if client:
                    client.scan_library()
                    logger.info("[最近剧集维护] 已触发 Jellyfin 媒体库扫描")
                else:
                    result.add_error("重新整理后无法触发 Jellyfin 媒体库扫描")
            except Exception as err:
                result.add_error(f"媒体库扫描失败：{err}")
        elif missing_sidecars and self._scan_after_reorganize and not self._dry_run:
            logger.warning(
                "[最近剧集维护] 部分重新整理文件的 NFO 或图片尚未生成，"
                "本轮不触发 Jellyfin 媒体库扫描"
            )

        if not self._dry_run:
            self._save_processing_state(processing_state)

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
            for key, item in list(pending.items()):
                missing = cls._missing_reorganized_sidecars(item.get("target"))
                if missing:
                    missing_by_key[key] = missing
                    continue
                missing_by_key.pop(key, None)
                pending.pop(key, None)

            remaining = deadline - monotonic()
            if not pending or remaining <= 0:
                break
            sleep(min(cls._SIDECAR_POLL_SECONDS, remaining))

        return {
            key: missing_by_key.get(
                key,
                cls._missing_reorganized_sidecars(item.get("target")),
            )
            for key, item in pending.items()
        }

    @staticmethod
    def _missing_reorganized_sidecars(target: Any) -> list[str]:
        if not target:
            return ["目标文件", "NFO", "图片"]

        media_path = Path(target)
        missing: list[str] = []
        try:
            if not media_path.is_file():
                missing.append("目标文件")
            if not media_path.with_suffix(".nfo").is_file():
                missing.append("NFO")

            image_extensions = (".jpg", ".jpeg", ".png", ".webp", ".avif")
            image_paths = [media_path.with_suffix(extension) for extension in image_extensions]
            image_paths.extend(
                media_path.parent / f"{media_path.stem}-thumb{extension}"
                for extension in image_extensions
            )
            if not any(path.is_file() for path in image_paths):
                missing.append("图片")
        except OSError:
            return ["目标文件", "NFO", "图片"]
        return missing

    @staticmethod
    def _file_change_details(current_file: Any, new_file: Any) -> str:
        current = str(current_file or "未知文件")
        target = str(new_file or current_file or "未知文件")
        if JellyfinServiceClient.path_key(current) == JellyfinServiceClient.path_key(target):
            return f"文件：{current}"
        return f"原文件：{current}｜新文件：{target}"

    @staticmethod
    def _result_title(fallback: str, path: Any) -> str:
        return Path(path).stem if path else fallback

    def _select_histories(
        self,
        histories: list[Any],
        reorganizer: MoviePilotReorganizer,
    ) -> tuple[list[Any], dict[str, dict[str, Any]], dict[str, int]]:
        state = self._load_processing_state()
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
        pending = [
            (history, key)
            for history, key in keyed_histories
            if (state.get(key) or {}).get("status") in pending_statuses
        ]
        pending.sort(key=lambda item: str((state.get(item[1]) or {}).get("updated_at") or ""))
        new_items = [
            (history, key)
            for history, key in keyed_histories
            if key not in state
            or (state.get(key) or {}).get("status") not in known_statuses
        ]
        monitoring = [
            (history, key)
            for history, key in keyed_histories
            if (state.get(key) or {}).get("status") == self._STATE_MONITORING
            and self._timestamp_is_due((state.get(key) or {}).get("next_preview_at"))
        ]
        monitoring.sort(
            key=lambda item: str((state.get(item[1]) or {}).get("next_preview_at") or "")
        )

        operation_limit = max(int(self._max_items), 1)
        limit = max(operation_limit, operation_limit * self._INSPECTION_MULTIPLIER)
        selected: list[tuple[Any, str]] = []
        other_items = new_items + monitoring
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
        return (
            [history for history, _ in selected],
            state,
            {
                "pending": sum(1 for _, key in selected if key in pending_keys),
                "new": sum(1 for _, key in selected if key in new_keys),
                "monitoring": sum(1 for _, key in selected if key in monitoring_keys),
                "complete": sum(
                    1
                    for _, key in keyed_histories
                    if (state.get(key) or {}).get("status") == self._STATE_COMPLETE
                ),
                "attention": sum(
                    1
                    for _, key in keyed_histories
                    if (state.get(key) or {}).get("status") == self._STATE_ATTENTION
                ),
            },
        )

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
            logger.warning(f"[最近剧集维护] 读取处理状态失败，将重新建立队列：{err}")
            return {}
        return value if isinstance(value, dict) else {}

    def _save_processing_state(self, state: dict[str, dict[str, Any]]) -> None:
        save_data = getattr(self, "save_data", None)
        if not callable(save_data):
            logger.warning("[最近剧集维护] 当前环境不支持保存处理状态")
            return
        try:
            save_data(self._PROCESSING_STATE_KEY, state)
        except Exception as err:
            logger.error(f"[最近剧集维护] 保存处理状态失败：{err}")

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
            state[key] = item

    def _mark_processing_verified(
        self,
        state: dict[str, dict[str, Any]],
        keys: set[str],
    ) -> None:
        next_preview_at = (
            datetime.now() + timedelta(hours=self._PREVIEW_CACHE_HOURS)
        ).isoformat(timespec="seconds")
        for key in keys:
            if bool((state.get(key) or {}).get("had_action")):
                self._mark_processing_state(state, {key}, self._STATE_COMPLETE)
            else:
                self._mark_processing_state(
                    state,
                    {key},
                    self._STATE_MONITORING,
                    next_preview_at=next_preview_at,
                )

    @staticmethod
    def _mark_processing_state(
        state: dict[str, dict[str, Any]],
        keys: set[str],
        status: str,
        rename_attempts: int | None = None,
        had_action: bool | None = None,
        expected_path: Any = None,
        next_preview_at: str | None = None,
        sidecar_pending: bool | None = None,
        sidecar_attempts: int | None = None,
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
            if sidecar_pending is not None:
                item["sidecar_pending"] = sidecar_pending
            if sidecar_attempts is not None:
                item["sidecar_attempts"] = sidecar_attempts
            if status in {
                RecentEpisodeMaintenance._STATE_COMPLETE,
                RecentEpisodeMaintenance._STATE_MONITORING,
            }:
                item.pop("sidecar_pending", None)
                item.pop("sidecar_attempts", None)
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
