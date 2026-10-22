from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
from types import SimpleNamespace

import pytest
import requests
import recentepisodemaintenance as plugin_module

from recentepisodemaintenance import RecentEpisodeMaintenance
from recentepisodemaintenance.danmu import (
    DanmuMaintenance, DanmuMatchError, DanmuUnavailableError, DanmuRequestLimiter,
    DanmuRateStateError, LogVarClient, LogVarRequestError, api_base, safe_error,
)
from recentepisodemaintenance.jellyfin_client import DanmuSubmissionError, JellyfinServiceClient
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
        self.comment_payload = {"success": True, "count": 1, "comments": [{"p": "1,1,25,16777215", "m": "弹幕"}]}
        self.comment_failure = None
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
        if "/comment/" in url:
            if self.comment_failure:
                raise self.comment_failure
            payload = self.comment_payload
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


def test_metadata_update_preserves_editable_fields_and_excludes_display_data():
    dto = {"Id": "item", "Type": "Episode", "Name": "山雨欲来", "Path": "/video.mp4",
           "Overview": "简介", "ProviderIds": {"Tmdb": "123", "DanmuID": "bilibili:old"},
           "LockData": True, "Tags": ["动画"], "Genres": ["动画"], "LockedFields": [],
           "IndexNumber": 7, "ParentIndexNumber": 1,
           "ForcedSortName": "排序名", "OriginalTitle": "Original", "CriticRating": 80,
           "CommunityRating": 8.5, "PreferredMetadataLanguage": "zh", "PreferredMetadataCountryCode": "CN",
           "OfficialRating": "TV-14", "CustomRating": "TV-PG", "ProductionYear": 2026,
           "DateCreated": "2026-01-01T00:00:00Z", "PremiereDate": "2026-01-01T00:00:00Z",
           "EndDate": None, "ProductionLocations": ["中国"], "Taglines": ["标语"],
           "AirsAfterSeasonNumber": 1, "AirsBeforeEpisodeNumber": 8, "AirsBeforeSeasonNumber": 2,
           "AspectRatio": "16:9", "Video3DFormat": None,
           "People": [{"Name": "演员", "Role": "角色", "Type": "Actor", "PrimaryImageTag": "image"}],
           "Studios": [{"Name": "工作室", "Id": "studio-id"}],
           "Trickplay": {"media": {"320": {"Width": 320}}},
           "UserData": {"PlaybackPositionTicks": 123}, "MediaSources": [{"Id": "source"}],
           "MediaStreams": [{"Type": "Video"}], "ImageTags": {"Primary": "image"}}
    original = deepcopy(dto)
    service = Service(dto)
    client = JellyfinServiceClient(service)
    item = client.danmu_item("item")
    client.set_danmu_id(item, "71")
    payload = service.posts[0][1]
    assert payload["ProviderIds"] == {"Tmdb": "123", "DanmuID": "danmuapi:71"}
    payload["ProviderIds"] = original["ProviderIds"]
    expected = {key: value for key, value in original.items()
                if key not in {"Trickplay", "UserData", "MediaSources", "MediaStreams", "ImageTags"}}
    expected["People"] = [{"Name": "演员", "Role": "角色", "Type": "Actor"}]
    expected["Studios"] = [{"Name": "工作室"}]
    assert payload == expected
    assert dto == original
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


def test_playback_and_trickplay_changes_do_not_block_danmu_write():
    dto = {"Id": "item", "Type": "Movie", "Name": "电影", "Path": "/video.mkv", "ProviderIds": {},
           "LockData": False, "Tags": [], "Genres": [], "LockedFields": [],
           "UserData": {"PlaybackPositionTicks": 1}, "Trickplay": {"old": {}}}
    service = Service(dto)
    client = JellyfinServiceClient(service)
    original = client.danmu_item("item")
    service.dto["UserData"]["PlaybackPositionTicks"] = 2
    service.dto["Trickplay"] = {"new": {}}
    client.set_danmu_id(original, "20")
    assert len(service.posts) == 1
    assert "UserData" not in service.posts[0][1] and "Trickplay" not in service.posts[0][1]


