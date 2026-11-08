"""End-to-end tests against a local HTTP server (no YouTube access needed).

yt-dlp's generic extractor treats a direct .mp4 link as a single video and an RSS feed
as a playlist, which exercises the same code paths as YouTube videos and playlists.
"""

import functools
import http.server
import shutil
import subprocess
import threading
import time

import pytest

from offline_video import app as app_module
from offline_video import downloader
from offline_video.app import create_app
from offline_video.downloader import AUDIO_OUTPUTS, VIDEO_OUTPUTS, DownloadManager

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


@pytest.fixture(scope="module")
def server(tmp_path_factory, monkeypatch_module):
    root = tmp_path_factory.mktemp("srv")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90",
                    "-frames:v", "1", str(root / "thumb.webp")], check=True)
    for i in (1, 2):
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
             "-f", "lavfi", "-i", "sine=duration=1", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(root / f"clip{i}.mp4")],
            check=True,
        )
    handler = functools.partial(QuietHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    base = f"http://127.0.0.1:{httpd.server_port}"
    (root / "feed.xml").write_text(
        f"""<?xml version="1.0"?><rss version="2.0"><channel><title>My Playlist</title><link>{base}/</link>
<item><title>One</title><guid>v1</guid><enclosure url="{base}/clip1.mp4" type="video/mp4"/></item>
<item><title>Two</title><guid>v2</guid><enclosure url="{base}/clip2.mp4" type="video/mp4"/></item>
<item><title>Gone</title><guid>v3</guid><enclosure url="{base}/missing.mp4" type="video/mp4"/></item>
</channel></rss>"""
    )
    # A page with a WebP thumbnail, like YouTube, so thumbnail conversion (which needs FFmpeg) runs.
    (root / "page.html").write_text(
        f"""<html><head><meta property="og:title" content="Page Video">
<meta property="og:image" content="{base}/thumb.webp">
<meta property="og:video" content="{base}/clip1.mp4"><meta property="og:video:type" content="video/mp4">
</head><body></body></html>"""
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield base
    httpd.shutdown()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    mp.setenv("NO_PROXY", "127.0.0.1,localhost")
    mp.setenv("no_proxy", "127.0.0.1,localhost")
    yield mp
    mp.undo()


def wait(job, timeout=60):
    end = time.time() + timeout
    while job.status not in ("done", "error", "cancelled"):
        assert time.time() < end, f"job stuck in {job.status}"
        time.sleep(0.1)
    return job


def test_single_video(server, tmp_path):
    m = DownloadManager(tmp_path)
    job = wait(m.submit(f"{server}/clip1.mp4", "360"))
    assert job.status == "done", job.error
    assert job.completed_items == 1 and not job.is_playlist
    [item] = m.library()
    assert item["media_url"] == "clip1 [clip1].mp4"
    assert item["playlist"] is None

    # Downloading again is skipped thanks to the archive.
    again = wait(m.submit(f"{server}/clip1.mp4", "360"))
    assert again.status == "done" and again.completed_items == 0
    assert "already in your library" in again.error


def test_playlist_goes_into_folder_and_survives_failed_item(server, tmp_path):
    m = DownloadManager(tmp_path)
    job = wait(m.submit(f"{server}/feed.xml", "360"))
    assert job.status == "done"
    assert job.is_playlist and job.title == "My Playlist"
    assert (job.total_items, job.completed_items, job.failed_items) == (3, 2, 1)
    assert "1 failed" in job.error
    items = m.library()
    assert [i["media_url"] for i in items] == ["My Playlist/001 - One [v1].mp4", "My Playlist/002 - Two [v2].mp4"]
    assert all(i["playlist"] == "My Playlist" for i in items)


def test_bad_url_reports_error(server, tmp_path):
    m = DownloadManager(tmp_path)
    job = wait(m.submit(f"{server}/nope.mp4"))
    assert job.status == "error" and "404" in job.error
    with pytest.raises(ValueError):
        m.submit("not a url")
    with pytest.raises(ValueError):
        m.submit(f"{server}/clip1.mp4", quality="4k")


def test_api_flow(server, tmp_path):
    app = create_app(str(tmp_path), settings_file=tmp_path / "settings.json")
    c = app.test_client()
    assert c.get("/").status_code == 200
    assert c.post("/api/downloads", json={"url": ""}).status_code == 400

    r = c.post("/api/downloads", json={"url": f"{server}/clip2.mp4", "quality": "480"})
    assert r.status_code == 201
    job = app.config["manager"].jobs[r.json["jobs"][0]["id"]]
    wait(job)
    assert c.get("/api/jobs").json["jobs"][0]["status"] == "done"

    [item] = c.get("/api/library").json["items"]
    media = c.get("/media/" + item["media_url"], headers={"Range": "bytes=0-99"})
    assert media.status_code == 206 and len(media.data) == 100
    media.close()

    assert c.get("/media/../../etc/passwd").status_code == 404
    assert c.delete("/api/library/" + item["id"]).status_code == 200
    assert c.get("/api/library").json["items"] == []
    assert (tmp_path / ".archive-mp4.txt").read_text().strip() == ""

    # Once deleted it can be downloaded again.
    r = c.post("/api/downloads", json={"url": f"{server}/clip2.mp4"})
    wait(app.config["manager"].jobs[r.json["jobs"][0]["id"]])
    assert len(c.get("/api/library").json["items"]) == 1


def test_every_output_format_and_formats_coexist(server, tmp_path):
    m = DownloadManager(tmp_path)
    expected_ext = {"mpeg": "mpg"}
    for out in VIDEO_OUTPUTS + AUDIO_OUTPUTS:
        job = wait(m.submit(f"{server}/page.html", "360", output=out), timeout=120)
        assert job.status == "done", (out, job.error)
        assert job.files == [f"Page Video [clip1].{expected_ext.get(out, out)}"], out
    items = m.library()
    assert sorted(i["format"] for i in items) == sorted(expected_ext.get(o, o) for o in VIDEO_OUTPUTS + AUDIO_OUTPUTS)
    assert all(i["thumbnail_url"] == "Page Video [clip1].jpg" for i in items)
    assert all((tmp_path / i["media_url"]).stat().st_size > 0 for i in items)

    # Deleting one format keeps the others and their shared thumbnail/metadata.
    assert m.delete("Page Video [clip1].mp4")
    assert len(m.library()) == len(items) - 1
    assert (tmp_path / "Page Video [clip1].info.json").exists()
    # ...and only that format can be downloaded again.
    assert wait(m.submit(f"{server}/page.html", "360", output="mp4")).files == ["Page Video [clip1].mp4"]
    assert wait(m.submit(f"{server}/page.html", "360", output="mp3")).completed_items == 0


def test_without_ffmpeg_saves_single_file_and_says_why(server, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # hide ffmpeg from yt-dlp too
    assert not downloader.ffmpeg_available()
    m = DownloadManager(tmp_path / "lib")
    job = wait(m.submit(f"{server}/page.html", "720", output="mkv"))
    assert job.status == "done" and job.failed_items == 0
    assert job.files == ["Page Video [clip1].mp4"]
    assert "install FFmpeg" in job.error
    [item] = m.library()
    assert item["thumbnail_url"] == "Page Video [clip1].webp"


def test_failed_item_is_not_reported_done(tmp_path):
    m = DownloadManager(tmp_path)
    job = wait(m.submit("http://127.0.0.1:9/nothing-listens-here.mp4"))
    assert job.status == "error" and job.completed_items == 0
    assert "\x1b" not in job.error


def test_logger_cleans_errors():
    log = downloader._Logger()
    log.error("\x1b[0;31mERROR:\x1b[0m ffmpeg not found. Please install or provide the path using --ffmpeg-location")
    assert log.errors == [downloader.FFMPEG_HINT]


def test_open_and_change_folder(server, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(app_module, "open_in_os", lambda path, reveal=False: opened.append((path, reveal)))
    settings = tmp_path / "settings.json"
    app = create_app(str(tmp_path / "lib"), settings_file=settings)
    c = app.test_client()
    st = c.get("/api/status").json
    assert st["library_dir"] == str((tmp_path / "lib").resolve()) and st["ffmpeg"] is True

    r = c.post("/api/downloads", json={"url": f"{server}/clip1.mp4"})
    job = wait(app.config["manager"].jobs[r.json["jobs"][0]["id"]])
    f = job.files[0]
    assert c.post("/api/open", json={"path": f}).status_code == 200
    assert c.post("/api/open", json={"path": f, "reveal": True}).status_code == 200
    assert c.post("/api/open", json={"path": ""}).status_code == 200  # the library folder itself
    lib = (tmp_path / "lib").resolve()
    assert opened == [(lib / f, False), (lib / f, True), (lib, False)]
    assert c.post("/api/open", json={"path": "../settings.json"}).status_code == 404
    # Only a browser on the same computer may open files.
    assert c.post("/api/open", json={"path": f}, environ_base={"REMOTE_ADDR": "192.168.1.5"}).status_code == 403

    new_dir = tmp_path / "Movies" / "YouTube"
    r = c.post("/api/settings", json={"library_dir": str(new_dir)})
    assert r.status_code == 200 and new_dir.is_dir()
    assert c.get("/api/status").json["library_dir"] == str(new_dir.resolve())
    # The choice is remembered for next time.
    app2 = create_app(settings_file=settings)
    assert app2.config["manager"].library_dir == new_dir.resolve()
