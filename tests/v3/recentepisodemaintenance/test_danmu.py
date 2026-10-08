from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
from types import SimpleNamespace

import pytest
import requests
import recentepisodemaintenance as plugin_module

from recentepisodemaintenance import RecentEpisodeMaintenance
from recentepisodemaintenance.danmu import (
    DanmuMaintenance, DanmuMatchError, LogVarClient, LogVarRequestError, api_base,
)
from recentepisodemaintenance.jellyfin_client import JellyfinServiceClient
from recentepisodemaintenance.models import EpisodeItem, RunResult
from recentepisodemaintenance.reorganizer import MoviePilotReorganizer


SETTINGS = {
    "ToAss": True,
    "DownloadOption": {"EnableAutoDownload": True},
    "Scrapers": [{"Name": "bilibili", "Enable": True}, {"Name": "弹幕API", "Enable": True}],
    "DanmuApi": {"ServerUrl": "https://logvar.test/token"},
}


class Session:
    def __init__(self, matches=None, episodes=None, failure=None):
        self.matches = matches if matches is not None else [
            {"animeId": 20, "episodeId": 71, "animeTitle": "测试剧(2026)〖电视剧〗from tencent",
             "episodeTitle": "〖tencent〗 第7集", "type": "电视剧"},
        ]
        self.episodes = episodes if episodes is not None else [{"episodeId": 71}]
        self.detail_override = {}
        self.match_override = {}
        self.failure = failure
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.failure:
            raise self.failure
        payload = {"success": True, "isMatched": True, "matches": self.matches, **self.match_override}
        if "/bangumi/" in url:
            candidate = self.matches[0]
            episodes = [{"episodeTitle": candidate.get("episodeTitle", ""), **ep} for ep in self.episodes]
            payload = {"success": True, "bangumi": {
                "animeId": candidate["animeId"], "animeTitle": candidate["animeTitle"],
                "type": candidate.get("type"), "typeDescription": candidate.get("typeDescription"),
                "episodes": episodes, **self.detail_override,
            }}
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: payload)


def history(path="/media/video.mp4", **overrides):
    values = dict(id=1, title="测试剧", year="2026", seasons="S01", episodes="E07",
                  type="电视剧", dest=path, media_id="20")
    return SimpleNamespace(**(values | overrides))


@pytest.mark.parametrize("url", ["", "ftp://host", "https://user:secret@host", "https://host/api/v2/match", "https://host?a=token"])
def test_rejects_invalid_base_url(url):
    with pytest.raises(ValueError):
        api_base(url)


def test_logvar_matches_precise_episode_without_downloading_comments():
    session = Session()
    match = LogVarClient(SETTINGS["DanmuApi"]["ServerUrl"], session).match(history(), movie=False, settings=SETTINGS)
    assert match.provider_value == "71"
    assert session.calls[0][2]["json"] == {"fileName": "测试剧 S01E07"}
    assert session.calls[0][2]["allow_redirects"] is False
    assert len(session.calls) == 2
    assert session.calls[1][1].endswith("/api/v2/bangumi/20")
    assert not any("/comment" in url for _, url, _ in session.calls)


@pytest.mark.parametrize("change", [
    {"animeTitle": "别的剧(2026)〖电视剧〗"},
    {"animeTitle": "测试剧外传(2026)〖电视剧〗"},
    {"episodeTitle": "预告片"},
    {"episodeTitle": "第0集"},
    {"episodeTitle": "第7集、第8集"},
])
def test_series_match_does_not_second_guess_names_or_episode_title_formats(change):
    session = Session()
    session.matches[0].update(change)
    match = LogVarClient("https://logvar.test/token", session).match(history(), movie=False, settings=SETTINGS)
    assert match.provider_value == "71"


@pytest.mark.parametrize("title,season,episode,source_title,source_episode", [
    ("遮天", "S01", "E183", "遮天 年番1(2023)【国漫】 from tencent", "【qq】 遮天_183"),
    ("遮天", "S01", "E184", "遮天 年番1(2023)【国漫】 from tencent", "【qq】 遮天_184"),
    ("我家那闺女", "S05", "E12", "我家那闺女2026(2026)【综艺】 from 360", "【imgo】 第6期上：张家齐母女错位的爱 2026-10-04期"),
    ("我家那闺女", "S05", "E13", "我家那闺女2026(2026)【综艺】 from 360", "【imgo】 第6期下：淡淡孟佳聊离婚感受 2026-10-04期"),
    ("原剧名", "S03", "E12", "英文别名(2026)【电视剧】", "Episode Name Without A Number"),
])
def test_provider_resolves_titles_and_episode_mapping_without_local_regexes(
        title, season, episode, source_title, source_episode):
    session = Session()
    session.matches[0].update(animeTitle=source_title, episodeTitle=source_episode)
    match = LogVarClient("https://logvar.test/token", session).match(
        history(title=title, seasons=season, episodes=episode), movie=False, settings=SETTINGS,
    )
    assert session.calls[0][2]["json"] == {"fileName": f"{title} {season}{episode}"}
    assert match.anime_title == source_title and match.episode_title == source_episode
    assert match.provider_value == "71"


