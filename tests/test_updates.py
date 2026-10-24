import datetime
import time

from hyper_wispr import updates
from hyper_wispr.settings_ui import compute_stats

ASSETS = [
    {"name": "Hyper-Wispr-Setup-0.4.0.exe", "browser_download_url": "https://github.com/omar-184/Hyper-Wispr/releases/download/v0.4.0/Hyper-Wispr-Setup-0.4.0.exe"},
    {"name": "Hyper-Wispr-Setup-0.4.0-NVIDIA.exe", "browser_download_url": "https://github.com/omar-184/Hyper-Wispr/releases/download/v0.4.0/Hyper-Wispr-Setup-0.4.0-NVIDIA.exe"},
]


def test_version_comparison():
    assert updates.is_newer("v0.4.0", "0.3.0")
    assert updates.is_newer("0.10.0", "0.9.9")
    assert updates.is_newer("0.3.1", "0.3")
    assert not updates.is_newer("0.3.0", "0.3.0")
    assert not updates.is_newer("0.2.0", "0.3.0")  # never offers a downgrade
    assert not updates.is_newer("0.3.0", "0.3.0-test")
    assert not updates.is_newer("", "0.3.0")


def test_picks_the_installer_matching_this_build():
    assert updates.pick_asset(ASSETS, "GPU")["name"].endswith("-NVIDIA.exe")
    assert updates.pick_asset(ASSETS, "CPU")["name"] == "Hyper-Wispr-Setup-0.4.0.exe"
    assert updates.pick_asset([], "CPU") is None


def test_only_downloads_this_projects_releases():
    d = updates.Downloader()
    assert not d.start({"name": "Hyper-Wispr-Setup-9.exe", "url": "https://example.com/Hyper-Wispr-Setup-9.exe"})["ok"]
    assert not d.start({"name": "evil.exe", "url": updates.DOWNLOAD_PREFIX + "v9/evil.exe"})["ok"]
    assert d.progress()["state"] == "idle"


def test_install_refuses_files_outside_the_download_folder(tmp_path):
    fake = tmp_path / "Hyper-Wispr-Setup-9.exe"
    fake.write_bytes(b"MZ")
    assert not updates.install(str(fake))["ok"]


def test_stats_from_history():
    now = time.time()
    yesterday = now - 86400
    rows = [
        (now, 6.0, "one two three four five six"),  # 6 words in 6 s
        (yesterday, 6.0, "one two three four five six"),
        (now - 5 * 86400, 3.0, "one two three"),
        (now, 1.0, ""),  # nothing typed: not counted
    ]
    st = compute_stats(rows)
    assert st["dictations"] == 3 and st["words"] == 15
    assert st["today_words"] == 6 and st["week_words"] == 15
    assert st["wpm"] == 60
    assert st["streak"] == 2
    assert len(st["daily"]) == 14 and st["daily"][-1] == {"day": datetime.date.today().isoformat(), "words": 6}


def test_streak_survives_until_the_first_dictation_of_the_day():
    rows = [(time.time() - 86400, 2.0, "hello there"), (time.time() - 2 * 86400, 2.0, "hello there")]
    assert compute_stats(rows)["streak"] == 2
    assert compute_stats([])["streak"] == 0