@pytest.mark.parametrize("field,value", [
    ("Path", "/renamed.mkv"), ("Overview", "新简介"), ("ProviderIds", {"Tmdb": "new"}),
    ("LockData", True), ("Tags", ["新标签"]), ("Genres", ["新类型"]),
    ("LockedFields", ["Name"]), ("IndexNumber", 8),
])
def test_editable_metadata_changes_abort_before_post(field, value):
    dto = {"Id": "item", "Type": "Movie", "Name": "电影", "Path": "/video.mkv", "ProviderIds": {},
           "LockData": False, "Tags": [], "Genres": [], "LockedFields": []}
    service = Service(dto)
    client = JellyfinServiceClient(service)
    original = client.danmu_item("item")
    service.dto[field] = value
    with pytest.raises(DanmuSubmissionError) as failure:
        client.set_danmu_id(original, "20")
    assert not failure.value.uncertain and not service.posts


@pytest.mark.parametrize("saved,readable", [(False, True), (True, True), (False, False)])
def test_failed_write_reads_back_once_without_blind_repost(saved, readable):
    dto = {"Id": "item", "Type": "Movie", "Name": "电影", "Path": "/video.mkv", "ProviderIds": {},
           "LockData": False, "Tags": [], "Genres": [], "LockedFields": []}
    service = Service(dto)
    client = JellyfinServiceClient(service)
    original = client.danmu_item("item")
    posts = []
    def post(url, **kwargs):
        posts.append(url)
        if saved:
            service.dto["ProviderIds"]["DanmuID"] = "danmuapi:20"
        if not readable:
            service.get_data = lambda _: None
        raise requests.HTTPError("HTTP 500 at https://jf.test/?api_key=secret",
                                 response=SimpleNamespace(status_code=500))
    service.post_data = post
    with pytest.raises(DanmuSubmissionError) as failure:
        client.set_danmu_id(original, "20")
    assert failure.value.uncertain == (saved or not readable)
    assert failure.value.status_code == 500 and len(posts) == 1
    assert len(service.gets) == (4 if readable else 3)
    assert original["ProviderIds"] == {}


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_http_rejection_does_not_wait_for_file_confirmation(status):
    dto = {"Id": "item", "Type": "Movie", "Name": "电影", "Path": "/video.mkv", "ProviderIds": {},
           "LockData": False, "Tags": [], "Genres": [], "LockedFields": []}
    service = Service(dto)
    client = JellyfinServiceClient(service)
    item = client.danmu_item("item")
    def post(*args, **kwargs):
        raise requests.HTTPError(response=SimpleNamespace(status_code=status))
    service.post_data = post
    with pytest.raises(DanmuSubmissionError) as failure:
        client.set_danmu_id(item, "20")
    assert not failure.value.uncertain and failure.value.status_code == status
    assert len(service.gets) == 3


def test_timeout_with_negative_readback_is_still_uncertain():
    dto = {"Id": "item", "Type": "Movie", "Name": "电影", "Path": "/video.mkv",
           "ProviderIds": {}, "LockData": False, "Tags": [], "Genres": [], "LockedFields": []}
    service = Service(dto)

    def timed_out(*args, **kwargs):
        raise requests.Timeout("request still running")

    service.post_data = timed_out
    client = JellyfinServiceClient(service)
    item = client.danmu_item("item")
    with pytest.raises(DanmuSubmissionError) as failure:
        client.set_danmu_id(item, "71")
    assert failure.value.uncertain
    assert "暂未发现目标 DanmuID" in str(failure.value)
    assert len(service.gets) == 4


@pytest.mark.parametrize("response,uncertain", [
    ({"success": True}, None), ({"success": False}, False), ({}, True), ("not-json", True),
])
def test_single_item_request_requires_queue_acknowledgement(response, uncertain):
    client = JellyfinServiceClient(SimpleNamespace(get_data=lambda _: response))
    if uncertain is None:
        client.request_danmu("item")
    else:
        with pytest.raises(DanmuSubmissionError) as failure:
            client.request_danmu("item")
        assert failure.value.uncertain == uncertain


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