@pytest.mark.parametrize("override,message", [
    ({"isMatched": False}, "尚未匹配"),
    ({"isMatched": None}, "未明确返回匹配成功"),
    ({"isMatched": "true"}, "未明确返回匹配成功"),
    ({"matches": []}, "未返回有效候选列表"),
    ({"matches": {}}, "未返回有效候选列表"),
    ({"matches": [{"animeId": 20}]}, "缺少作品 ID 或分集 ID"),
])
def test_match_requires_explicit_success_and_valid_candidate_ids(override, message):
    session = Session()
    session.match_override = override
    with pytest.raises((RuntimeError, DanmuMatchError), match=message) as error:
        LogVarClient("https://logvar.test/token", session).match(history(), movie=False, settings=SETTINGS)
    assert "查询：测试剧 S01E07" in str(error.value)
    assert len(session.calls) == 1


@pytest.mark.parametrize("kind,message", [("电影", "媒体类型不符"), ("", "未提供媒体类型")])
def test_series_match_rejects_movie_or_missing_media_type(kind, message):
    session = Session()
    session.matches[0]["type"] = kind
    with pytest.raises(DanmuMatchError, match=message) as error:
        LogVarClient("https://logvar.test/token", session).match(history(), movie=False, settings=SETTINGS)
    assert "返回：测试剧" in str(error.value)
    assert len(session.calls) == 1


@pytest.mark.parametrize("source_title", [
    "一念永恒 完结季(2026)【动漫】 from 360",
    "一念永恒 第四季(2026)〖动漫〗from tencent",
    "一念永恒 S04(2026)〖动漫〗from tencent",
    "一念永恒(2026)〖动漫〗from tencent",
])
def test_cross_season_match_does_not_require_original_year_or_episode_number(source_title):
    session = Session(matches=[{
        "animeId": 20, "episodeId": 71, "animeTitle": source_title,
        "episodeTitle": "【qq】 第6集", "type": "动漫",
    }])
    match = LogVarClient("https://logvar.test/token", session).match(
        history(title="一念永恒", year="2020", episodes="E171"), movie=False, settings=SETTINGS,
    )
    assert session.calls[0][2]["json"] == {"fileName": "一念永恒 S01E171"}
    assert match.provider_value == "71"
    assert match.query == "一念永恒 S01E171" and match.episode_title == "【qq】 第6集"


def test_later_season_can_map_to_continuous_numbering():
    session = Session()
    session.matches[0].update(animeTitle="测试剧年番(2025)〖动漫〗", episodeTitle="第213集")
    match = LogVarClient("https://logvar.test/token", session).match(
        history(year="2017", seasons="S05", episodes="E13"), movie=False, settings=SETTINGS,
    )
    assert match.query == "测试剧 S05E13" and match.episode_title == "第213集"


def test_variety_season_can_map_to_numbered_part():
    session = Session()
    session.matches[0].update(animeTitle="测试剧第五季(2025)〖综艺〗", episodeTitle="第6期下")
    match = LogVarClient("https://logvar.test/token", session).match(
        history(year="2019", seasons="S05", episodes="E13"), movie=False, settings=SETTINGS,
    )
    assert match.query == "测试剧 S05E13" and match.episode_title == "第6期下"


@pytest.mark.parametrize("detail_override,episodes", [
    ({"animeId": 999}, None),
    ({"type": "电影"}, None),
    ({"type": ""}, None),
    ({}, []),
    ({}, [{"episodeId": 72}]),
    ({}, [{"episodeId": 71}, {"episodeId": 71}]),
])
def test_match_must_be_confirmed_in_source_work_details(detail_override, episodes):
    session = Session(episodes=episodes)
    session.detail_override = detail_override
    with pytest.raises(DanmuMatchError):
        LogVarClient("https://logvar.test/token", session).match(history(), movie=False, settings=SETTINGS)


