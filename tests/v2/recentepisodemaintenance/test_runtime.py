from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from recentepisodemaintenance import RecentEpisodeMaintenance
from recentepisodemaintenance.jellyfin_client import JellyfinServiceClient
from recentepisodemaintenance.models import (
    EpisodeItem,
    EpisodeTarget,
    OperationResult,
    RunResult,
)
import recentepisodemaintenance as plugin_module


def test_skipped_count_is_unique_per_episode():
    result = RunResult()

    result.add_skipped("/library/show/episode01.mkv")
    result.add_skipped("/library/show/episode01.mkv")
    result.add_skipped("/library/show/episode02.mkv")

    assert result.skipped == 2


def test_episode_label_excludes_jellyfin_title_and_file():
    episode = EpisodeItem(
        item_id="1",
        name="第83集",
        series_name="搞笑一家人3",
        season_number=1,
        episode_number=83,
        path="/media/搞笑一家人3 S01E83 - 1080p 第83集.mkv",
    )

    assert episode.episode_label == "搞笑一家人3 S01E83"


def test_summary_lists_bare_full_file_paths():
    result = RunResult(refreshed=1, reorganized=1)
    result.add_refreshed_title("/media/搞笑一家人3 S01E83.mkv")
    result.add_reorganized_title("/media/仙逆 S01E149.mp4")
    result.add_error("刷新失败", "/media/秘密森林 S02E16.mkv")

    summary = result.summary()

    assert "- /media/搞笑一家人3 S01E83.mkv" in summary
    assert "- /media/仙逆 S01E149.mp4" in summary
    assert "处理失败剧集：" in summary
    assert "- /media/秘密森林 S02E16.mkv" in summary
    assert "失败详情：" in summary
    assert "- 刷新失败" in summary
    assert "Jellyfin 标题" not in summary
    assert "MP 预览文件" not in summary


def test_summary_includes_queue_counts():
    result = RunResult(
        queue_counts={
            "pending": 50,
            "new": 0,
            "monitoring": 1,
            "refresh_waiting": 6,
            "monitoring_waiting": 2,
            "sidecar_waiting": 3,
            "cleanup_waiting": 4,
            "complete": 26,
            "attention": 5,
        }
    )

    summary = result.summary()

    assert "队列：" in summary
    assert "当前状态：" in summary
    assert "本轮待复查 50 条" in summary
    assert "新记录 0 条" in summary
    assert "到期复查 1 条" in summary
    assert "等待刷新确认 6 条" in summary
    assert "等待复查 2 条" in summary
    assert "等待附件 3 条" in summary
    assert "等待清理 4 条" in summary
    assert "已完成 26 条" in summary
    assert "需人工检查 5 条" in summary


def test_waiting_records_text_spacing():
    assert RecentEpisodeMaintenance._waiting_records_text({}) == ""
    assert RecentEpisodeMaintenance._waiting_records_text(
        {"cleanup_waiting": 1}
    ) == "，另有 1 条记录等待旧附件清理"
    assert RecentEpisodeMaintenance._waiting_records_text(
        {"sidecar_waiting": 2, "cleanup_waiting": 1}
    ) == "，另有 2 条记录等待附件生成、1 条记录等待旧附件清理"
    assert RecentEpisodeMaintenance._waiting_records_text(
        {"refresh_waiting": 1}
    ) == "，另有 1 条记录等待刷新确认"
    assert RecentEpisodeMaintenance._waiting_records_text(
        {
            "sidecar_waiting": 1,
            "sidecar_waiting_items": [
                "测试剧 S01E01｜文件：/library/show/new.mkv"
            ],
            "cleanup_waiting": 1,
            "cleanup_waiting_items": [
                "测试剧 S01E02｜旧文件：/library/show/old.mkv｜旧附件：/library/show/old.nfo"
            ],
        }
    ) == (
        "，另有 1 条记录等待附件生成：测试剧 S01E01｜文件：/library/show/new.mkv、"
        "1 条记录等待旧附件清理：测试剧 S01E02｜旧文件：/library/show/old.mkv｜"
        "旧附件：/library/show/old.nfo"
    )