@pytest.mark.parametrize("payload,expected", [
    ({"comments": [], "count": 0}, DanmuUnavailableError),
    ({"comments": []}, DanmuUnavailableError),
    ({"comments": [], "count": 1}, LogVarRequestError),
    ({"comments": [], "count": False}, LogVarRequestError),
    ({"comments": [], "count": "0"}, LogVarRequestError),
    ({"count": 0}, LogVarRequestError),
    ({"success": False, "comments": [], "count": 0}, LogVarRequestError),
    ({"errorCode": 500, "comments": [], "count": 0}, LogVarRequestError),
])
def test_availability_requires_an_explicit_valid_empty_result(payload, expected):
    session = Session()
    client = LogVarClient("https://logvar.test/token", session)
    match = client.match(history(), movie=False, settings=SETTINGS)
    session.comment_payload = payload
    with pytest.raises(expected):
        client.ensure_comments_available(match)
    assert session.calls[-1][1].endswith("/api/v2/comment/71")
    assert session.calls[-1][2]["params"] == {"format": "json"}


def retry_manual_state(state):
    plugin = RecentEpisodeMaintenance()
    plugin.get_data = lambda _: deepcopy(state)
    plugin.save_data = lambda _, value: state.update(deepcopy(value))
    assert plugin._retry_attention_records() == 1


