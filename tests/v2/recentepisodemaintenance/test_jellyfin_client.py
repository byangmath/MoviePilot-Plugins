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