def test_notification_requires_action_or_failure():
    assert RunResult().should_notify() is False
    assert RunResult(actions_submitted=1).should_notify() is True
    assert RunResult(failed=1).should_notify() is True


def test_processing_checkpoint_saves_only_outside_dry_run():
    plugin = RecentEpisodeMaintenance()
    saved = []
    plugin._save_processing_state = saved.append

    plugin._dry_run = False
    plugin._checkpoint_processing_state({"episode": {"status": "pending"}})
    plugin._dry_run = True
    plugin._checkpoint_processing_state({"ignored": {}})

    assert saved == [{"episode": {"status": "pending"}}]


def test_reorganized_record_is_rechecked_by_jellyfin_before_completion(
    tmp_path,
    monkeypatch,
):
    old_media = tmp_path / "Show S01E01 - Old Title.mkv"
    new_media = tmp_path / "Show S01E01 - New Title.mkv"
    old_media.write_bytes(b"old")
    new_media.write_bytes(b"new")
    new_media.with_suffix(".nfo").write_text("metadata", encoding="utf-8")
    new_media.with_suffix(".jpg").write_bytes(b"image")
    history = SimpleNamespace(
        id=1,
        date="2026-07-22 00:00:00",
        dest=str(old_media),
    )

    class FakeReorganizer:
        reorganize_calls = 0

        def __init__(self, logger, dry_run):
            self.dry_run = dry_run

        def recent_histories(self, days, tracked_history_ids=None):
            return [history]

        @staticmethod
        def processing_key(_history):
            return "episode"

        @staticmethod
        def target_path(item):
            return Path(item.dest)

        @staticmethod
        def display_name(_history):
            return "Show S01E01"

        @staticmethod
        def episode_target(item):
            return EpisodeTarget(path=item.dest)

        @staticmethod
        def preview(_history):
            return OperationResult(success=True, target=new_media)

        @staticmethod
        def related_history_count(_history):
            return 0

        def reorganize(self, history, skip_same_name=True, preview_only=False):
            type(self).reorganize_calls += 1
            return OperationResult(success=True, target=new_media)

    episode = EpisodeItem(
        item_id="jf-1",
        name="New Title",
        series_name="Show",
        season_number=1,
        episode_number=1,
        path=str(old_media),
    )

    class FakeClient:
        refresh_calls = 0

        @staticmethod
        def path_key(path):
            return JellyfinServiceClient.path_key(path)

        def match_recent_episodes(self, targets, days, library_ids):
            return {self.path_key(target.path): [episode] for target in targets}

        def refresh_episode(self, **_kwargs):
            type(self).refresh_calls += 1

    plugin = RecentEpisodeMaintenance()
    plugin._enable_refresh = True
    plugin._enable_reorganize = True
    plugin._max_items = 10
    plugin._days = 15
    plugin._dry_run = False
    plugin._notify = False
    plugin._library_ids = []
    plugin._refresh_mode = "all"
    plugin._replace_images = True
    plugin._scan_after_reorganize = False
    plugin._cleanup_old_sidecars = False
    plugin._skip_same_name = True
    saved = []
    plugin._load_processing_state = lambda: deepcopy(saved[-1]) if saved else {
        "episode": {
            "status": plugin._STATE_PENDING_REFRESH,
            "history_id": 1,
            "history_date": history.date,
            "expected_path": str(new_media),
            "had_action": True,
            "refresh_check_after": "2000-01-01T00:00:00",
        }
    }
    plugin._save_processing_state = lambda state: saved.append(deepcopy(state))
    client = FakeClient()
    plugin._get_jellyfin_client = lambda: client
    monkeypatch.setattr(plugin_module, "MoviePilotReorganizer", FakeReorganizer)

    plugin._run_once()

    assert FakeReorganizer.reorganize_calls == 1
    assert FakeClient.refresh_calls == 0
    assert saved[-1]["episode"]["status"] == plugin._STATE_PENDING_REORGANIZE
    assert saved[-1]["episode"]["sidecar_pending"] is False

    history.dest = str(new_media)
    episode.path = str(new_media)
    episode.name = "Wrong Title"
    plugin._run_once()

    assert FakeClient.refresh_calls == 1
    assert FakeReorganizer.reorganize_calls == 1
    assert saved[-1]["episode"]["status"] == plugin._STATE_PENDING_REFRESH
    assert saved[-1]["episode"]["refresh_check_after"] > "2000-01-01T00:00:00"


