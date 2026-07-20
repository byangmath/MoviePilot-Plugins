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

def test_selection_counts_due_and_waiting_monitoring_records():
    plugin = RecentEpisodeMaintenance()
    plugin._max_items = 10
    reorganizer = MoviePilotReorganizer(logger=None)
    due = history(
        history_id=1,
        source="/source/show/due.mkv",
        dest="/library/show/Season 01/测试剧 S01E01.mkv",
        date="2026-07-19 12:00:00",
        download_hash="due-transfer",
    )
    waiting = history(
        history_id=2,
        source="/source/show/waiting.mkv",
        dest="/library/show/Season 01/测试剧 S01E02.mkv",
        date="2026-07-19 12:01:00",
        download_hash="waiting-transfer",
    )
    waiting.episodes = "E02"
    plugin._load_processing_state = lambda: {
        reorganizer.processing_key(due): {
            "status": plugin._STATE_MONITORING,
            "next_preview_at": "2000-01-01T00:00:00",
        },
        reorganizer.processing_key(waiting): {
            "status": plugin._STATE_MONITORING,
            "next_preview_at": "2999-01-01T00:00:00",
        },
    }

    selected, _, selection = plugin._select_histories(
        histories=[due, waiting],
        reorganizer=reorganizer,
    )

    assert selected == [due]
    assert selection["monitoring"] == 1
    assert selection["monitoring_waiting"] == 1

def test_selection_lists_attention_records_with_reason_and_path():
    plugin = RecentEpisodeMaintenance()
    plugin._max_items = 10
    reorganizer = MoviePilotReorganizer(logger=None)
    video = history(
        history_id=1,
        source="/source/show/episode.mkv",
        dest="/library/show/Season 01/测试剧 S01E01.mkv",
        date="2026-07-19 12:00:00",
    )
    key = reorganizer.processing_key(video)
    plugin._load_processing_state = lambda: {
        key: {
            "status": plugin._STATE_ATTENTION,
            "expected_path": "/library/show/Season 01/测试剧 S01E01.mkv",
            "sidecar_pending": True,
        }
    }

    selected, _, selection = plugin._select_histories(
        histories=[video],
        reorganizer=reorganizer,
    )

    assert selected == []
    assert selection["attention"] == 1
    assert selection["attention_items"] == [
        "测试剧 S01E01：刮削附件多次补齐失败｜"
        "文件：/library/show/Season 01/测试剧 S01E01.mkv"
    ]

def test_selection_defers_pending_sidecars_until_recheck_time():
    plugin = RecentEpisodeMaintenance()
    plugin._max_items = 10
    reorganizer = MoviePilotReorganizer(logger=None)
    due = history(
        history_id=1,
        source="/source/show/due.mkv",
        dest="/library/show/Season 01/测试剧 S01E01.mkv",
        date="2026-07-19 12:00:00",
        download_hash="due-sidecar",
    )
    waiting = history(
        history_id=2,
        source="/source/show/waiting.mkv",
        dest="/library/show/Season 01/测试剧 S01E02.mkv",
        date="2026-07-19 12:01:00",
        download_hash="waiting-sidecar",
    )
    waiting.episodes = "E02"
    plugin._load_processing_state = lambda: {
        reorganizer.processing_key(due): {
            "status": plugin._STATE_PENDING_REORGANIZE,
            "sidecar_pending": True,
            "sidecar_check_after": "2000-01-01T00:00:00",
        },
        reorganizer.processing_key(waiting): {
            "status": plugin._STATE_PENDING_REORGANIZE,
            "sidecar_pending": True,
            "sidecar_check_after": "2999-01-01T00:00:00",
        },
    }

    selected, _, selection = plugin._select_histories(
        histories=[due, waiting],
        reorganizer=reorganizer,
    )

    assert selected == [due]
    assert selection["pending"] == 1
    assert selection["sidecar_waiting"] == 1


def test_clearing_sidecar_pending_also_clears_recheck_time():
    plugin = RecentEpisodeMaintenance()
    state = {
        "episode": {
            "status": plugin._STATE_PENDING_REORGANIZE,
            "sidecar_pending": True,
            "sidecar_attempts": 1,
            "sidecar_check_after": "2999-01-01T00:00:00",
        }
    }

    plugin._mark_processing_state(
        state,
        {"episode"},
        plugin._STATE_PENDING_REORGANIZE,
        sidecar_pending=False,
    )

    assert state["episode"]["sidecar_pending"] is False
    assert "sidecar_check_after" not in state["episode"]

def test_selection_migrates_pending_sidecars_without_recheck_time():
    plugin = RecentEpisodeMaintenance()
    plugin._max_items = 10
    reorganizer = MoviePilotReorganizer(logger=None)
    video = history(
        history_id=1,
        source="/source/show/episode.mkv",
        dest="/library/show/Season 01/测试剧 S01E01.mkv",
        date="2026-07-19 12:00:00",
    )
    key = reorganizer.processing_key(video)
    plugin._load_processing_state = lambda: {
        key: {
            "status": plugin._STATE_PENDING_REORGANIZE,
            "sidecar_pending": True,
            "sidecar_attempts": 1,
        }
    }

    selected, state, selection = plugin._select_histories(
        histories=[video],
        reorganizer=reorganizer,
    )

    assert selected == []
    assert selection["sidecar_waiting"] == 1
    assert state[key]["sidecar_check_after"]