def test_empty_source_stops_automatically_and_manual_retry_does_not_reset_evidence(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    h = history(str(path))
    key = MoviePilotReorganizer.processing_key(h)
    state = {key: {"status": "complete"}}
    run, jf = runner(path)
    run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    node = state[key]["danmu"]
    node["check_after"] = "2000-01-01T00:00:00"
    DanmuMaintenance.confirm_due(state, logging.getLogger("test"), RunResult())
    assert node["verify_comments"] and jf.writes == ["71"]
    run.logvar.session.comment_payload = {"count": 0, "comments": []}
    for number in (1, 2, 3):
        node["check_after"] = "2000-01-01T00:00:00"
        result = RunResult(operation_limit=1)
        run.run([h], MoviePilotReorganizer, state, result, 15, [])
        assert node["unavailable_count"] == number
        assert node["status"] == ("retry" if number < 3 else "attention")
        assert result.operations_used == result.danmu_submitted == 0
        assert result.failed == 0 and result.danmu_unavailable == 1
    assert "已停止自动重试" in node["error"] and "返回 0 条" in node["error"]
    before = len(run.logvar.session.calls)
    run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert len(run.logvar.session.calls) == before
    retry_manual_state(state)
    node = state[key]["danmu"]
    assert node["unavailable_count"] == 3 and node["verify_comments"]
    assert node["manual_rechecks_remaining"] == 2
    run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert node["status"] == "retry" and node["unavailable_count"] == 4
    assert node["manual_rechecks_remaining"] == 1
    node["check_after"] = "2000-01-01T00:00:00"
    run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert node["status"] == "attention" and node["unavailable_count"] == 5
    assert node["manual_rechecks_remaining"] == 0
    assert jf.writes == ["71"]
    retry_manual_state(state)
    run.logvar.session.comment_payload = {"count": 1, "comments": [{"p": "1", "m": "available"}]}
    run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert state[key]["danmu"]["status"] == "submitted"
    assert state[key]["danmu"]["unavailable_count"] == 0
    assert not state[key]["danmu"]["manual_recheck"]
    assert "manual_rechecks_remaining" not in state[key]["danmu"]
    assert jf.writes == ["71", "71"]


def test_repeated_no_match_stops_and_survives_manual_reset(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    h = history(str(path))
    key = MoviePilotReorganizer.processing_key(h)
    state = {key: {"status": "complete"}}
    run, jf = runner(path)
    run.logvar.session.match_override = {"isMatched": False}
    for number in (1, 2, 3):
        if number > 1:
            state[key]["danmu"]["check_after"] = "2000-01-01T00:00:00"
        run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert state[key]["danmu"]["status"] == "attention"
    retry_manual_state(state)
    run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert state[key]["danmu"]["status"] == "retry"
    state[key]["danmu"]["check_after"] = "2000-01-01T00:00:00"
    run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert state[key]["danmu"]["status"] == "attention"
    assert state[key]["danmu"]["unavailable_count"] == 5
    assert not jf.writes
    assert not any("/comment/" in call[1] for call in run.logvar.session.calls)


@pytest.mark.parametrize("dry_run", [False, True])
def test_old_missing_xml_attention_requires_probe_but_dry_run_does_not_download(tmp_path, dry_run):
    path = tmp_path / "video.mp4"
    path.touch()
    h = history(str(path))
    key = MoviePilotReorganizer.processing_key(h)
    state = {key: {"status": "complete", "danmu": {
        "status": "attention", "path": str(path), "attempts": 3, "error": "提交后未生成 XML",
    }}}
    retry_manual_state(state)
    assert state[key]["danmu"]["verify_comments"]
    run, jf = runner(path, dry_run=dry_run)
    run.logvar.session.comment_payload = {"count": 0, "comments": []}
    result = RunResult(operation_limit=1)
    run.run([h], MoviePilotReorganizer, state, result, 15, [])
    calls = [call for call in run.logvar.session.calls if "/comment/" in call[1]]
    assert len(calls) == (0 if dry_run else 1)
    assert not jf.writes and result.operations_used == 0
    if not dry_run:
        assert state[key]["danmu"]["status"] == "retry"
        assert state[key]["danmu"]["unavailable_count"] == 1
        assert state[key]["danmu"]["manual_rechecks_remaining"] == 1


@pytest.mark.parametrize("failure", [requests.Timeout(), requests.HTTPError(response=SimpleNamespace(status_code=429))])
def test_comment_network_failure_is_not_counted_as_empty(tmp_path, failure):
    path = tmp_path / "video.mp4"
    path.touch()
    h = history(str(path))
    key = MoviePilotReorganizer.processing_key(h)
    state = {key: {"status": "complete", "danmu": {
        "status": "ready", "path": str(path), "attempts": 0, "verify_comments": True,
        "unavailable_count": 1,
    }}}
    run, jf = runner(path)
    run.logvar.session.comment_failure = failure
    run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert state[key]["danmu"]["status"] == "retry"
    assert state[key]["danmu"]["unavailable_count"] == 1
    assert "返回 0 条" not in state[key]["danmu"]["error"]
    assert not jf.writes


@pytest.mark.parametrize("limited", [False, True])
def test_comment_checks_have_a_per_run_cap_and_failures_stop_batch(tmp_path, limited):
    paths = [tmp_path / f"video-{index}.mp4" for index in range(5)]
    for path in paths:
        path.touch()
    histories = [history(str(path), id=index + 1) for index, path in enumerate(paths)]
    state = {MoviePilotReorganizer.processing_key(h): {"status": "complete", "danmu": {
        "status": "ready", "path": h.dest, "attempts": 0, "verify_comments": True,
    }} for h in histories}
    run, jf = runner(paths[0])
    jf.match_recent_media = lambda targets, *_: {
        JellyfinServiceClient.path_key(target.path): [EpisodeItem(target.path, path=target.path)]
        for target in targets
    }
    jf.danmu_item = lambda path: {"Id": path, "Type": "Episode", "Path": path,
                                "ProviderIds": {}, "ParentIndexNumber": 1, "IndexNumber": 7}
    if limited:
        run.logvar.session.comment_failure = requests.HTTPError(response=SimpleNamespace(status_code=429))
    else:
        run.logvar.session.comment_payload = {"count": 0, "comments": []}
    result = RunResult(operation_limit=10 if limited else 2)
    run.run(histories, MoviePilotReorganizer, state, result, 15, [])
    assert sum("/comment/" in call[1] for call in run.logvar.session.calls) == (3 if limited else 2)
    assert not jf.writes and result.operations_used == 0
    assert ("本轮暂停" in result.summary()) == limited
    nodes = [state[MoviePilotReorganizer.processing_key(h)]["danmu"] for h in histories]
    if limited:
        assert all(not node.get("unavailable_count") for node in nodes)
    else:
        assert [node.get("unavailable_count", 0) for node in nodes] == [1, 1, 0, 0, 0]


@pytest.mark.parametrize("uncertain", [False, True])
def test_submission_failure_classification_keeps_concrete_safe_error(tmp_path, caplog, uncertain):
    path = tmp_path / "video.mp4"
    path.touch()
    h = history(str(path))
    key = MoviePilotReorganizer.processing_key(h)
    run, jf = runner(path)
    calls = []

    def fail_write(dto, provider_value):
        calls.append(provider_value)
        raise DanmuSubmissionError(
            "HTTP 500 Trickplay https://server.test/api?api_key=private-token",
            uncertain=uncertain,
        )

    jf.set_danmu_id = fail_write
    state = {}
    result = RunResult(operation_limit=1)
    with caplog.at_level(logging.INFO, logger="test"):
        run.run([h], MoviePilotReorganizer, state, result, 15, [])
    node = state[key]["danmu"]
    assert node["status"] == ("submitted" if uncertain else "retry")
    assert node["attempts"] == 1
    assert "HTTP 500 Trickplay" in node["error"]
    assert "private-token" not in str(state) + caplog.text + str(result.errors)
    assert ("单项弹幕补全等待确认" in caplog.text) == uncertain
    assert result.failed == 1 and result.danmu_submitted == 0
    assert result.operations_used == 1
    run.run([h], MoviePilotReorganizer, state, RunResult(operation_limit=1), 15, [])
    assert len(calls) == 1
    if uncertain:
        node["check_after"] = "2000-01-01T00:00:00"
        DanmuMaintenance.confirm_due(state, logging.getLogger("test"), RunResult())
        assert node["status"] == "retry"
        assert "HTTP 500 Trickplay" in node["error"]
        assert len(calls) == 1


@pytest.mark.parametrize("status,expected_requests", [(500, 3), (401, 1)])
def test_jellyfin_submission_failures_pause_batch_without_penalizing_unrequested(tmp_path, status, expected_requests):
    paths = [tmp_path / f"video-{index}.mp4" for index in range(5)]
    for path in paths:
        path.touch()
    histories = [history(str(path), id=index + 1) for index, path in enumerate(paths)]
    run, jf = runner(paths[0])
    jf.match_recent_media = lambda targets, *_: {
        JellyfinServiceClient.path_key(target.path): [EpisodeItem(target.path, path=target.path)]
        for target in targets
    }
    jf.danmu_item = lambda item_id: {
        "Id": item_id, "Type": "Episode", "Path": item_id, "Name": "正确标题",
        "ProviderIds": {}, "ParentIndexNumber": 1, "IndexNumber": 7,
    }
    calls = []
    response = requests.Response()
    response.status_code = status

    def fail_write(dto, provider_value):
        calls.append(dto["Path"])
        cause = requests.HTTPError(f"HTTP {status}", response=response)
        raise DanmuSubmissionError(f"写入失败：HTTP {status}", uncertain=False, cause=cause)

    jf.set_danmu_id = fail_write
    state = {}
    result = RunResult(operation_limit=10)
    run.run(histories, MoviePilotReorganizer, state, result, 15, [])
    assert len(calls) == result.operations_used == expected_requests
    assert result.danmu_submitted == 0
    for index, h in enumerate(histories):
        node = state[MoviePilotReorganizer.processing_key(h)]["danmu"]
        assert node["status"] == "retry"
        assert node["attempts"] == (1 if index < expected_requests else 0)
        assert not DanmuMaintenance.due(node)
    assert any("暂停后续弹幕请求" in error for error in result.errors)


@pytest.mark.parametrize("message", [
    "HTTP 500 https://server.test/token-secret/api",
    "api_key=token-secret token:token-secret access_token=token-secret",
    "Authorization: Bearer token-secret",
    "Authorization: Basic token-secret",
])
def test_submission_error_redacts_urls_and_credentials(message):
    assert "token-secret" not in safe_error(RuntimeError(message))


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
    rate_state = {}
    plugin.get_data = lambda _: deepcopy(rate_state) or None
    plugin.save_data = lambda _, value: rate_state.update(deepcopy(value))
    plugin._run_once()
    assert "deleted" not in state
    node = state[MoviePilotReorganizer.processing_key(h)]
    assert node.get("status") != "complete"
    assert node["danmu"]["status"] == "submitted" and jf.writes == ["71"]


def limiter_store(clock):
    store = {}
    def save(value):
        store.clear()
        store.update(deepcopy(value))
    def factory():
        return DanmuRequestLimiter(lambda: deepcopy(store) or None, save, lambda: clock[0])
    return store, factory


def test_durable_rate_window_spaces_submissions_across_instances():
    clock = [datetime(2026, 10, 20, 0, 0)]
    store, factory = limiter_store(clock)
    first, second = factory(), factory()
    assert first.reserve() is None
    assert second.reserve() == "2026-10-20T00:00:21"
    for second_number in (21, 42):
        clock[0] = clock[0].replace(second=second_number)
        assert factory().reserve() is None
    clock[0] = datetime(2026, 10, 20, 0, 1)
    assert first.reserve() == "2026-10-20T00:01:03"
    assert len(store["events"]) == 3
    clock[0] += timedelta(seconds=3)
    assert second.reserve() is None
    assert len(store["events"]) == 3


def test_content_probe_and_download_share_rate_capacity():
    clock = [datetime(2026, 10, 20)]
    store, factory = limiter_store(clock)
    assert factory().reserve(2) is None
    clock[0] += timedelta(seconds=21)
    assert factory().reserve(2) == "2026-10-20T00:01:01"
    assert factory().reserve(1) is None
    clock[0] += timedelta(seconds=21)
    assert factory().reserve(1) == "2026-10-20T00:01:01"
    assert sum(event["cost"] for event in store["events"]) == 3


def test_slow_requests_keep_their_rate_reservation_after_completion():
    clock = [datetime(2026, 10, 20)]
    store, factory = limiter_store(clock)
    assert factory().reserve(2) is None
    clock[0] += timedelta(seconds=90)
    factory().succeeded()
    assert factory().reserve() == "2026-10-20T00:01:51"
    clock[0] += timedelta(seconds=21)
    assert factory().reserve() is None
    clock[0] += timedelta(seconds=21)
    assert factory().reserve() == "2026-10-20T00:02:31"


@pytest.mark.parametrize("value", [[], {"events": [{"at": "bad", "cost": 1}]},
                                      {"events": [{"at": "2026-10-20", "cost": 0}]}])
def test_invalid_rate_state_fails_closed(value):
    limiter = DanmuRequestLimiter(lambda: value, lambda _: pytest.fail("must not save invalid data"))
    with pytest.raises(DanmuRateStateError):
        limiter.reserve()


@pytest.mark.parametrize("raises", [False, True])
def test_rate_checkpoint_failure_prevents_submission(tmp_path, raises):
    path = tmp_path / "video.mp4"
    path.touch()
    run, jf = runner(path)
    def save(_):
        if raises:
            raise OSError("database unavailable")
        return False
    run.limiter = DanmuRequestLimiter(lambda: None, save)
    state, result = {}, RunResult(operation_limit=10)
    run.run([history(str(path))], MoviePilotReorganizer, state, result, 15, [])
    assert not jf.writes and not run.logvar.session.calls
    assert result.failed == 1
    assert next(iter(state.values()))["danmu"]["attempts"] == 0


def test_rate_circuit_survives_worker_runs_and_resets_after_success():
    clock = [datetime(2026, 10, 20)]
    store, factory = limiter_store(clock)
    for _ in range(3):
        factory().failed()
    assert factory().reserve() == "2026-10-20T01:00:00"
    clock[0] += timedelta(hours=1)
    assert factory().reserve() is None
    factory().succeeded()
    assert store["failures"] == 0 and store["cooldown_until"] is None
    factory().failed(immediate=True)
    assert factory().reserve() == "2026-10-20T02:00:00"


def test_rate_deferred_items_are_not_attempted_or_matched(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    h = history(str(path))
    clock = [datetime.now()]
    _, factory = limiter_store(clock)
    run, jf = runner(path)
    run.limiter = factory()
    assert run.limiter.reserve() is None
    state, result = {}, RunResult(operation_limit=10)
    run.run([h], MoviePilotReorganizer, state, result, 15, [])
    node = state[MoviePilotReorganizer.processing_key(h)]["danmu"]
    assert node["dispatch_pending"] and node["attempts"] == 0
    assert result.operations_used == result.failed == 0
    assert not jf.match_calls and not jf.writes and not run.logvar.session.calls
    node["check_after"] = "2000-01-01T00:00:00"
    clock[0] += timedelta(seconds=21)
    run.run([h], MoviePilotReorganizer, state, result, 15, [])
    assert jf.writes == ["71"] and node["attempts"] == 1


def test_rate_limited_batch_sends_only_one_and_queues_the_rest(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    items = [history(str(path), id=i) for i in (1, 2, 3)]
    run, jf = runner(path)
    _, factory = limiter_store([datetime.now()])
    run.limiter = factory()
    state, result = {}, RunResult(operation_limit=10)
    run.run(items, MoviePilotReorganizer, state, result, 15, [])
    assert jf.writes == ["71"] and result.danmu_submitted == 1
    waiting = [item["danmu"] for item in state.values() if item["danmu"]["status"] != "submitted"]
    assert len(waiting) == 2
    assert all(node["dispatch_pending"] and node["attempts"] == 0 for node in waiting)


def test_completed_danmu_is_not_reopened_until_daily_check(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.touch()
    h = history(str(path))
    run, jf = runner(path)
    state = {MoviePilotReorganizer.processing_key(h): {"danmu": {
        "status": "complete", "path": str(path), "check_after": DanmuMaintenance.after(1440),
    }}}
    monkeypatch.setattr(run, "files", lambda *_: pytest.fail("daily check not yet due"))
    result = RunResult(operation_limit=1)
    run.run([h], MoviePilotReorganizer, state, result, 15, [])
    assert result.danmu_checked == 0 and jf.match_calls == 0


def test_unavailable_attention_is_separate_from_failures():
    state = {"a": {"danmu": {"status": "attention", "unavailable": True, "label": "No data"}},
             "b": {"danmu": {"status": "attention", "label": "Invalid match"}}}
    result = RunResult()
    DanmuMaintenance._counts(state, result)
    assert len(result.danmu_unavailable_items) == len(result.danmu_attention) == 1
    assert "No data" in result.danmu_unavailable_items[0]
    assert "Invalid match" in result.danmu_attention[0]
    assert result.failed == 0


@pytest.mark.parametrize("error,marker,expected", [
    ("LogVar 已匹配到媒体，但当前来源返回 0 条弹幕；累计确认 8 次", None, True),
    ("LogVar 尚未匹配到目标媒体；累计确认 3 次", None, True),
    ("LogVar 请求失败（HTTP 429）", None, False),
    ("弹幕目录暂不可访问", None, False),
    ("LogVar 尚未匹配到目标媒体", False, False),
])
def test_legacy_no_data_classification_uses_current_evidence_not_only_counter(error, marker, expected):
    node = {"status": "attention", "error": error, "unavailable_count": 8}
    if marker is not None:
        node["unavailable"] = marker
    result = RunResult()
    DanmuMaintenance._counts({"old": {"danmu": node}}, result)
    assert bool(result.danmu_unavailable_items) is expected
    assert bool(result.danmu_attention) is not expected
    assert "unavailable" not in node if marker is None else node["unavailable"] is marker


def test_danmu_queue_worker_only_reads_queued_ids_and_prunes_deleted(monkeypatch):
    h = history("/library/video.mp4")
    key = MoviePilotReorganizer.processing_key(h)
    state = {
        key: {"history_id": 1, "status": "complete", "danmu": {"status": "ready"}},
        "deleted": {"history_id": 2, "status": "complete", "danmu": {"status": "retry"}},
        "monitor": {"history_id": 3, "status": "monitoring"},
        "busy": {"history_id": 4, "status": "pending_refresh", "danmu": {"status": "ready"}},
    }
    queries, maintained, saves = [], [], []
    def by_ids(ids):
        queries.append(ids)
        return [h]
    fake = SimpleNamespace(histories_by_ids=by_ids, processing_key=MoviePilotReorganizer.processing_key)
    monkeypatch.setattr(plugin_module, "MoviePilotReorganizer", lambda **_: fake)
    plugin = RecentEpisodeMaintenance()
    plugin._enabled = plugin._enable_danmu = True
    plugin._dry_run = False
    plugin._max_items = 10
    plugin.get_data = lambda _: None
    plugin._load_processing_state = lambda: deepcopy(state)
    plugin._checkpoint_processing_state = lambda value: saves.append(deepcopy(value)) or True
    plugin._maintain_danmu = lambda histories, *_: maintained.append(histories)
    plugin.run_danmu_queue()
    assert queries == [{1, 2}] and maintained == [[h]]
    assert "deleted" not in saves[-1] and "monitor" in saves[-1] and "busy" in saves[-1]
    assert not plugin._run_active


def test_idle_or_overlapping_danmu_worker_does_not_query_mp(monkeypatch):
    monkeypatch.setattr(plugin_module, "MoviePilotReorganizer", lambda **_: pytest.fail("no MP call"))
    plugin = RecentEpisodeMaintenance()
    plugin._enabled = plugin._enable_danmu = True
    plugin._dry_run = False
    plugin._load_processing_state = lambda: {}
    plugin.run_danmu_queue()
    plugin._run_active = True
    plugin.run_danmu_queue()
    assert not plugin._pending_run


def test_cooling_danmu_worker_does_not_query_mp_or_jellyfin(monkeypatch):
    monkeypatch.setattr(plugin_module, "MoviePilotReorganizer", lambda **_: pytest.fail("no MP query during cooldown"))
    plugin = RecentEpisodeMaintenance()
    plugin._enabled = plugin._enable_danmu = True
    plugin._dry_run = False
    clock = [datetime.now()]
    _, factory = limiter_store(clock)
    factory().failed(immediate=True)
    plugin._danmu_request_limiter = factory
    plugin._get_jellyfin_client = lambda: pytest.fail("no Jellyfin request during cooldown")
    state = {"key": {"history_id": 1, "status": "complete", "danmu": {"status": "retry", "attempts": 1}}}
    plugin._load_processing_state = lambda: deepcopy(state)
    saved = []
    plugin._checkpoint_processing_state = lambda value: saved.append(deepcopy(value)) or True
    plugin.run_danmu_queue()
    node = saved[-1]["key"]["danmu"]
    assert node["attempts"] == 1 and node["dispatch_pending"]
    assert datetime.fromisoformat(node["check_after"]) > clock[0] + timedelta(minutes=59)


def test_http_429_pauses_dispatch_across_runs(tmp_path):
    path = tmp_path / "video.mp4"
    path.touch()
    run, jf = runner(path)
    _, factory = limiter_store([datetime.now()])
    run.limiter = factory()
    run.logvar.session.failure = requests.HTTPError(response=SimpleNamespace(status_code=429))
    result, state = RunResult(operation_limit=10), {}
    run.run([history(str(path))], MoviePilotReorganizer, state, result, 15, [])
    assert result.failed == 1 and not jf.writes
    assert datetime.fromisoformat(factory().reserve()) > datetime.now() + timedelta(minutes=59)
