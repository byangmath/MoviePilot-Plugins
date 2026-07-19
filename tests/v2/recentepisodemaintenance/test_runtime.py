from recentepisodemaintenance import RecentEpisodeMaintenance
from recentepisodemaintenance.models import RunResult
import recentepisodemaintenance as plugin_module


def test_skipped_count_is_unique_per_episode():
    result = RunResult()

    result.add_skipped("/library/show/episode01.mkv")
    result.add_skipped("/library/show/episode01.mkv")
    result.add_skipped("/library/show/episode02.mkv")

    assert result.skipped == 2


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