def test_series_details_are_linked_by_ids_not_title_spellings():
    session = Session(episodes=[{"episodeId": 71, "episodeTitle": "最新分集名称"}])
    session.detail_override = {"animeTitle": "来源作品最新名称(2025)【动漫】"}
    match = LogVarClient("https://logvar.test/token", session).match(history(), movie=False, settings=SETTINGS)
    assert match.anime_title == "来源作品最新名称(2025)【动漫】"
    assert match.episode_title == "最新分集名称" and match.provider_value == "71"


def test_same_episode_id_in_different_works_is_ambiguous():
    session = Session()
    session.matches.append({**session.matches[0], "animeId": 21})
    with pytest.raises(DanmuMatchError):
        LogVarClient("https://logvar.test/token", session).match(history(), movie=False, settings=SETTINGS)
    assert len(session.calls) == 1


def test_source_details_are_reused_only_within_this_client():
    session = Session()
    client = LogVarClient("https://logvar.test/token", session)
    client.match(history(), movie=False, settings=SETTINGS)
    client.match(history(), movie=False, settings=SETTINGS)
    assert [method for method, _, _ in session.calls] == ["POST", "GET", "POST"]
    LogVarClient("https://logvar.test/token", session).match(history(), movie=False, settings=SETTINGS)
    assert [method for method, _, _ in session.calls][-2:] == ["POST", "GET"]


def test_ambiguous_candidates_do_not_choose_first():
    session = Session()
    session.matches.append({**session.matches[0], "episodeId": 72})
    with pytest.raises(DanmuMatchError, match="返回 2 个不同匹配"):
        LogVarClient("https://logvar.test/token", session).match(history(), movie=False, settings=SETTINGS)


def test_ambiguous_candidates_are_not_filtered_until_one_looks_right():
    session = Session()
    session.matches.append({**session.matches[0], "episodeId": 72, "type": "电影"})
    with pytest.raises(DanmuMatchError, match="返回 2 个不同匹配"):
        LogVarClient("https://logvar.test/token", session).match(history(), movie=False, settings=SETTINGS)
    assert len(session.calls) == 1


def test_movie_uses_validated_anime_id_not_episode_id():
    movie = SimpleNamespace(title="测试电影", year="2026")
    session = Session(matches=[{"episodeId": 71, "animeId": 20, "animeTitle": "测试电影(2026)〖电影〗", "type": "电影"}])
    match = LogVarClient("https://logvar.test/token", session).match(movie, movie=True, settings=SETTINGS)
    assert match.episode_id == "71"
    assert match.provider_value == "20"
    assert session.calls[0][2]["json"] == {"fileName": "测试电影 (2026)"}
    assert session.calls[1][0] == "GET"
    session.episodes.append({"episodeId": 72})
    with pytest.raises(DanmuMatchError):
        LogVarClient("https://logvar.test/token", session).match(movie, movie=True, settings=SETTINGS)


def test_movie_still_requires_matching_year_and_type():
    session = Session(matches=[{"episodeId": 71, "animeId": 20, "animeTitle": "测试电影(2025)〖电影〗", "type": "电影"}])
    with pytest.raises(DanmuMatchError):
        LogVarClient("https://logvar.test/token", session).match(
            history(title="测试电影", year="2026", type="电影"), movie=True, settings=SETTINGS,
        )
    assert len(session.calls) == 1


@pytest.mark.parametrize("override", [
    {"animeTitle": "另一电影(2026)【电影】"},
    {"animeTitle": "测试电影(2025)【电影】"},
    {"type": "电视剧"},
])
def test_movie_details_still_require_title_year_and_movie_type(override):
    session = Session(matches=[{"episodeId": 71, "animeId": 20, "animeTitle": "测试电影(2026)〖电影〗", "type": "电影"}])
    session.detail_override = override
    with pytest.raises(DanmuMatchError):
        LogVarClient("https://logvar.test/token", session).match(
            history(title="测试电影", year="2026", type="电影"), movie=True, settings=SETTINGS,
        )


def test_logvar_error_does_not_expose_token():
    session = Session(failure=requests.ConnectionError("https://logvar.test/secret-token/api/v2/match"))
    with pytest.raises(RuntimeError) as error:
        LogVarClient("https://logvar.test/secret-token", session).match(history(), movie=False, settings=SETTINGS)
    assert "secret-token" not in str(error.value)


