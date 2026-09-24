import pytest

import recentepisodemaintenance.jellyfin_client as client_module
from recentepisodemaintenance import RecentEpisodeMaintenance
from recentepisodemaintenance.jellyfin_client import JellyfinServiceClient
from recentepisodemaintenance.models import EpisodeTarget


class UnavailableService:
    def __init__(self):
        self.calls = 0

    def get_data(self, _url):
        self.calls += 1
        return None


class HttpStatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.response = type("Response", (), {"status_code": status_code})()


class HttpErrorService:
    def __init__(self, status_code):
        self.status_code = status_code
        self.calls = 0

    def get_data(self, _url):
        self.calls += 1
        raise HttpStatusError(self.status_code)


def test_library_request_can_fail_without_retry(monkeypatch):
    service = UnavailableService()
    waits = []
    monkeypatch.setattr(client_module, "sleep", waits.append)

    with pytest.raises(RuntimeError):
        JellyfinServiceClient(service).libraries(retry=False)

    assert service.calls == 1
    assert waits == []


def test_background_library_request_keeps_bounded_retry(monkeypatch):
    service = UnavailableService()
    waits = []
    monkeypatch.setattr(client_module, "sleep", waits.append)

    with pytest.raises(RuntimeError):
        JellyfinServiceClient(service).libraries()

    assert service.calls == 3
    assert waits == [30, 30]


@pytest.mark.parametrize("status_code", [401, 403, 404, 422])
def test_client_errors_fail_without_retry(monkeypatch, status_code):
    service = HttpErrorService(status_code)
    waits = []
    monkeypatch.setattr(client_module, "sleep", waits.append)

    with pytest.raises(HttpStatusError):
        JellyfinServiceClient(service).libraries()

    assert service.calls == 1
    assert waits == []


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_transient_http_errors_keep_bounded_retry(monkeypatch, status_code):
    service = HttpErrorService(status_code)
    waits = []
    monkeypatch.setattr(client_module, "sleep", waits.append)

    with pytest.raises(HttpStatusError):
        JellyfinServiceClient(service).libraries()

    assert service.calls == 3
    assert waits == [30, 30]


def test_unknown_request_error_fails_without_retry(monkeypatch):
    class InvalidService:
        def __init__(self):
            self.calls = 0

        def get_data(self, _url):
            self.calls += 1
            raise ValueError("invalid response")

    service = InvalidService()
    waits = []
    monkeypatch.setattr(client_module, "sleep", waits.append)

    with pytest.raises(ValueError):
        JellyfinServiceClient(service).libraries()

    assert service.calls == 1
    assert waits == []


def test_form_library_options_disable_retry():
    plugin = RecentEpisodeMaintenance()

    class Client:
        def libraries(self, *, retry=True):
            assert retry is False
            return [{"title": "电视剧", "value": "tv"}]

    plugin._get_jellyfin_client = lambda: Client()

    assert plugin._library_options() == [
        {"title": "全部", "value": "__all__"},
        {"title": "电视剧", "value": "tv"},
    ]


def test_matches_movies_and_episodes_with_separate_jellyfin_item_types():
    class MediaService:
        def __init__(self):
            self.urls = []

        def get_data(self, url):
            self.urls.append(url)
            if "IncludeItemTypes=Movie" in url:
                return {
                    "Items": [{
                        "Id": "movie-1",
                        "Type": "Movie",
                        "Name": "挽救计划",
                        "Path": "/library/movies/挽救计划 (2026)/挽救计划 (2026).mkv",
                        "DateCreated": "2999-01-01T00:00:00Z",
                    }]
                }
            if "IncludeItemTypes=Episode" in url:
                return {
                    "Items": [{
                        "Id": "episode-1",
                        "Type": "Episode",
                        "Name": "正式标题",
                        "SeriesName": "测试剧",
                        "ParentIndexNumber": 1,
                        "IndexNumber": 1,
                        "Path": "/library/tv/测试剧/Season 01/测试剧 S01E01 - 正式标题.mkv",
                        "DateCreated": "2999-01-01T00:00:00Z",
                    }]
                }
            raise AssertionError(url)

    service = MediaService()
    client = JellyfinServiceClient(service)
    movie_path = "/library/movies/挽救计划 (2026)/挽救计划 (2026).mkv"
    episode_path = "/library/tv/测试剧/Season 01/测试剧 S01E01 - 正式标题.mkv"

    matches = client.match_recent_media(
        targets=[
            EpisodeTarget(path=movie_path, media_type="movie"),
            EpisodeTarget(path=episode_path, media_type="tv"),
        ],
        days=15,
    )

    assert matches[client.path_key(movie_path)][0].is_movie
    assert matches[client.path_key(episode_path)][0].is_episode
    assert any("IncludeItemTypes=Movie" in url for url in service.urls)
    assert any("IncludeItemTypes=Episode" in url for url in service.urls)
