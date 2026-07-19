from types import SimpleNamespace

from recentepisodemaintenance.reorganizer import MoviePilotReorganizer


def history(
    *,
    history_id: int,
    dest: str,
    source: str,
    date: str,
    download_hash: str = "same-transfer",
):
    return SimpleNamespace(
        id=history_id,
        src=source,
        dest=dest,
        src_fileitem={"path": source},
        dest_fileitem={"path": dest},
        src_storage="local",
        dest_storage="local",
        mode="link",
        status=True,
        title="测试剧",
        year="2026",
        tmdbid=123,
        doubanid=None,
        seasons="S01",
        episodes="E01",
        download_hash=download_hash,
    )


def test_prefers_video_history_when_newer_record_is_subtitle():
    reorganizer = MoviePilotReorganizer(logger=None)
    subtitle = history(
        history_id=2,
        source="/source/show/episode.zh-tw.srt",
        dest="/library/show/Season 01/测试剧 S01E01.zh-tw.srt",
        date="2026-07-19 12:01:00",
    )
    video = history(
        history_id=1,
        source="/source/show/episode.mkv",
        dest="/library/show/Season 01/测试剧 S01E01.mkv",
        date="2026-07-19 12:00:59",
    )

    selected = reorganizer._select_primary_histories([subtitle, video])

    assert selected == [video]
    assert reorganizer.related_history_count(video) == 1


def test_does_not_attach_history_from_another_download():
    reorganizer = MoviePilotReorganizer(logger=None)
    video = history(
        history_id=2,
        source="/source/show/new/episode.mkv",
        dest="/library/show/Season 01/测试剧 S01E01.mkv",
        date="2026-07-19 12:01:00",
        download_hash="new-transfer",
    )
    old_subtitle = history(
        history_id=1,
        source="/source/show/old/episode.srt",
        dest="/library/show/Season 01/测试剧 S01E01.srt",
        date="2026-07-19 11:00:00",
        download_hash="old-transfer",
    )

    selected = reorganizer._select_primary_histories([video, old_subtitle])

    assert selected == [video]
    assert reorganizer.related_history_count(video) == 0


def test_ignores_episode_group_without_video_history():
    reorganizer = MoviePilotReorganizer(logger=None)
    subtitle = history(
        history_id=1,
        source="/source/show/episode.srt",
        dest="/library/show/Season 01/测试剧 S01E01.srt",
        date="2026-07-19 12:00:00",
    )

    assert reorganizer._select_primary_histories([subtitle]) == []