class Service:
    def __init__(self, dto):
        self.dto = dto
        self.posts = []
        self.gets = []

    def get_data(self, url):
        self.gets.append(url)
        if "/MetadataEditor" in url:
            return {"ExternalIdInfos": [{"Key": "DanmuID"}]}
        return deepcopy(self.dto)

    def post_data(self, url, data=None, headers=None):
        self.posts.append((url, json.loads(data), headers))
        return SimpleNamespace(status_code=204, content=b"", raise_for_status=lambda: None)


def test_metadata_update_preserves_every_other_field_and_provider():
    dto = {"Id": "item", "Type": "Episode", "Name": "山雨欲来", "Path": "/video.mp4",
           "Overview": "简介", "ProviderIds": {"Tmdb": "123", "DanmuID": "bilibili:old"},
           "LockData": True, "Tags": ["动画"], "Genres": ["动画"], "LockedFields": [],
           "IndexNumber": 7, "ParentIndexNumber": 1}
    original = deepcopy(dto)
    service = Service(dto)
    client = JellyfinServiceClient(service)
    item = client.danmu_item("item")
    client.set_danmu_id(item, "71")
    payload = service.posts[0][1]
    assert payload["ProviderIds"] == {"Tmdb": "123", "DanmuID": "danmuapi:71"}
    payload["ProviderIds"] = original["ProviderIds"]
    assert payload == original == dto
    assert "userId=[USER]" in service.gets[0]
    assert not any("/Refresh" in url for url, _, _ in service.posts)


def test_concurrent_metadata_change_aborts_write():
    dto = {"Id": "item", "Type": "Movie", "Name": "电影", "Path": "/video.mkv", "ProviderIds": {},
           "LockData": False, "Tags": [], "Genres": [], "LockedFields": []}
    service = Service(dto)
    client = JellyfinServiceClient(service)
    original = client.danmu_item("item")
    service.dto["Name"] = "新名称"
    with pytest.raises(RuntimeError):
        client.set_danmu_id(original, "20")
    assert service.posts == []


class Jellyfin:
    def __init__(self, path):
        self.path = str(path)
        self.writes = []
        self.match_calls = 0

    def danmu_settings(self):
        return SETTINGS

    def match_recent_media(self, targets, days, library_ids):
        self.match_calls += 1
        return {JellyfinServiceClient.path_key(self.path): [EpisodeItem("item", path=self.path)]}

    def danmu_item(self, item_id):
        return {"Id": "item", "Type": "Episode", "Path": self.path, "Name": "正确标题",
                "ProviderIds": {}, "ParentIndexNumber": 1, "IndexNumber": 7}

    def set_danmu_id(self, dto, provider_value):
        self.writes.append(provider_value)


def runner(path, *, dry_run=False, checkpoint=None):
    jf = Jellyfin(path)
    run = DanmuMaintenance(jf, LogVarClient("https://logvar.test/token", Session()),
                          logging.getLogger("test"), checkpoint or (lambda _: True), dry_run)
    return run, jf


def test_cross_season_preview_logs_mapping_without_metadata_write(tmp_path, caplog):
    path = tmp_path / "一念永恒 S01E171.mkv"
    path.touch()
    run, jf = runner(path, dry_run=True)
    jf.danmu_item = lambda _: {
        "Id": "item", "Type": "Episode", "Path": str(path), "ProviderIds": {},
        "ParentIndexNumber": 1, "IndexNumber": 171,
    }
    run.logvar.session.matches[0].update(
        animeTitle="一念永恒 完结季(2026)【动漫】 from 360", episodeTitle="【qq】 第6集",
    )
    with caplog.at_level(logging.INFO, logger="test"):
        result = RunResult(operation_limit=1)
        run.run([history(str(path), title="一念永恒", year="2020", episodes="E171")],
                MoviePilotReorganizer, {}, result, 15, [])
    assert result.danmu_previewed == 1 and not jf.writes
    assert "一念永恒 S01E171 → 一念永恒 完结季(2026)" in caplog.text
    assert "【qq】 第6集" in caplog.text and "分集 ID：71" in caplog.text


def test_submit_is_not_success_and_confirmation_is_file_only(tmp_path):
    path = tmp_path / "测试剧.mp4"
    path.touch()
    h = history(str(path))
    key = MoviePilotReorganizer.processing_key(h)
    state = {key: {"status": "complete"}}
    result = RunResult(operation_limit=1)
    run, jf = runner(path)
    run.run([h], MoviePilotReorganizer, state, result, 15, [])
    node = state[key]["danmu"]
    assert jf.writes == ["71"]
    assert node["status"] == "submitted"
    assert result.danmu_completed == []
    path.with_suffix(".xml").write_text('<i><d p="1,1,25,16777215,0,0,0,0">弹幕</d></i>', encoding="utf-8")
    path.with_suffix(".danmu.ass").write_text("[Events]\nDialogue: 0,0:00:01.00,0:00:05.00,Default,,0,0,0,,弹幕", encoding="utf-8")
    node["check_after"] = "2000-01-01T00:00:00"
    confirmation = RunResult()
    assert DanmuMaintenance.confirm_due(state, logging.getLogger("test"), confirmation)
    assert node["status"] == "complete"
    assert confirmation.danmu_completed == [str(path)]
    assert jf.match_calls == 1