def test_sidecar_checks_scan_shared_directory_once(tmp_path, monkeypatch):
    first = tmp_path / "Show S01E01.mkv"
    second = tmp_path / "Show S01E02.mkv"
    for media in (first, second):
        media.write_bytes(b"video")
        media.with_suffix(".nfo").write_text("metadata", encoding="utf-8")
        media.with_suffix(".jpg").write_bytes(b"image")

    real_scandir = plugin_module.scandir
    scan_count = 0

    def counting_scandir(directory):
        nonlocal scan_count
        scan_count += 1
        return real_scandir(directory)

    monkeypatch.setattr(plugin_module, "scandir", counting_scandir)
    cache = {}

    assert RecentEpisodeMaintenance._missing_reorganized_sidecars(first, cache) == []
    assert RecentEpisodeMaintenance._missing_reorganized_sidecars(second, cache) == []
    assert scan_count == 1


def test_sidecar_checks_preserve_case_sensitive_file_names(tmp_path):
    media = tmp_path / "Show S01E01.mkv"
    media.write_bytes(b"video")
    media.with_suffix(".NFO").write_text("metadata", encoding="utf-8")
    media.with_suffix(".JPG").write_bytes(b"image")

    assert RecentEpisodeMaintenance._missing_reorganized_sidecars(media) == [
        "NFO",
        "\u56fe\u7247",
    ]


def test_sidecar_checks_do_not_treat_matching_directories_as_files(tmp_path):
    media = tmp_path / "Show S01E01.mkv"
    media.mkdir()
    media.with_suffix(".nfo").mkdir()
    media.with_suffix(".jpg").mkdir()

    assert RecentEpisodeMaintenance._missing_reorganized_sidecars(media) == [
        "\u76ee\u6807\u6587\u4ef6",
        "NFO",
        "\u56fe\u7247",
    ]

def test_snapshot_old_sidecars_records_only_known_attachments(tmp_path):
    old_media = tmp_path / "Show S01E01 - Old title.mkv"
    new_media = tmp_path / "Show S01E01 - New title.mkv"
    old_media.write_bytes(b"video")
    expected = [
        old_media.with_suffix(".nfo"),
        old_media.with_suffix(".jpg"),
        tmp_path / f"{old_media.stem}-thumb.jpg",
        tmp_path / f"{old_media.stem}.zh-CN.srt",
        old_media.with_suffix(".xml"),
        tmp_path / f"{old_media.stem}.danmu.ass",
        tmp_path / f"{old_media.stem}.trickplay",
    ]
    for path in expected[:-1]:
        path.write_text("sidecar", encoding="utf-8")
    expected[-1].mkdir()
    (tmp_path / "season.nfo").write_text("shared", encoding="utf-8")
    (tmp_path / f"{old_media.stem}-unknown.txt").write_text(
        "unrelated",
        encoding="utf-8",
    )

    snapshot = RecentEpisodeMaintenance._snapshot_old_sidecars(
        old_media,
        new_media,
    )

    assert snapshot == sorted(str(path) for path in expected)
    assert str(old_media) not in snapshot
    assert str(tmp_path / "season.nfo") not in snapshot


