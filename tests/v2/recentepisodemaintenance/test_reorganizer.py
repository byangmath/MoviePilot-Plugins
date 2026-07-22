from datetime import datetime
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
    selected, state, _ = plugin._select_histories(
        histories=video_pool,
        reorganizer=reorganizer,
    )

    assert len(video_pool) == 6
    assert len(selected) == 5
    assert all(reorganizer._is_video_history(item) for item in selected)
    assert {item["history_id"] for item in state.values()} == set(range(1, 7))


def test_tracked_history_ids_keep_unfinished_records_only():
    plugin = RecentEpisodeMaintenance()

    tracked = plugin._tracked_history_ids(
        {
            "pending": {"status": plugin._STATE_PENDING_REFRESH, "history_id": 10},
            "monitoring": {"status": plugin._STATE_MONITORING, "history_id": "11"},
            "attention": {"status": plugin._STATE_ATTENTION, "history_id": 12},
            "complete": {"status": plugin._STATE_COMPLETE, "history_id": 13},
            "invalid": {"status": plugin._STATE_PENDING_REFRESH, "history_id": "x"},
        }
    )

    assert tracked == {10, 11, 12}


def test_recent_history_query_includes_tracked_records_outside_date_window():
    class Expression:
        def __init__(self, value):
            self.value = value

        def __or__(self, other):
            return Expression(("or", self.value, other.value))

    class Column:
        def __init__(self, name):
            self.name = name

        def __ge__(self, value):
            return Expression(("ge", self.name, value))

        def in_(self, values):
            return Expression(("in", self.name, set(values)))

        def is_(self, value):
            return Expression(("is", self.name, value))

        def isnot(self, value):
            return Expression(("isnot", self.name, value))

        def desc(self):
            return Expression(("desc", self.name))

    class HistoryModel:
        date = Column("date")
        id = Column("id")
        status = Column("status")
        seasons = Column("seasons")
        episodes = Column("episodes")

    class Query:
        def __init__(self):
            self.filters = []

        def filter(self, expression):
            self.filters.append(expression.value)
            return self

        def order_by(self, _expression):
            return self

        def all(self):
            return []

    query = Query()

    class Database:
        def query(self, model):
            assert model is HistoryModel
            return query

    class Session:
        def __enter__(self):
            return Database()

        def __exit__(self, *_args):
            return False

    reorganizer = MoviePilotReorganizer(logger=None)
    reorganizer._transfer_history_cls = HistoryModel
    reorganizer._get_db = object()
    reorganizer._db_session = Session

    assert reorganizer.recent_histories(15, tracked_history_ids={7, 8}) == []
    assert query.filters[0][0] == "or"
    assert query.filters[0][2] == ("in", "id", {7, 8})


def test_verified_monitoring_uses_adaptive_intervals():
    plugin = RecentEpisodeMaintenance()
    state = {"episode": {}}
    observed_hours = []

    for _ in range(4):
        before = datetime.now()
        plugin._mark_processing_verified(state, {"episode"})
        due = datetime.fromisoformat(state["episode"]["next_preview_at"])
        observed_hours.append(round((due - before).total_seconds() / 3600))

    assert observed_hours == [24, 48, 72, 72]
    assert state["episode"]["monitoring_checks"] == 4


def test_expired_healthy_record_completes_only_after_it_is_checked():
    plugin = RecentEpisodeMaintenance()
    plugin._days = 15
    state = {
        "episode": {
            "status": plugin._STATE_MONITORING,
            "history_id": 1,
            "history_date": "2026-01-01 00:00:00",
            "monitoring_checks": 3,
        }
    }

    assert plugin._tracked_history_ids(state) == {1}

    plugin._mark_processing_verified(state, {"episode"})

    assert state["episode"]["status"] == plugin._STATE_COMPLETE
    assert "monitoring_checks" not in state["episode"]
    assert plugin._tracked_history_ids(state) == set()


