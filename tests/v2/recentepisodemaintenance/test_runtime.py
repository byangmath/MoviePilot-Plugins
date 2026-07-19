from recentepisodemaintenance import RecentEpisodeMaintenance
from recentepisodemaintenance.models import EpisodeItem, RunResult
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