def test_cleanup_snapshot_preserves_subtitle_until_new_name_exists(tmp_path):
    old_media = tmp_path / "Show S01E01 - Old title.mkv"
    new_media = tmp_path / "Show S01E01 - New title.mkv"
    old_nfo = old_media.with_suffix(".nfo")
    old_image = old_media.with_suffix(".jpg")
    old_subtitle = tmp_path / f"{old_media.stem}.zh-CN.srt"
    for path in (old_nfo, old_image, old_subtitle):
        path.write_text("old", encoding="utf-8")

    snapshot = [str(old_nfo), str(old_image), str(old_subtitle)]
    deleted, renamed, issues = RecentEpisodeMaintenance._cleanup_old_sidecar_snapshot(
        snapshot,
        old_media,
        new_media,
    )

    assert deleted == [str(old_nfo), str(old_image)]
    assert issues == [f"{old_subtitle.name} 缺少新名称对应文件"]
    assert old_subtitle.is_file()

    new_subtitle = tmp_path / f"{new_media.stem}.zh-CN.srt"
    new_subtitle.write_text("new", encoding="utf-8")
    old_nfo.write_text("recreated", encoding="utf-8")
    deleted, renamed, issues = RecentEpisodeMaintenance._cleanup_old_sidecar_snapshot(
        snapshot,
        old_media,
        new_media,
    )

    assert deleted == [str(old_nfo), str(old_subtitle)]
    assert issues == []
    assert new_subtitle.is_file()


def test_cleanup_snapshot_rejects_path_outside_old_media_directory(tmp_path):
    media_dir = tmp_path / "library"
    other_dir = tmp_path / "other"
    media_dir.mkdir()
    other_dir.mkdir()
    old_media = media_dir / "Show S01E01 - Old title.mkv"
    new_media = media_dir / "Show S01E01 - New title.mkv"
    outside = other_dir / f"{old_media.stem}.nfo"
    outside.write_text("keep", encoding="utf-8")

    deleted, renamed, issues = RecentEpisodeMaintenance._cleanup_old_sidecar_snapshot(
        [str(outside)],
        old_media,
        new_media,
    )

    assert deleted == []
    assert issues == [f"{outside.name} 未通过路径校验"]
    assert outside.is_file()

def test_cleanup_snapshot_removes_only_exact_trickplay_directory(tmp_path):
    old_media = tmp_path / "Show S01E01 - Old title.mkv"
    new_media = tmp_path / "Show S01E01 - New title.mkv"
    old_trickplay = tmp_path / f"{old_media.stem}.trickplay"
    other_trickplay = tmp_path / "Other episode.trickplay"
    old_trickplay.mkdir()
    other_trickplay.mkdir()
    (old_trickplay / "0.jpg").write_bytes(b"frame")
    (other_trickplay / "0.jpg").write_bytes(b"keep")

    deleted, renamed, issues = RecentEpisodeMaintenance._cleanup_old_sidecar_snapshot(
        [str(old_trickplay)],
        old_media,
        new_media,
    )

    assert deleted == [str(old_trickplay)]
    assert issues == []
    assert not old_trickplay.exists()
    assert other_trickplay.is_dir()

def test_cleanup_snapshot_renames_danmu_when_new_name_is_missing(tmp_path):
    old_media = tmp_path / "Show S01E01 - Old title.mkv"
    new_media = tmp_path / "Show S01E01 - New title.mkv"
    old_danmu = [
        old_media.with_suffix(".xml"),
        tmp_path / f"{old_media.stem}.danmu.ass",
    ]
    new_danmu = [
        new_media.with_suffix(".xml"),
        tmp_path / f"{new_media.stem}.danmu.ass",
    ]
    for path in old_danmu:
        path.write_text("danmu", encoding="utf-8")

    deleted, renamed, issues = RecentEpisodeMaintenance._cleanup_old_sidecar_snapshot(
        [str(path) for path in old_danmu],
        old_media,
        new_media,
    )

    assert deleted == []
    assert renamed == [str(path) for path in new_danmu]
    assert issues == []
    assert all(not path.exists() for path in old_danmu)
    assert all(path.read_text(encoding="utf-8") == "danmu" for path in new_danmu)

    for path in old_danmu:
        path.write_text("recreated", encoding="utf-8")
    deleted, renamed, issues = RecentEpisodeMaintenance._cleanup_old_sidecar_snapshot(
        [str(path) for path in old_danmu],
        old_media,
        new_media,
    )

    assert deleted == [str(path) for path in old_danmu]
    assert renamed == []
    assert issues == []
    assert all(path.read_text(encoding="utf-8") == "danmu" for path in new_danmu)


