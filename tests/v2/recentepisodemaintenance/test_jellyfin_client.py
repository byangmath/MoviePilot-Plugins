import pytest

import recentepisodemaintenance.jellyfin_client as client_module
from recentepisodemaintenance import RecentEpisodeMaintenance
from recentepisodemaintenance.jellyfin_client import JellyfinServiceClient


class UnavailableService:
    def __init__(self):
        self.calls = 0

    def get_data(self, _url):
        self.calls += 1
        return None


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