@pytest.mark.parametrize("dry_run,save_ok", [(True, True), (False, False)])
def test_dry_run_or_failed_checkpoint_never_writes(tmp_path, dry_run, save_ok):
    path = tmp_path / "video.mp4"
    path.touch()
    run, jf = runner(path, dry_run=dry_run, checkpoint=lambda _: save_ok)
    result = RunResult(operation_limit=1)
    run.run([history(str(path))], MoviePilotReorganizer, {}, result, 15, [])
    assert jf.writes == []


def test_existing_xml_missing_ass_is_preserved_without_redownload(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    xml = path.with_suffix(".xml")
    content = '<i><d p="1">已有弹幕</d></i>'
    xml.write_text(content, encoding="utf-8")
    run, jf = runner(path)
    state = {}
    run.run([history(str(path))], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert jf.writes == [] and jf.match_calls == 0
    assert xml.read_text(encoding="utf-8") == content
    assert next(iter(state.values()))["danmu"]["status"] == "attention"


def test_reorganization_and_operation_limit_block_danmu(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    h = history(str(path))
    key = MoviePilotReorganizer.processing_key(h)
    run, jf = runner(path)
    state = {key: {"status": "pending_reorganize", "sidecar_pending": True}}
    run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert jf.writes == [] and jf.match_calls == 0
    state[key] = {"status": "complete"}
    run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1, operations_used=1), 15, [])
    assert jf.writes == [] and jf.match_calls == 0


def test_pending_danmu_keeps_completed_history_tracked_and_retryable():
    state = {"key": {"history_id": 1, "status": "complete", "danmu": {"status": "attention", "path": "/video.mp4"}}}
    plugin = RecentEpisodeMaintenance()
    plugin.get_data = lambda _: deepcopy(state)
    saved = []
    plugin.save_data = lambda _, value: saved.append(deepcopy(value))
    assert plugin._tracked_history_ids(state) == {1}
    assert plugin._retry_attention_records() == 1
    assert saved[-1]["key"]["danmu"]["status"] == "ready"
    assert saved[-1]["key"]["status"] == "complete"


def test_form_exposes_disabled_danmu_and_persistent_hints():
    plugin = RecentEpisodeMaintenance()
    plugin._media_server_name = "Jellyfin"
    plugin._library_ids = []
    plugin._jellyfin_service_options = lambda: []
    plugin._library_options = lambda: []
    form, defaults = plugin.get_form()
    fields = [child["content"][0]["props"] for row in form[0]["content"] for child in row["content"]]
    assert defaults["enable_danmu"] is False and defaults["danmu_api_url"] == ""
    for model in ("enable_danmu", "danmu_api_url"):
        field = next(field for field in fields if field["model"] == model)
        assert field["persistent-hint"] is True and "LogVar" in field["hint"]


def test_incomplete_metadata_is_never_posted():
    dto = {"Id": "item", "Type": "Movie", "Name": "电影", "Path": "/video.mkv", "ProviderIds": {}}
    service = Service(dto)
    with pytest.raises(RuntimeError, match="完整编辑字段"):
        JellyfinServiceClient(service).danmu_item("item")
    assert not service.posts


def test_old_danmu_without_unified_id_is_rejected_before_write():
    dto = {"Id": "item", "Type": "Movie", "Name": "电影", "Path": "/video.mkv", "ProviderIds": {},
           "LockData": False, "Tags": [], "Genres": [], "LockedFields": []}
    service = Service(dto)
    service.get_data = lambda url: {"ExternalIdInfos": [{"Key": "DanmuApiID"}]} if "/MetadataEditor" in url else dto
    with pytest.raises(RuntimeError, match="DanmuID 编辑字段"):
        JellyfinServiceClient(service).danmu_item("item")
    assert not service.posts


def test_movie_submission_and_xml_only_completion(tmp_path):
    path = tmp_path / "电影.mkv"
    path.touch()
    h = history(str(path), title="测试电影", type="电影", seasons="", episodes="")
    run, jf = runner(path)
    settings = deepcopy(SETTINGS)
    settings["ToAss"] = False
    jf.danmu_settings = lambda: settings
    jf.danmu_item = lambda _: {"Id": "item", "Type": "Movie", "Path": str(path), "ProviderIds": {}}
    run.logvar.session.matches = [{"episodeId": 71, "animeId": 20, "animeTitle": "测试电影(2026)〖电影〗", "type": "电影"}]
    state = {}
    result = RunResult(operation_limit=1)
    run.run([h], MoviePilotReorganizer, state, result, 15, [])
    assert jf.writes == ["20"] and result.danmu_submitted == 1
    node = next(iter(state.values()))["danmu"]
    path.with_suffix(".xml").write_text('<i><d p="1">弹幕</d></i>', encoding="utf-8")
    node["check_after"] = "2000-01-01T00:00:00"
    assert DanmuMaintenance.confirm_due(state, logging.getLogger("test"), RunResult())
    assert node["status"] == "complete"


def test_existing_ass_without_xml_is_not_overwritten(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    ass = path.with_suffix(".danmu.ass")
    ass.write_text("existing", encoding="utf-8")
    run, jf = runner(path)
    state = {}
    run.run([history(str(path))], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert not jf.writes and not jf.match_calls
    assert ass.read_text(encoding="utf-8") == "existing"
    assert next(iter(state.values()))["danmu"]["status"] == "attention"


def test_recently_added_item_waits_without_logvar_request(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    run, jf = runner(path)
    dto = jf.danmu_item("item")
    dto["DateCreated"] = datetime.now(timezone.utc).isoformat()
    jf.danmu_item = lambda _: dto
    state = {}
    run.run([history(str(path))], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert not jf.writes and not run.logvar.session.calls
    assert not DanmuMaintenance.due(next(iter(state.values()))["danmu"])


def test_retries_wait_and_stop_after_three_submissions(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    run, jf = runner(path)
    state = {}
    h = history(str(path))
    for attempt in range(3):
        run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
        node = next(iter(state.values()))["danmu"]
        assert node["attempts"] == attempt + 1
        assert not DanmuMaintenance.due(node)
        run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
        assert len(jf.writes) == attempt + 1
        node["check_after"] = "2000-01-01T00:00:00"
        assert DanmuMaintenance.confirm_due(state, logging.getLogger("test"), RunResult())
        assert node["status"] == ("retry" if attempt < 2 else "attention")
        node["check_after"] = "2000-01-01T00:00:00"
    run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert len(jf.writes) == 3


def test_xml_generated_but_ass_missing_has_bounded_file_only_wait(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    run, jf = runner(path)
    state = {}
    run.run([history(str(path))], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    path.with_suffix(".xml").write_text('<i><d p="1">弹幕</d></i>', encoding="utf-8")
    node = next(iter(state.values()))["danmu"]
    for index in range(3):
        node["check_after"] = "2000-01-01T00:00:00"
        DanmuMaintenance.confirm_due(state, logging.getLogger("test"), RunResult())
        assert node["status"] == ("submitted" if index < 2 else "attention")
    assert jf.writes == ["71"] and jf.match_calls == 1


def test_disabled_auto_download_uses_single_item_request(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    run, jf = runner(path)
    settings = deepcopy(SETTINGS)
    settings["DownloadOption"]["EnableAutoDownload"] = False
    jf.danmu_settings = lambda: settings
    requests_ = []
    jf.request_danmu = requests_.append
    run.run([history(str(path))], MoviePilotReorganizer, {}, RunResult(operation_limit=1), 15, [])
    assert jf.writes == ["71"] and requests_ == ["item"]


def test_different_network_urls_preserve_jellyfin_settings_and_use_mp_route(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    run, jf = runner(path)
    settings = deepcopy(SETTINGS)
    settings["DanmuApi"]["ServerUrl"] = "https://other.test/token"
    original = deepcopy(settings)
    jf.danmu_settings = lambda: settings
    run.run([history(str(path))], MoviePilotReorganizer, {}, RunResult(operation_limit=1), 15, [])
    assert jf.writes == ["71"] and jf.match_calls == 1
    assert run.logvar.session.calls[0][1] == "https://logvar.test/token/api/v2/match"
    assert settings == original


def test_missing_jellyfin_api_configuration_never_writes(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    run, jf = runner(path)
    settings = deepcopy(SETTINGS)
    settings["DanmuApi"]["ServerUrl"] = ""
    jf.danmu_settings = lambda: settings
    with pytest.raises(RuntimeError, match="尚未配置"):
        run.run([history(str(path))], MoviePilotReorganizer, {}, RunResult(operation_limit=1), 15, [])
    assert not jf.writes and not run.logvar.session.calls


@pytest.mark.parametrize("preview_exists", [False, True])
def test_danmu_uses_actual_video_not_unexecuted_preview_and_recovers_false_attention(tmp_path, preview_exists):
    path = tmp_path / "第7集.mp4"
    path.touch()
    preview = tmp_path / "正式标题.mp4"
    if preview_exists:
        preview.touch()
    h = history(str(path))
    key = MoviePilotReorganizer.processing_key(h)
    state = {key: {"expected_path": str(preview), "danmu": {
        "status": "attention", "path": str(preview), "attempts": 3,
        "error": "未找到当前视频文件",
    }}}
    run, jf = runner(path)
    run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert jf.writes == ["71"]
    assert state[key]["danmu"]["path"] == str(path)
    assert state[key]["danmu"]["status"] == "submitted"
    assert state[key]["danmu"]["attempts"] == 1


@pytest.mark.parametrize("rename_attempts", [0, 1])
def test_preview_fallback_requires_a_submitted_rename_and_existing_video(tmp_path, rename_attempts):
    old_path = tmp_path / "old.mp4"
    new_path = tmp_path / "new.mp4"
    new_path.touch()
    h = history(str(old_path))
    key = MoviePilotReorganizer.processing_key(h)
    state = {key: {"expected_path": str(new_path), "rename_attempts": rename_attempts}}
    run, jf = runner(new_path)
    run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert jf.writes == (["71"] if rename_attempts else [])
    assert state[key]["danmu"]["path"] == str(new_path if rename_attempts else old_path)


@pytest.mark.parametrize("failure,expected_calls", [
    (requests.ConnectionError("https://logvar.test/secret-token"), 3),
    (requests.Timeout("https://logvar.test/secret-token"), 3),
    (requests.HTTPError(response=SimpleNamespace(status_code=503)), 3),
    (requests.HTTPError(response=SimpleNamespace(status_code=429)), 3),
    (requests.HTTPError(response=SimpleNamespace(status_code=401)), 1),
    (requests.HTTPError(response=SimpleNamespace(status_code=403)), 1),
    (requests.HTTPError(response=SimpleNamespace(status_code=404)), 1),
    (requests.exceptions.SSLError("https://logvar.test/secret-token"), 1),
])
def test_service_failures_stop_batch_without_counting_unattempted_media(tmp_path, failure, expected_calls):
    histories = []
    for index in range(50):
        path = tmp_path / f"video{index}.mp4"
        path.touch()
        histories.append(history(str(path), id=index + 1))
    run, jf = runner(histories[0].dest)
    run.logvar.session.failure = failure
    jf.match_recent_media = lambda targets, *_: {
        JellyfinServiceClient.path_key(target.path): [EpisodeItem(target.path, path=target.path)]
        for target in targets
    }
    jf.danmu_item = lambda path: {
        "Id": path, "Type": "Episode", "Path": path, "ProviderIds": {},
        "ParentIndexNumber": 1, "IndexNumber": 7,
    }
    state = {}
    result = RunResult(operation_limit=10)
    run.run(histories, MoviePilotReorganizer, state, result, 15, [])
    assert len(run.logvar.session.calls) == expected_calls
    assert not jf.writes and result.operations_used == 0
    nodes = [state[MoviePilotReorganizer.processing_key(h)]["danmu"] for h in histories]
    assert [node["attempts"] for node in nodes] == [1] * expected_calls + [0] * (50 - expected_calls)
    assert all(node["status"] == "retry" and not DanmuMaintenance.due(node) for node in nodes)
    assert result.danmu_waiting == 50
    assert "本轮暂停" in result.summary() and "secret-token" not in result.summary()


def test_successful_response_resets_consecutive_service_failures(tmp_path):
    histories = []
    for index in range(5):
        path = tmp_path / f"video{index}.mp4"
        path.touch()
        histories.append(history(str(path), id=index + 1))
    run, jf = runner(histories[0].dest, dry_run=True)
    jf.match_recent_media = lambda targets, *_: {
        JellyfinServiceClient.path_key(target.path): [EpisodeItem(target.path, path=target.path)]
        for target in targets
    }
    jf.danmu_item = lambda path: {
        "Id": path, "Type": "Episode", "Path": path, "ProviderIds": {},
        "ParentIndexNumber": 1, "IndexNumber": 7,
    }
    original = run.logvar.match
    calls = []
    def match(h, **kwargs):
        calls.append(h)
        if len(calls) != 3:
            raise LogVarRequestError("LogVar 连接失败")
        return original(h, **kwargs)
    run.logvar.match = match
    result = RunResult(operation_limit=1)
    run.run(histories, MoviePilotReorganizer, {}, result, 15, [])
    assert len(calls) == 5 and result.danmu_previewed == 1
    assert "本轮暂停" not in result.summary()


def test_danmu_only_completion_does_not_complete_future_metadata_work():
    state = {"key": {"history_id": 1, "danmu_only": True, "danmu": {"status": "complete"}}}
    assert RecentEpisodeMaintenance._tracked_history_ids(state) == set()
    state["key"]["danmu"]["status"] = "retry"
    assert RecentEpisodeMaintenance._tracked_history_ids(state) == {1}
    state["key"]["danmu"]["status"] = "complete"
    state["key"]["status"] = "monitoring"
    assert RecentEpisodeMaintenance._tracked_history_ids(state) == {1}


def test_matching_connection_failure_has_backoff(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    run, jf = runner(path)
    def unavailable(*args):
        raise RuntimeError("https://jellyfin.test/?api_key=secret")
    jf.match_recent_media = unavailable
    state = {}
    result = RunResult(operation_limit=1)
    run.run([history(str(path))], MoviePilotReorganizer, state, result, 15, [])
    node = next(iter(state.values()))["danmu"]
    assert node["status"] == "retry" and not DanmuMaintenance.due(node)
    assert "secret" not in result.summary()
    assert not jf.writes


def test_ten_minute_confirmation_never_calls_mp_or_jellyfin(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    path.with_suffix(".xml").write_text('<i><d p="1">弹幕</d></i>', encoding="utf-8")
    state = {"key": {"status": "complete", "danmu": {
        "status": "submitted", "path": str(path), "check_after": "2000-01-01T00:00:00", "to_ass": False,
    }}}
    plugin = RecentEpisodeMaintenance()
    plugin._enabled = True
    plugin._enable_danmu = True
    plugin._dry_run = False
    plugin._max_items = 1
    plugin._notify = False
    plugin._load_processing_state = lambda: deepcopy(state)
    plugin._save_processing_state = lambda value: state.update(deepcopy(value)) or True
    plugin.run_once = lambda: pytest.fail("file confirmation must not run the MP workflow")
    plugin._get_jellyfin_client = lambda: pytest.fail("file confirmation must not call Jellyfin")
    plugin.run_due_follow_up()
    assert state["key"]["danmu"]["status"] == "complete"
    assert not plugin._run_active


def test_danmu_only_main_workflow_never_previews_and_prunes_deleted_history(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.touch()
    h = history(str(path), date="2999-01-01 00:00:00")
    fake = SimpleNamespace(
        compatibility_error=lambda: "", recent_histories=lambda **_: [h],
        processing_key=MoviePilotReorganizer.processing_key,
        target_path=MoviePilotReorganizer.target_path,
        display_name=MoviePilotReorganizer.display_name,
        media_type=MoviePilotReorganizer.media_type,
        preview=lambda _: pytest.fail("Danmu-only mode must not preview"),
    )
    monkeypatch.setattr(plugin_module, "MoviePilotReorganizer", lambda **_: fake)
    monkeypatch.setattr(plugin_module, "LogVarClient", lambda _: LogVarClient("https://logvar.test/token", Session()))
    jf = Jellyfin(path)
    plugin = RecentEpisodeMaintenance()
    plugin._enable_refresh = plugin._enable_reorganize = False
    plugin._enable_danmu = True
    plugin._danmu_api_url = "https://logvar.test/token"
    plugin._max_items = 1
    plugin._days = 15
    plugin._dry_run = plugin._notify = False
    plugin._library_ids = []
    state = {"deleted": {"history_id": 99, "status": "complete", "danmu": {"status": "retry"}}}
    plugin._load_processing_state = lambda: deepcopy(state)
    def save(value):
        state.clear()
        state.update(deepcopy(value))
        return True
    plugin._save_processing_state = save
    plugin._get_jellyfin_client = lambda: jf
    plugin._run_once()
    assert "deleted" not in state
    node = state[MoviePilotReorganizer.processing_key(h)]
    assert node.get("status") != "complete"
    assert node["danmu"]["status"] == "submitted" and jf.writes == ["71"]