def test_protects_danmu_before_reorganization_and_finalizes_it(tmp_path):
    old_media = tmp_path / "Show S01E01 - Old title.mkv"
    new_media = tmp_path / "Show S01E01 - New title.mkv"
    old_danmu = [
        old_media.with_suffix(".xml"),
        tmp_path / f"{old_media.stem}.danmu.ass",
    ]
    ordinary_subtitle = tmp_path / f"{old_media.stem}.zh-CN.ass"
    for path in old_danmu:
        path.write_text("old danmu", encoding="utf-8")
    ordinary_subtitle.write_text("subtitle", encoding="utf-8")

    snapshot = [str(path) for path in [*old_danmu, ordinary_subtitle]]
    protected, issues = RecentEpisodeMaintenance._protect_danmu_sidecars(
        snapshot,
        old_media,
    )

    assert protected == [str(path) for path in old_danmu]
    assert issues == []
    assert all(not path.exists() for path in old_danmu)
    assert ordinary_subtitle.is_file()
    assert all(
        RecentEpisodeMaintenance._danmu_protection_path(path, old_media).is_file()
        for path in old_danmu
    )

    recovered_snapshot = RecentEpisodeMaintenance._snapshot_old_sidecars(
        old_media,
        new_media,
    )
    assert all(str(path) in recovered_snapshot for path in old_danmu)
    deleted, renamed, issues = RecentEpisodeMaintenance._cleanup_old_sidecar_snapshot(
        protected,
        old_media,
        new_media,
    )

    assert deleted == []
    assert renamed == [
        str(new_media.with_suffix(".xml")),
        str(tmp_path / f"{new_media.stem}.danmu.ass"),
    ]
    assert issues == []
    assert all(
        not RecentEpisodeMaintenance._danmu_protection_path(path, old_media).exists()
        for path in old_danmu
    )


def test_protected_danmu_keeps_new_files_and_can_restore_old_names(tmp_path):
    old_media = tmp_path / "Show S01E01 - Old title.mkv"
    new_media = tmp_path / "Show S01E01 - New title.mkv"
    old_danmu = [
        old_media.with_suffix(".xml"),
        tmp_path / f"{old_media.stem}.danmu.ass",
    ]
    new_danmu = [
        new_media.with_suffix(".xml"),
        tmp_path / f"{new_media.stem}.danmu.ass",
    ]
    for path in old_danmu:
        path.write_text("old danmu", encoding="utf-8")

    protected, issues = RecentEpisodeMaintenance._protect_danmu_sidecars(
        [str(path) for path in old_danmu],
        old_media,
    )
    for path in new_danmu:
        path.write_text("new danmu", encoding="utf-8")

    deleted, renamed, issues = RecentEpisodeMaintenance._cleanup_old_sidecar_snapshot(
        protected,
        old_media,
        new_media,
    )

    assert len(deleted) == 2
    assert renamed == []
    assert issues == []
    assert all(path.read_text(encoding="utf-8") == "new danmu" for path in new_danmu)

    for path in old_danmu:
        path.write_text("restore me", encoding="utf-8")
    protected, issues = RecentEpisodeMaintenance._protect_danmu_sidecars(
        [str(path) for path in old_danmu],
        old_media,
    )
    deleted, renamed, issues = RecentEpisodeMaintenance._cleanup_old_sidecar_snapshot(
        protected,
        old_media,
        old_media,
    )

    assert deleted == []
    assert renamed == [str(path) for path in old_danmu]
    assert issues == []
    assert all(path.read_text(encoding="utf-8") == "restore me" for path in old_danmu)

def test_danmu_settlement_uses_existing_new_video_after_interrupted_run(tmp_path):
    old_media = tmp_path / "Show S01E01 - Old title.mkv"
    new_media = tmp_path / "Show S01E01 - New title.mkv"
    new_media.write_bytes(b"new video")
    skipped = OperationResult(
        success=False,
        skipped=True,
        target=old_media,
    )

    assert RecentEpisodeMaintenance._danmu_settle_target(
        skipped,
        old_media,
        new_media,
    ) == new_media
    new_media.unlink()
    successful = OperationResult(
        success=True,
        target=new_media,
    )
    assert RecentEpisodeMaintenance._danmu_settle_target(
        successful,
        old_media,
        new_media,
    ) is None
