from recentepisodemaintenance import RecentEpisodeMaintenance
from recentepisodemaintenance.models import EpisodeItem, OperationResult, RunResult
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