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

from offline_video.app import create_app
from offline_video.downloader import DownloadManager

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


@pytest.fixture(scope="module")
def server(tmp_path_factory, monkeypatch_module):
    root = tmp_path_factory.mktemp("srv")
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
    app = create_app(str(tmp_path))
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
    assert (tmp_path / ".archive.txt").read_text().strip() == ""

    # Once deleted it can be downloaded again.
    r = c.post("/api/downloads", json={"url": f"{server}/clip2.mp4"})
    wait(app.config["manager"].jobs[r.json["jobs"][0]["id"]])
    assert len(c.get("/api/library").json["items"]) == 1
