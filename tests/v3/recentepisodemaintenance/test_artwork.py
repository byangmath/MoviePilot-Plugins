from base64 import b64decode
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace

import pytest

from recentepisodemaintenance import RecentEpisodeMaintenance
from recentepisodemaintenance import artwork as module
from recentepisodemaintenance.artwork import EpisodeArtwork
from recentepisodemaintenance.jellyfin_client import JellyfinServiceClient
from recentepisodemaintenance.models import EpisodeItem, RunResult
from recentepisodemaintenance.reorganizer import MoviePilotReorganizer


def history(number, **overrides):
    return SimpleNamespace(**({
        "id": number, "type": "tv", "media_source": "themoviedb", "media_id": "224839",
        "seasons": "S01", "episodes": f"E{number}", "dest": f"/tv/Example S01E{number}.mp4",
    } | overrides))


def test_reuses_preview_cache_and_queries_season_once(monkeypatch):
    calls = []
    refreshing = False

    @contextmanager
    def fresh():
        nonlocal refreshing
        refreshing = True
        try:
            yield
        finally:
            refreshing = False

    class TmdbChain:
        def tmdb_episodes(self, **kwargs):
            calls.append((refreshing, kwargs))
            return [SimpleNamespace(episode_number=i, id=i + 1000, still_path=f"/still{i}.jpg") for i in (187, 188)]

    monkeypatch.setattr(module, "_first_import", lambda candidates: {
        "TmdbChain": TmdbChain, "fresh": fresh,
    }[candidates[0][1]])
    season, number = EpisodeArtwork.identity(history(187))
    helper = EpisodeArtwork({season})
    assert helper.still_path(season, number, "1187") == "/still187.jpg"
    assert helper.still_path(season, 188, "1188") == "/still188.jpg"
    assert helper.still_path(season, 188, "1187") is None
    assert calls == [(False, {"tmdbid": 224839, "season": 1, "episode_group": None})]
    assert EpisodeArtwork(set()).still_path(season, 187) == "/still187.jpg"
    assert calls[-1][0] is True


@pytest.mark.parametrize("overrides", [
    {"episodes": "E187-E188"}, {"media_source": "douban"}, {"seasons": "S01-S02"},
    {"type": "movie", "seasons": None, "episodes": None},
])
def test_skips_unverified_episode_identity(overrides):
    assert EpisodeArtwork.identity(history(187, **overrides)) is None


def test_download_validates_jpeg_and_rejects_untrusted_path(monkeypatch):
    from PIL import Image
    import requests

    buffer = BytesIO()
    Image.new("RGB", (32, 16)).save(buffer, "JPEG")
    payload = buffer.getvalue()

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_content(self, **kwargs):
            yield payload

    calls = []
    monkeypatch.setattr(module, "_first_import", lambda _: None)
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: (calls.append((url, kwargs)), Response())[1])
    assert EpisodeArtwork.download("/still.jpg") == payload
    assert calls[0][0].startswith("https://image.tmdb.org/")
    assert not calls[0][1]["allow_redirects"]
    with pytest.raises(ValueError):
        EpisodeArtwork.download("//other.test/still.jpg")
    assert len(calls) == 1
    payload = b"<html>error</html>"
    with pytest.raises(OSError):
        EpisodeArtwork.download("/still.jpg")


def test_jellyfin_upload_checks_for_existing_image_and_confirms_write():
    class Service:
        images = []
        posts = 0

        def get_data(self, url):
            assert "/Images?" in url
            return self.images

        def post_data(self, url, *, data, headers):
            assert "/Images/Primary?" in url
            assert b64decode(data) == b"jpeg"
            assert headers == {"Content-Type": "image/jpeg"}
            self.posts += 1
            self.images = [{"ImageType": "Primary"}]
            return {"ok": True}

    service = Service()
    client = JellyfinServiceClient(service)
    assert client.add_primary_image_if_missing("item", b"jpeg") is True
    assert client.add_primary_image_if_missing("item", b"jpeg") is False
    assert service.posts == 1
    service.images = {"error": True}
    with pytest.raises(RuntimeError):
        client.add_primary_image_if_missing("item", b"jpeg")
    assert service.posts == 1


def test_missing_artwork_retries_on_due_run_and_stops_after_one(monkeypatch):
    plugin = RecentEpisodeMaintenance()
    plugin._enable_reorganize = False
    plugin._dry_run = False
    plugin._days = 15
    plugin._library_ids = ["library"]
    record = history(187)
    reorganizer = MoviePilotReorganizer(None)
    key = reorganizer.processing_key(record)
    path_key = JellyfinServiceClient.path_key(record.dest)
    entry = EpisodeItem("episode", path=record.dest, season_number=1, episode_number=187)
    state = {key: {"status": "complete"}}
    queries = []
    writes = []
    still = None

    def find_still(self, season, number, episode_id):
        queries.append(number)
        return still

    client = SimpleNamespace(
        path_key=JellyfinServiceClient.path_key,
        has_primary_image=lambda item: False,
        add_primary_image_if_missing=lambda item, data: (writes.append((item, data)), True)[1],
    )
    monkeypatch.setattr(EpisodeArtwork, "still_path", find_still)
    monkeypatch.setattr(EpisodeArtwork, "download", staticmethod(lambda path: b"jpeg"))

    def run(inspected):
        result = RunResult(operation_limit=1)
        plugin._maintain_episode_artwork(
            [record], reorganizer, state, result, inspected=inspected,
            client=client, matches={path_key: [entry]},
        )
        return result

    run({key})
    assert "artwork_check_after" in state[key]
    run({key})
    assert queries == [187]
    state[key]["artwork_check_after"] = "2000-01-01T00:00:00"
    still = "/still.jpg"
    plugin._dry_run = True
    assert run(set()).artwork_previewed == 1
    assert not writes and not state[key].get("artwork_done")
    plugin._dry_run = False
    assert run(set()).artwork_completed == 1
    assert state[key] == {"status": "complete", "artwork_done": True}
    still = "/newer.jpg"
    run({key})
    assert queries == [187, 187, 187]
    assert writes == [("episode", b"jpeg")]


def test_known_primary_image_never_queries_tmdb(monkeypatch):
    plugin = RecentEpisodeMaintenance()
    plugin._enable_reorganize = False
    plugin._dry_run = False
    plugin._days = 15
    record = history(187)
    reorganizer = MoviePilotReorganizer(None)
    path_key = JellyfinServiceClient.path_key(record.dest)
    key = reorganizer.processing_key(record)
    entry = EpisodeItem("episode", path=record.dest, season_number=1, episode_number=187, primary_image_tag="present")
    client = SimpleNamespace(path_key=JellyfinServiceClient.path_key)
    monkeypatch.setattr(EpisodeArtwork, "still_path", lambda *args: pytest.fail("unexpected TMDB query"))
    state = {key: {}}
    plugin._maintain_episode_artwork(
        [record], reorganizer, state, RunResult(operation_limit=1), inspected={key},
        client=client, matches={path_key: [entry]},
    )
    assert state[key] == {"artwork_done": True}
