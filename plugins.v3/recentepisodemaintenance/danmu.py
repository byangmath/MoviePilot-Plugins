from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import quote, urlsplit, urlunsplit
import unicodedata
from xml.etree import ElementTree

import requests

from .jellyfin_client import DanmuSubmissionError, JellyfinServiceClient
from .models import EpisodeTarget, RunResult


class DanmuMatchError(ValueError):
    """An ambiguous or incompatible match must not be automatically applied."""


class DanmuUnavailableError(RuntimeError):
    """A successful service response explicitly has no match or no comments."""


class LogVarRequestError(RuntimeError):
    """Safe service failure text, without leaking the token-bearing request URL."""

    def __init__(self, message: str, *, stop_immediately: bool = False):
        super().__init__(message)
        self.stop_immediately = stop_immediately


def safe_error(error: Exception) -> str:
    message = re.sub(r"https?://[^\s]+", "[地址已隐藏]", str(error))
    message = re.sub(r"\b(Bearer|Basic)\s+[^\s,;'\"]+", r"\1 [已隐藏]", message, flags=re.I)
    return re.sub(r"\b(api_key|apikey|access_token|token|authorization)\s*[:=]\s*[^\s,;'\"]+",
                  r"\1=[已隐藏]", message, flags=re.I)


def api_base(value: str) -> str:
    parts = urlsplit(str(value or "").strip())
    if (parts.scheme not in {"http", "https"} or not parts.netloc
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError("请填写有效的 LogVar 服务基础地址，可带 Token 路径，不要附加 API 接口或查询参数")
    if "/api/" in parts.path:
        raise ValueError("LogVar 地址只填写服务基础地址，不要附加 /api/v2 等接口路径")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def title_key(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def content_title(value: str) -> tuple[str, int | None, int | None]:
    value = unicodedata.normalize("NFKC", str(value or ""))
    year_match = re.search(r"[([]((?:19|20)\d{2})[)\]]", value)
    season_match = re.search(r"(?:\bS(\d{1,3})\b|第\s*(\d+)\s*季)", value, re.I)
    value = re.split(r"[〖【]|\s+from\s+", value, maxsplit=1, flags=re.I)[0]
    value = re.sub(r"[([](?:19|20)\d{2}[)\]]", "", value)
    value = re.sub(r"(?:\bS\d{1,3}\b|第\s*\d+\s*季)", "", value, flags=re.I)
    return (
        title_key(value),
        int(season_match.group(1) or season_match.group(2)) if season_match else None,
        int(year_match.group(1)) if year_match else None,
    )


@dataclass(frozen=True)
class DanmuMatch:
    episode_id: str
    provider_value: str
    anime_title: str
    episode_title: str
    query: str


class LogVarClient:
    def __init__(self, base_url: str, session: Any = None):
        self.base_url = api_base(base_url)
        self.session = session or requests.Session()
        self._details: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if callable(close):
            close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.session.request(
                method, self.base_url + path, timeout=(10, 30),
                allow_redirects=False, **kwargs,
            )
            if 300 <= response.status_code < 400:
                raise LogVarRequestError("LogVar 接口重定向，请检查服务基础地址", stop_immediately=True)
            response.raise_for_status()
        except requests.exceptions.SSLError:
            raise LogVarRequestError("LogVar TLS 证书或握手失败，请检查服务地址和证书", stop_immediately=True) from None
        except requests.Timeout:
            raise LogVarRequestError("LogVar 请求超时，请检查 MP 容器到弹幕服务的网络") from None
        except requests.ConnectionError:
            raise LogVarRequestError("LogVar 连接失败，请检查 MP 容器的网络、DNS 和服务端口") from None
        except requests.RequestException as err:
            status = getattr(getattr(err, "response", None), "status_code", None)
            transient = status == 429 or (isinstance(status, int) and status >= 500)
            hint = "，请检查 Token 或访问权限" if status in {401, 403} else ""
            raise LogVarRequestError(
                f"LogVar 请求失败（HTTP {status or '请求异常'}）{hint}",
                stop_immediately=not transient,
            ) from None
        try:
            payload = response.json()
        except ValueError:
            raise LogVarRequestError("LogVar 返回内容不是有效 JSON，请检查服务基础地址", stop_immediately=True) from None
        if not isinstance(payload, dict) or payload.get("success") is False or payload.get("errorCode") not in (None, 0):
            raise LogVarRequestError("LogVar 未返回成功结果，请检查弹幕服务日志", stop_immediately=True)
        return payload

    def match(self, history: Any, *, movie: bool, settings: dict[str, Any]) -> DanmuMatch:
        title = str(getattr(history, "title", "") or "").strip()
        year = str(getattr(history, "year", "") or "").strip()
        season_text = str(getattr(history, "seasons", "") or "")
        episode_text = str(getattr(history, "episodes", "") or "")
        season = re.fullmatch(r"S(\d+)", season_text, re.I)
        episode = re.fullmatch(r"E(\d+)", episode_text, re.I)
        if not title or (not movie and (not season or not episode)):
            raise DanmuMatchError("缺少明确剧名或单集季集编号，多集视频暂不补全弹幕")
        if not movie and (int(season.group(1)) < 1 or int(episode.group(1)) < 1):
            raise DanmuMatchError("特典或缺少正片季集编号，暂不补全弹幕")
        query = (f"{title} ({year})" if year else title) if movie else f"{title} {season_text}{episode_text}"
        payload = self._request("POST", "/api/v2/match", json={"fileName": query})
        if payload.get("isMatched") is False:
            raise DanmuUnavailableError(f"LogVar 尚未匹配到目标媒体｜查询：{query}")
        if payload.get("isMatched") is not True:
            raise DanmuMatchError(f"LogVar 未明确返回匹配成功，未提交下载｜查询：{query}")
        matches = payload.get("matches")
        if not isinstance(matches, list) or not matches:
            raise DanmuMatchError(f"LogVar 匹配成功但未返回有效候选列表｜查询：{query}")
        candidates: dict[tuple[str, str], dict[str, Any]] = {}
        for candidate in matches:
            if not isinstance(candidate, dict) or not candidate.get("episodeId") or not candidate.get("animeId"):
                raise DanmuMatchError(f"LogVar 候选缺少作品 ID 或分集 ID｜查询：{query}")
            candidate_key = (str(candidate["animeId"]), str(candidate["episodeId"]))
            if candidate_key in candidates and candidates[candidate_key] != candidate:
                raise DanmuMatchError(f"LogVar 同一分集 ID 返回了矛盾信息，未提交下载｜查询：{query}")
            candidates[candidate_key] = candidate
        if len(candidates) != 1:
            raise DanmuMatchError(f"LogVar 返回 {len(candidates)} 个不同匹配，无法唯一确定，未提交下载｜查询：{query}")
        candidate = next(iter(candidates.values()))
        context = f"查询：{query}｜返回：{candidate.get('animeTitle') or '未提供作品名'} / {candidate.get('episodeTitle') or '未提供分集名'}"
        kind = str(candidate.get("typeDescription") or candidate.get("type") or "").strip().casefold()
        movie_types = {"电影", "剧场版", "movie"}
        if not kind:
            raise DanmuMatchError(f"LogVar 未提供媒体类型，未提交下载｜{context}")
        if (kind in movie_types) != movie:
            raise DanmuMatchError(f"LogVar 媒体类型不符：请求{'电影' if movie else '剧集'}，返回 {kind}｜{context}")
        if movie:
            name, _, matched_year = content_title(candidate.get("animeTitle", ""))
            if name != title_key(title):
                raise DanmuMatchError(f"电影片名不一致，未提交下载｜{context}")
            if year.isdigit() and matched_year != int(year):
                raise DanmuMatchError(f"电影年份不一致：预期 {year}，返回 {matched_year or '未提供'}｜{context}")
        # LogVar owns series aliases and season/episode mapping. Validate IDs, not names.
        api_settings = settings.get("DanmuApi") or {}
        if str(api_settings.get("AllowedSources") or "").strip():
            raise DanmuMatchError("Danmu 限制了采集源，暂无法确认匹配结果符合限制，请检查来源配置")
        platforms = {p.strip().casefold() for p in str(api_settings.get("AllowedPlatforms") or "").split(",") if p.strip()}
        if platforms:
            platform = re.search(r"[〖【]([^〗】]+)[〗】]", str(candidate.get("episodeTitle", "")))
            if not platform or platform.group(1).casefold() not in platforms:
                raise DanmuMatchError("匹配结果不符合 Danmu 允许的平台限制")
        episode_id = str(candidate["episodeId"])
        provider_value = episode_id
        anime_id = str(candidate["animeId"])
        if anime_id not in self._details:
            detail = self._request("GET", "/api/v2/bangumi/" + quote(anime_id, safe=""))
            bangumi = detail.get("bangumi")
            if not isinstance(bangumi, dict):
                raise DanmuMatchError(f"LogVar 未返回有效作品详情，未提交下载｜{context}")
            self._details[anime_id] = bangumi
        bangumi = self._details[anime_id]
        if str(bangumi.get("animeId")) != anime_id:
            raise DanmuMatchError(f"LogVar 作品 ID 不一致：匹配 {anime_id}，详情 {bangumi.get('animeId')}｜{context}")
        detail_kind = str(bangumi.get("typeDescription") or bangumi.get("type") or "").strip().casefold()
        if not detail_kind or (detail_kind in movie_types) != movie:
            raise DanmuMatchError(f"LogVar 来源作品详情的媒体类型缺失或不符｜{context}")
        episodes = bangumi.get("episodes") or []
        if not isinstance(episodes, list):
            raise DanmuMatchError(f"LogVar 作品分集列表无效，未提交下载｜{context}")
        selected = [ep for ep in episodes if isinstance(ep, dict) and str(ep.get("episodeId")) == episode_id]
        if len(selected) != 1:
            raise DanmuMatchError(f"LogVar 分集 ID {episode_id} 在来源作品 {anime_id} 的详情中出现 {len(selected)} 次，未提交下载｜{context}")
        if movie:
            if len(episodes) != 1:
                raise DanmuMatchError(f"电影作品包含 {len(episodes)} 个分集，不能安全交给 Danmu 下载｜{context}")
            detail_name, _, detail_year = content_title(bangumi.get("animeTitle", ""))
            if detail_name != title_key(title) or (year.isdigit() and detail_year != int(year)):
                raise DanmuMatchError(f"电影详情中的片名或年份不一致，未提交下载｜{context}")
            provider_value = anime_id
        return DanmuMatch(episode_id, provider_value,
                          str(bangumi.get("animeTitle") or candidate.get("animeTitle") or "未提供作品名"),
                          str(selected[0].get("episodeTitle") or candidate.get("episodeTitle") or "未提供分集名"), query)

    def ensure_comments_available(self, match: DanmuMatch) -> None:
        payload = self._request("GET", "/api/v2/comment/" + quote(match.episode_id, safe=""),
                                params={"format": "json"})
        comments = payload.get("comments")
        count = payload.get("count")
        if not isinstance(comments, list) or (count is not None and (
                isinstance(count, bool) or not isinstance(count, int) or count != len(comments))):
            raise LogVarRequestError("LogVar 弹幕内容或数量格式异常，不能判定为无弹幕", stop_immediately=True)
        if not comments:
            raise DanmuUnavailableError(
                f"LogVar 已匹配到媒体，但当前来源返回 0 条弹幕｜查询：{match.query}"
                f"｜来源：{match.anime_title} / {match.episode_title}"
            )


class DanmuMaintenance:
    MAX_ATTEMPTS = 3
    MAX_UNAVAILABLE_RESULTS = 3
    MANUAL_UNAVAILABLE_RESULTS = 2
    CONFIRM_MINUTES = 10
    RETRY_MINUTES = 60
    AUTO_DOWNLOAD_GRACE_MINUTES = 30
    MAX_SERVICE_FAILURES = 3

    def __init__(self, jellyfin: JellyfinServiceClient, logvar: LogVarClient, logger: Any,
                 checkpoint: Callable[[dict[str, Any]], bool], dry_run: bool):
        self.jellyfin = jellyfin
        self.logvar = logvar
        self.logger = logger
        self.checkpoint = checkpoint
        self.dry_run = dry_run

    @staticmethod
    def due(node: dict[str, Any]) -> bool:
        if node.get("status") not in {"ready", "retry", "submitted"}:
            return False
        try:
            return datetime.fromisoformat(node["check_after"]) <= datetime.now()
        except (KeyError, TypeError, ValueError):
            return True

    @staticmethod
    def after(minutes: int) -> str:
        return (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")

    @staticmethod
    def files(path: str, to_ass: bool) -> tuple[bool, bool]:
        video = Path(path)
        xml = video.with_suffix(".xml")
        xml_ready = False
        if xml.is_file() and 0 < xml.stat().st_size <= 10 * 1024 * 1024:
            try:
                root = ElementTree.parse(xml).getroot()
                xml_ready = root.tag == "i" and root.find("d") is not None
            except ElementTree.ParseError:
                pass
        ass = video.with_suffix(".danmu.ass")
        ass_ready = not to_ass
        if to_ass and ass.is_file() and ass.stat().st_size > 0:
            with ass.open(encoding="utf-8-sig", errors="replace") as handle:
                # ASS must contain dialogue, not just an empty header.
                ass_ready = any(line.startswith("Dialogue:") for line in handle)
        return xml_ready, ass_ready

    def run(self, histories: list[Any], reorganizer: Any, state: dict[str, Any], result: RunResult,
            days: int, library_ids: list[str]) -> None:
        settings = self.jellyfin.danmu_settings()
        if not str((settings.get("DanmuApi") or {}).get("ServerUrl") or "").strip():
            raise RuntimeError("Jellyfin Danmu 尚未配置弹幕 API 地址，未写入关联")
        to_ass = settings.get("ToAss") is True
        blocked = {"pending_refresh", "pending_reorganize", "attention"}
        candidates: list[tuple[Any, str, str, dict[str, Any]]] = []
        checked = 0
        def queue_order(history: Any) -> tuple[bool, str]:
            node = state.get(reorganizer.processing_key(history), {}).get("danmu") or {}
            return node.get("status") == "complete", str(node.get("check_after") or "")

        for history in sorted(histories, key=queue_order):
            key = reorganizer.processing_key(history)
            item = state.setdefault(key, {})
            path = str(reorganizer.target_path(history) or "")
            if not path or item.get("status") in blocked or any(item.get(flag) for flag in ("scan_pending", "sidecar_pending", "cleanup_pending", "operation_intent")):
                continue
            # A preview path does not prove that the video was actually renamed.
            if not Path(path).is_file() and int(item.get("rename_attempts") or 0) > 0:
                renamed_path = str(item.get("expected_path") or "")
                if renamed_path and Path(renamed_path).is_file():
                    path = renamed_path
            node = item.get("danmu") or {}
            if node.get("path") != path:
                node = {"status": "ready", "path": path, "attempts": 0,
                        "label": reorganizer.display_name(history), "check_after": self.after(0)}
            if node.get("status") == "attention":
                continue
            if node.get("status") != "complete" and not self.due(node):
                continue
            if checked >= max(result.operation_limit, 1) * 5:
                break
            checked += 1
            item["danmu"] = node
            result.danmu_checked += 1
            if not Path(path).is_file():
                self._fail(node, "未找到当前视频文件，未提交弹幕下载", terminal=True)
                continue
            try:
                xml_ready, ass_ready = self.files(path, to_ass)
            except OSError:
                node["attempts"] = int(node.get("attempts", 0)) + 1
                self._fail(node, "弹幕目录暂不可访问")
                continue
            if xml_ready and ass_ready:
                if node.get("status") == "submitted":
                    result.danmu_completed.append(path)
                    self.logger.info(f"[最近媒体维护] 弹幕补全完成：{path}")
                node.update(status="complete", error="")
                continue
            if Path(path).with_suffix(".xml").exists():
                if node.get("status") == "submitted" and xml_ready and int(node.get("confirmations", 0)) < 2:
                    node["confirmations"] = int(node.get("confirmations", 0)) + 1
                    node["check_after"] = self.after(self.CONFIRM_MINUTES)
                    continue
                self._fail(node, "已有 XML 但内容无效或 ASS 未生成；保留原文件，不覆盖下载", terminal=True)
                continue
            if node.get("status") == "submitted":
                node["verify_comments"] = True
                previous_error = node.get("error")
                detail = f"提交后未生成 XML；上次请求：{previous_error}" if previous_error else "提交后未生成 XML，稍后有限次重试"
                self._fail(node, detail)
                self.logger.warning(f"[最近媒体维护] 弹幕确认失败：{path}；{node['error']}")
                continue
            if to_ass and Path(path).with_suffix(".danmu.ass").exists():
                self._fail(node, "已有 ASS 但缺少 XML；为避免覆盖现有字幕，未提交下载", terminal=True)
                continue
            if result.operations_used >= result.operation_limit:
                node["check_after"] = self.after(self.CONFIRM_MINUTES)
                continue
            candidates.append((history, key, path, node))
        if not candidates:
            self._counts(state, result)
            return
        targets = [EpisodeTarget(path, reorganizer.media_type(history)) for history, _, path, _ in candidates]
        try:
            matches = self.jellyfin.match_recent_media(targets, days, library_ids)
        except Exception as err:
            for _, _, path, node in candidates:
                node["attempts"] = int(node.get("attempts", 0)) + 1
                self._fail(node, "Jellyfin 媒体匹配失败，稍后有限次重试")
                result.add_error(f"弹幕媒体匹配失败：{safe_error(err)}", path)
            self._counts(state, result)
            return
        service_failures = 0
        submission_failures = 0
        availability_checks = 0
        for index, (history, key, path, node) in enumerate(candidates):
            if result.operations_used >= result.operation_limit:
                break
            verify_comments = bool(node.get("verify_comments") or int(node.get("unavailable_count", 0)) > 0)
            if not self.dry_run and verify_comments and availability_checks >= max(result.operation_limit, 1):
                node["check_after"] = self.after(self.RETRY_MINUTES)
                continue
            try:
                items = matches.get(JellyfinServiceClient.path_key(path)) or []
                if len(items) != 1:
                    raise RuntimeError("未在 Jellyfin 中唯一匹配到该视频")
                media = items[0]
                dto = self.jellyfin.danmu_item(media.item_id)
                if Path(dto["Path"]).name != Path(path).name:
                    raise DanmuMatchError("Jellyfin 路径已变化，未写入弹幕关联")
                if dto["Type"] != ("Movie" if reorganizer.media_type(history) == "movie" else "Episode"):
                    raise DanmuMatchError("Jellyfin 媒体类型与 MP 记录不一致")
                if dto["Type"] == "Episode" and f"S{int(dto.get('ParentIndexNumber') or 0):02d}E{int(dto.get('IndexNumber') or 0):02d}".casefold() != (str(getattr(history, "seasons", "")) + str(getattr(history, "episodes", ""))).casefold():
                    raise DanmuMatchError("Jellyfin 季集编号与 MP 记录不一致")
                created = dto.get("DateCreated")
                if created:
                    timestamp = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    if timestamp > datetime.now(timezone.utc) - timedelta(minutes=self.AUTO_DOWNLOAD_GRACE_MINUTES):
                        node["check_after"] = self.after(self.AUTO_DOWNLOAD_GRACE_MINUTES)
                        continue
                match = self.logvar.match(history, movie=dto["Type"] == "Movie", settings=settings)
                if self.dry_run:
                    service_failures = 0
                    result.danmu_previewed += 1
                    self.logger.info(f"[最近媒体维护] 弹幕试运行：{match.query} → {match.anime_title} / {match.episode_title}；分集 ID：{match.episode_id}；未写入关联或下载")
                    continue
                # Only failed downloads need a content probe; normal first submissions stay cheap.
                if verify_comments:
                    availability_checks += 1
                    self.logvar.ensure_comments_available(match)
                    node["unavailable_count"] = 0
                    node["manual_recheck"] = False
                    node.pop("manual_rechecks_remaining", None)
                service_failures = 0
                self.logger.info(f"[最近媒体维护] 弹幕匹配：{match.query} → {match.anime_title} / {match.episode_title}；分集 ID：{match.episode_id}")
                # Checkpoint before the external operation; interruptions resume file confirmation.
                node.update(status="submitted", attempts=int(node.get("attempts", 0)) + 1,
                            check_after=self.after(self.CONFIRM_MINUTES), item_id=media.item_id,
                            provider_value=match.provider_value, to_ass=to_ass, confirmations=0, error="")
                if not self.checkpoint(state):
                    result.add_error("弹幕操作意图保存失败，停止提交")
                    break
                if Path(path).with_suffix(".xml").exists() or (
                    to_ass and Path(path).with_suffix(".danmu.ass").exists()
                ):
                    self._fail(node, "其他任务已生成弹幕文件，保留已有文件", terminal=True)
                    continue
                result.operations_used += 1
                pause_submissions = False
                try:
                    if dto["ProviderIds"].get("DanmuID") == f"danmuapi:{match.provider_value}":
                        self.jellyfin.request_danmu(media.item_id)
                    else:
                        self.jellyfin.set_danmu_id(dto, match.provider_value)
                        if not (settings.get("DownloadOption") or {}).get("EnableAutoDownload"):
                            self.jellyfin.request_danmu(media.item_id)
                    result.danmu_submitted += 1
                    submission_failures = 0
                except Exception as err:
                    submission_failures += 1
                    rejected = isinstance(err, DanmuSubmissionError) and not err.uncertain
                    detail = safe_error(err)
                    if rejected:
                        self._fail(node, f"弹幕提交失败：{detail}")
                    else:
                        # An unacknowledged write/queue request must first confirm files.
                        node["error"] = f"弹幕请求结果未确认，将先检查文件再决定是否重试：{detail}"
                    result.add_error(f"弹幕补全 {reorganizer.display_name(history)}：{node['error']}", path)
                    self.logger.warning(f"[最近媒体维护] {node['error']}｜文件：{path}")
                    pause_submissions = (
                        getattr(err, "status_code", None) in {400, 401, 403, 404, 405, 422}
                        or submission_failures >= self.MAX_SERVICE_FAILURES
                    )
                    if pause_submissions:
                        message = f"Jellyfin 弹幕提交失败，本轮暂停后续弹幕请求，约 {self.RETRY_MINUTES} 分钟后重试；未请求的媒体不计失败次数"
                        for _, _, _, waiting in candidates[index + 1:]:
                            waiting.update(status="retry", check_after=self.after(self.RETRY_MINUTES),
                                           error="Jellyfin 弹幕提交暂不可用，本轮尚未请求")
                        self.logger.warning(f"[最近媒体维护] {message}")
                        result.add_error(message)
                if not self.checkpoint(state):
                    result.add_error("弹幕提交后的状态保存失败，停止提交")
                    break
                if node["status"] == "submitted":
                    self.logger.info(f"[最近媒体维护] 单项弹幕补全等待确认：{reorganizer.display_name(history)}；{self.CONFIRM_MINUTES} 分钟后确认 XML{' 和 ASS' if to_ass else ''}｜文件：{path}")
                if pause_submissions:
                    break
            except Exception as err:
                node["attempts"] = int(node.get("attempts", 0)) + 1
                detail = safe_error(err)
                if isinstance(err, DanmuUnavailableError):
                    node["verify_comments"] = True
                    node["unavailable_count"] = int(node.get("unavailable_count", 0)) + 1
                    if node.get("manual_recheck"):
                        remaining = max(int(node.get("manual_rechecks_remaining", self.MANUAL_UNAVAILABLE_RESULTS)) - 1, 0)
                        node["manual_rechecks_remaining"] = remaining
                        stopped = remaining == 0
                    else:
                        stopped = node["unavailable_count"] >= self.MAX_UNAVAILABLE_RESULTS
                    detail += f"；累计确认无匹配或无弹幕 {node['unavailable_count']} 次"
                    if node.get("manual_recheck"):
                        detail += f"；本次手动复核剩余 {remaining} 次"
                    detail += "，已停止自动重试；手动重新处理仅复核可用性" if stopped else "，稍后复核可用性，暂不请求 Jellyfin 下载"
                    # Availability confirmations have their own budget, separate from download attempts.
                    node.update(error=detail, status="attention" if stopped else "retry",
                                check_after=self.after(self.RETRY_MINUTES))
                    self.logger.warning(f"[最近媒体维护] 弹幕暂不可用 {reorganizer.display_name(history)}：{detail}｜文件：{path}")
                else:
                    self._fail(node, detail, terminal=isinstance(err, (DanmuMatchError, ValueError)))
                result.add_error(f"弹幕补全 {reorganizer.display_name(history)}：{detail}", path)
                if isinstance(err, LogVarRequestError):
                    service_failures += 1
                    if err.stop_immediately or service_failures >= self.MAX_SERVICE_FAILURES:
                        reason = "服务地址或权限异常" if err.stop_immediately else f"连续 {service_failures} 次服务请求失败"
                        message = f"LogVar {reason}，本轮暂停后续弹幕请求，约 {self.RETRY_MINUTES} 分钟后重试；未请求的媒体不计失败次数"
                        for _, _, _, waiting in candidates[index + 1:]:
                            waiting.update(status="retry", check_after=self.after(self.RETRY_MINUTES),
                                           error="LogVar 服务暂不可用，本轮尚未请求")
                        self.logger.warning(f"[最近媒体维护] {message}")
                        result.add_error(message)
                        break
                else:
                    service_failures = 0
        self._counts(state, result)

    def _fail(self, node: dict[str, Any], message: str, terminal: bool = False) -> None:
        node.update(error=message, status="attention" if terminal or int(node.get("attempts", 0)) >= self.MAX_ATTEMPTS else "retry",
                    check_after=self.after(self.RETRY_MINUTES))

    @classmethod
    def confirm_due(cls, state: dict[str, Any], logger: Any, result: RunResult) -> bool:
        """File-only confirmations: no MP preview, matching request or Jellyfin refresh."""
        changed = False
        for item in state.values():
            node = item.get("danmu") or {}
            if node.get("status") != "submitted" or not cls.due(node):
                continue
            changed = True
            result.danmu_checked += 1
            path = str(node.get("path") or "")
            try:
                if not Path(path).is_file():
                    node.update(status="attention", error="等待弹幕时视频已消失，请检查媒体或整理记录")
                    continue
                xml_ready, ass_ready = cls.files(path, bool(node.get("to_ass")))
                if xml_ready and ass_ready:
                    node.update(status="complete", error="")
                    result.danmu_completed.append(path)
                    logger.info(f"[最近媒体维护] 弹幕补全完成：{path}")
                elif Path(path).with_suffix(".xml").exists():
                    if xml_ready and int(node.get("confirmations", 0)) < 2:
                        node["confirmations"] = int(node.get("confirmations", 0)) + 1
                        node["check_after"] = cls.after(cls.CONFIRM_MINUTES)
                    else:
                        node.update(status="attention", error="XML 无效或 ASS 未生成；保留 XML，不覆盖下载")
                else:
                    node["verify_comments"] = True
                    previous_error = node.get("error")
                    detail = f"提交后未生成 XML；上次请求：{previous_error}" if previous_error else "提交后未生成 XML"
                    node.update(status="attention" if int(node.get("attempts", 0)) >= cls.MAX_ATTEMPTS else "retry",
                                error=detail, check_after=cls.after(cls.RETRY_MINUTES))
                    logger.warning(f"[最近媒体维护] 弹幕确认失败：{path}；{node['error']}")
            except OSError:
                node["attempts"] = int(node.get("attempts", 0)) + 1
                node.update(status="attention" if node["attempts"] >= cls.MAX_ATTEMPTS else "retry",
                            error="弹幕目录暂不可访问", check_after=cls.after(cls.RETRY_MINUTES))
        cls._counts(state, result)
        return changed

    @staticmethod
    def _counts(state: dict[str, Any], result: RunResult) -> None:
        result.danmu_waiting = sum(
            (item.get("danmu") or {}).get("status") in {"ready", "retry", "submitted"}
            for item in state.values()
        )
        for item in state.values():
            node = item.get("danmu") or {}
            if node.get("status") == "attention":
                text = f"{node.get('label', '媒体')}｜文件：{node.get('path', '未知')}｜原因：{node.get('error', '未知')}"
                if text not in result.danmu_attention:
                    result.danmu_attention.append(text)
