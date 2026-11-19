from __future__ import annotations

from contextlib import nullcontext
from io import BytesIO
import re
from typing import Any
from urllib.parse import urlsplit

from .reorganizer import MoviePilotReorganizer, _first_import


SeasonKey = tuple[int, int, str | None]


class EpisodeArtwork:
    _MAX_IMAGE_BYTES = 8 * 1024 * 1024

    def __init__(self, previewed_seasons: set[SeasonKey]):
        self._previewed_seasons = previewed_seasons
        self._season_cache: dict[SeasonKey, list[Any]] = {}

    @staticmethod
    def identity(history: Any) -> tuple[SeasonKey, int] | None:
        if MoviePilotReorganizer.media_type(history) != "tv":
            return None
        source = getattr(history, "media_source", None)
        source = str(getattr(source, "value", source) or "").casefold()
        tmdb_id = getattr(history, "tmdbid", None)
        if source in {"tmdb", "themoviedb"}:
            tmdb_id = getattr(history, "media_id", None) or tmdb_id
        if source not in {"", "tmdb", "themoviedb"}:
            return None
        season = MoviePilotReorganizer._season_number(getattr(history, "seasons", None))
        episode = re.fullmatch(r"E?(\d+)", str(getattr(history, "episodes", "") or "").strip(), re.I)
        if not str(tmdb_id or "").isdecimal() or season is None or season < 0 or not episode:
            return None
        return (int(tmdb_id), season, getattr(history, "episode_group", None) or None), int(episode[1])

    @staticmethod
    def _field(row: Any, name: str) -> Any:
        return row.get(name) if isinstance(row, dict) else getattr(row, name, None)

    def still_path(self, season: SeasonKey, number: int, episode_tmdb_id: Any = None) -> str | None:
        if season not in self._season_cache:
            # A cached response from a fresh preview avoids a second upstream request.
            # Seed before querying so an error is not retried once per episode.
            self._season_cache[season] = []
            chain = _first_import([("app.chain.tmdb", "TmdbChain")])
            if not chain or not callable(getattr(chain, "tmdb_episodes", None)):
                raise RuntimeError("MoviePilot 未提供分集查询接口")
            fresh = _first_import([("app.runtime.cache", "fresh")])
            context = fresh() if callable(fresh) and season not in self._previewed_seasons else nullcontext()
            with context:
                self._season_cache[season] = chain().tmdb_episodes(
                    tmdbid=season[0], season=season[1], episode_group=season[2],
                ) or []
        matches = [row for row in self._season_cache[season]
                   if str(self._field(row, "episode_number")) == str(number)]
        if len(matches) != 1:
            return None
        row = matches[0]
        row_id = self._field(row, "id")
        if episode_tmdb_id and str(row_id or "") != str(episode_tmdb_id):
            return None
        path = self._field(row, "still_path")
        return path if isinstance(path, str) and re.fullmatch(r"/[A-Za-z0-9_-]+\.jpg", path, re.I) else None

    @classmethod
    def download(cls, still_path: str) -> bytes:
        import requests
        from PIL import Image

        if not re.fullmatch(r"/[A-Za-z0-9_-]+\.jpg", still_path, re.I):
            raise ValueError("无效的剧照路径")
        get_setting = _first_import([("app.runtime.settings", "get_runtime_setting")])
        image_url = get_setting("TMDB_IMAGE_URL") if callable(get_setting) else None
        url = image_url(still_path, "w780") if callable(image_url) else f"https://image.tmdb.org/t/p/w780{still_path}"
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("TMDB 剧照地址不支持 HTTPS")
        with requests.get(
            url,
            timeout=(5, 10), stream=True, allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(f"下载剧照返回 HTTP {response.status_code}")
            payload = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                payload.extend(chunk)
                if len(payload) > cls._MAX_IMAGE_BYTES:
                    raise ValueError("剧照文件过大")
        with Image.open(BytesIO(payload)) as image:
            if image.format != "JPEG" or image.width * image.height > 4_000_000:
                raise ValueError("剧照不是有效的 JPEG")
            image.load()
        return bytes(payload)