def test_refresh_cooldown_defers_record_without_dropping_it():
    plugin = RecentEpisodeMaintenance()
    plugin._max_items = 10
    reorganizer = MoviePilotReorganizer(logger=None)
    video = history(
        history_id=1,
        source="/source/show/episode.mkv",
        dest="/library/show/Season 01/测试剧 S01E01.mkv",
        date="2026-07-01 12:00:00",
    )
    key = reorganizer.processing_key(video)
    stored_state = {
        key: {
            "status": plugin._STATE_PENDING_REFRESH,
            "history_id": 1,
            "refresh_check_after": "2999-01-01T00:00:00",
        }
    }

    selected, state, selection = plugin._select_histories(
        histories=[video],
        reorganizer=reorganizer,
        state=stored_state,
    )

    assert selected == []
    assert selection["refresh_waiting"] == 1
    assert state[key]["history_id"] == 1

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
            "expected_path": "/library/show/Season 01/测试剧 S01E02 - 新标题.mkv",
        },
    }

    selected, _, selection = plugin._select_histories(
        histories=[due, waiting],
        reorganizer=reorganizer,
    )

    assert selected == [due]
    assert selection["pending"] == 1
    assert selection["sidecar_waiting"] == 1
    assert selection["sidecar_waiting_items"] == [
        "测试剧 S01E02｜文件：/library/show/Season 01/测试剧 S01E02 - 新标题.mkv"
    ]


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

def test_selection_defers_old_sidecar_cleanup_until_due():
    plugin = RecentEpisodeMaintenance()
    plugin._max_items = 10
    plugin._cleanup_old_sidecars = True
    reorganizer = MoviePilotReorganizer(logger=None)
    due = history(
        history_id=1,
        source="/source/show/due.mkv",
        dest="/library/show/Season 01/测试剧 S01E01.mkv",
        date="2026-07-19 12:00:00",
        download_hash="due-cleanup",
    )
    waiting = history(
        history_id=2,
        source="/source/show/waiting.mkv",
        dest="/library/show/Season 01/测试剧 S01E02.mkv",
        date="2026-07-19 12:01:00",
        download_hash="waiting-cleanup",
    )
    waiting.episodes = "E02"
    plugin._load_processing_state = lambda: {
        reorganizer.processing_key(due): {
            "status": plugin._STATE_PENDING_REORGANIZE,
            "cleanup_pending": True,
            "cleanup_check_after": "2000-01-01T00:00:00",
        },
        reorganizer.processing_key(waiting): {
            "status": plugin._STATE_PENDING_REORGANIZE,
            "cleanup_pending": True,
            "cleanup_check_after": "2999-01-01T00:00:00",
            "cleanup_old_media_path": "/library/show/Season 01/测试剧 S01E02 - 旧标题.mkv",
            "old_sidecars": [
                "/library/show/Season 01/测试剧 S01E02 - 旧标题.nfo",
                "/library/show/Season 01/测试剧 S01E02 - 旧标题.jpg",
            ],
        },
    }

    selected, _, selection = plugin._select_histories(
        histories=[due, waiting],
        reorganizer=reorganizer,
    )

    assert selected == [due]
    assert selection["pending"] == 1
    assert selection["cleanup_waiting"] == 1
    assert selection["cleanup_waiting_items"] == [
        "测试剧 S01E02｜旧文件：/library/show/Season 01/测试剧 S01E02 - 旧标题.mkv｜"
        "旧附件：/library/show/Season 01/测试剧 S01E02 - 旧标题.nfo；"
        "/library/show/Season 01/测试剧 S01E02 - 旧标题.jpg"
    ]


def test_verified_record_remains_pending_until_old_sidecar_cleanup():
    plugin = RecentEpisodeMaintenance()
    state = {
        "episode": {
            "status": plugin._STATE_PENDING_REORGANIZE,
            "had_action": True,
            "cleanup_pending": True,
            "old_sidecars": ["/library/show/old.nfo"],
        }
    }

    plugin._mark_processing_verified(state, {"episode"})

    assert state["episode"]["status"] == plugin._STATE_PENDING_REORGANIZE
    assert state["episode"]["cleanup_pending"] is True
    assert state["episode"]["old_sidecars"] == ["/library/show/old.nfo"]


def test_clearing_cleanup_pending_removes_cleanup_state():
    plugin = RecentEpisodeMaintenance()
    state = {
        "episode": {
            "status": plugin._STATE_PENDING_REORGANIZE,
            "cleanup_pending": True,
            "old_sidecars": ["/library/show/old.nfo"],
            "cleanup_old_media_path": "/library/show/old.mkv",
            "cleanup_check_after": "2999-01-01T00:00:00",
            "cleanup_passes": 1,
        }
    }

    plugin._mark_processing_state(
        state,
        {"episode"},
        plugin._STATE_PENDING_REORGANIZE,
        cleanup_pending=False,
    )

    assert "cleanup_pending" not in state["episode"]
    assert "old_sidecars" not in state["episode"]
    assert "cleanup_old_media_path" not in state["episode"]
    assert "cleanup_check_after" not in state["episode"]
    assert "cleanup_passes" not in state["episode"]
