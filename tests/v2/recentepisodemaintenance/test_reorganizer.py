from types import SimpleNamespace

from recentepisodemaintenance.reorganizer import MoviePilotReorganizer
from recentepisodemaintenance import RecentEpisodeMaintenance


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
        date=date,
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


def test_attachment_histories_do_not_consume_video_inspection_limit():
    plugin = RecentEpisodeMaintenance()
    plugin._max_items = 1
    reorganizer = MoviePilotReorganizer(logger=None)
    candidates = []

    for index in range(12):
        subtitle = history(
            history_id=100 + index,
            source=f"/source/show/subtitle-{index}.srt",
            dest=f"/library/show/Season 01/subtitle-{index}.srt",
            date=f"2026-07-19 12:{index:02d}:00",
        )
        subtitle.episodes = f"E{20 + index:02d}"
        candidates.append(subtitle)

    for index in range(6):
        video = history(
            history_id=index + 1,
            source=f"/source/show/episode-{index + 1}.mkv",
            dest=f"/library/show/Season 01/episode-{index + 1}.mkv",
            date=f"2026-07-19 11:{index:02d}:00",
        )
        video.episodes = f"E{index + 1:02d}"
        candidates.append(video)

    video_pool = reorganizer._select_primary_histories(candidates)
    selected, _, _ = plugin._select_histories(
        histories=video_pool,
        reorganizer=reorganizer,
    )

    assert len(video_pool) == 6
    assert len(selected) == 5
    assert all(reorganizer._is_video_history(item) for item in selected)
