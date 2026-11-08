"""Flask web app: queue YouTube videos/playlists and watch them offline."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file

from . import __version__
from .downloader import (
    AUDIO_OUTPUTS,
    QUALITIES,
    VIDEO_OUTPUTS,
    DownloadManager,
    extract_urls,
    ffmpeg_available,
    open_in_os,
)
from .phone import KEY_COOKIE, PhoneServer, new_key, print_qr, qr_svg

SETTINGS_FILE = Path.home() / ".offline_video.json"


def default_library_dir() -> Path:
    """./downloads when run from a source checkout (run.sh / run.bat), otherwise a folder in the
    user's Movies (Mac) or Videos folder."""
    checkout = Path(__file__).resolve().parent.parent
    if not getattr(sys, "frozen", False) and (checkout / "run.sh").exists():
        return checkout / "downloads"
    videos = Path.home() / ("Movies" if sys.platform == "darwin" else "Videos")
    return videos / "Offline Video"


def load_settings(settings_file: Path) -> dict:
    try:
        return json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings_file: Path, settings: dict) -> None:
    try:
        settings_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass


def create_app(library_dir: str | None = None, workers: int = 1, settings_file: Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # uploaded link lists
    settings_file = settings_file or SETTINGS_FILE
    settings = load_settings(settings_file)
    library_dir = library_dir or os.environ.get("OFFLINE_VIDEO_DIR") or settings.get("library_dir") or default_library_dir()
    manager = DownloadManager(library_dir, workers=workers)
    app.config["manager"] = manager
    app.config.setdefault("PORT", 8000)
    if not settings.get("access_key"):
        settings["access_key"] = new_key()
        save_settings(settings_file, settings)
    phone = PhoneServer(app, app.config["PORT"])
    app.config["phone"] = phone
    app.config["settings"] = settings

    def is_local() -> bool:
        try:
            return ipaddress.ip_address(request.remote_addr or "").is_loopback
        except ValueError:
            return False

    def local_only():
        # Opening files, changing folders and phone settings act on the computer running the app, so
        # only allow them from a browser on that same computer.
        if not is_local():
            abort(403)

    def phone_url(base: str) -> str:
        return f"{base}/?key={settings['access_key']}"

    @app.before_request
    def require_key_for_other_devices():
        """Phones (anything not on this computer) need the secret key from the QR code. It arrives as
        ?key=… once and is then remembered in a cookie."""
        if is_local() or request.path in ("/manifest.webmanifest",) or request.path.startswith("/static/icons/"):
            return None
        key = settings["access_key"]
        if request.args.get("key") == key:
            if request.path == "/" or request.method != "GET":
                return None  # the home page keeps ?key= so "Add to Home Screen" bookmarks it
            resp = redirect(request.path)
        elif request.cookies.get(KEY_COOKIE) == key:
            return None
        else:
            return render_template("locked.html"), 401
        resp.set_cookie(KEY_COOKIE, key, max_age=400 * 24 * 3600, httponly=True, samesite="Lax")
        return resp

    @app.after_request
    def remember_key(resp):
        if not is_local() and request.args.get("key") == settings["access_key"] and resp.status_code < 400:
            resp.set_cookie(KEY_COOKIE, settings["access_key"], max_age=400 * 24 * 3600, httponly=True, samesite="Lax")
        return resp

    @app.get("/")
    def index():
        return render_template(
            "index.html", qualities=QUALITIES, video_outputs=VIDEO_OUTPUTS, audio_outputs=AUDIO_OUTPUTS,
            remote=not is_local(),
        )

    @app.get("/manifest.webmanifest")
    def manifest():
        # Lets phones "Add to Home Screen" as an app. The start page carries the key when the request
        # comes from an already-connected phone, because home-screen apps keep their own cookies.
        start = "/"
        if not is_local() and request.cookies.get(KEY_COOKIE) == settings["access_key"]:
            start = f"/?key={settings['access_key']}"
        data = {
            "name": "Offline Video",
            "short_name": "Offline Video",
            "description": "Download YouTube videos and playlists and watch them offline.",
            "start_url": start,
            "scope": "/",
            "display": "standalone",
            "background_color": "#111114",
            "theme_color": "#e5322d",
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        }
        return app.response_class(json.dumps(data), mimetype="application/manifest+json")

    @app.get("/api/status")
    def status():
        return jsonify(
            library_dir=str(manager.library_dir),
            ffmpeg=ffmpeg_available(),
            version=__version__,
            local=is_local(),
        )

    def phone_state(error: str | None = None):
        url = phone_url(f"http://{phone.ip}:{phone._server.server_port}") if phone.running else None
        return jsonify(enabled=phone.running, url=url, qr=qr_svg(url) if url else None, error=error)

    @app.get("/api/phone")
    def get_phone():
        local_only()
        return phone_state()

    @app.post("/api/phone")
    def set_phone():
        local_only()
        enable = bool((request.get_json(silent=True) or {}).get("enabled"))
        error = None
        if enable:
            try:
                phone.start()
            except OSError as e:
                error = f"Couldn't turn on phone access: {e}"
        else:
            phone.stop()
        settings["phone_access"] = phone.running
        save_settings(settings_file, settings)
        return phone_state(error)

    @app.post("/api/phone/new-key")
    def new_phone_key():
        """Make a new secret link; phones using the old one are disconnected."""
        local_only()
        settings["access_key"] = new_key()
        save_settings(settings_file, settings)
        return phone_state()

    @app.post("/api/settings")
    def update_settings():
        local_only()
        data = request.get_json(silent=True) or {}
        new_dir = (data.get("library_dir") or "").strip()
        if not new_dir:
            return jsonify(error="Enter a folder path"), 400
        try:
            path = manager.set_library_dir(new_dir)
        except OSError as e:
            return jsonify(error=f"Can't use that folder: {e.strerror or e}"), 400
        settings["library_dir"] = str(path)
        save_settings(settings_file, settings)
        return jsonify(library_dir=str(path))

    @app.post("/api/open")
    def open_path():
        local_only()
        data = request.get_json(silent=True) or {}
        path = manager.resolve(data.get("path") or "")
        if path is None or not path.exists():
            abort(404)
        try:
            open_in_os(path, reveal=bool(data.get("reveal")))
        except OSError as e:
            return jsonify(error=f"Couldn't open it: {e}"), 500
        return jsonify(ok=True)

    def queue_urls(text, data):
        urls = extract_urls(text)
        if not urls:
            return jsonify(error="No links found. Paste YouTube video or playlist links (http:// or https://)."), 400
        quality = data.get("quality") or "720"
        output = data.get("output") or "mp4"
        subtitles = str(data.get("subtitles", "")).lower() in ("1", "true", "on", "yes")
        try:
            jobs = [manager.submit(u, quality, subtitles, output) for u in urls]
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(jobs=[j.to_dict() for j in jobs], queued=len(jobs)), 201

    @app.post("/api/downloads")
    def add_download():
        # Accepts one or many links: one per line, comma/space separated, or mixed with other text.
        data = request.get_json(silent=True) or request.form
        return queue_urls(data.get("url") or "", data)

    @app.post("/api/downloads/file")
    def add_download_file():
        """Queue every link in an uploaded text file (.txt, .csv, …)."""
        f = request.files.get("file")
        if f is None:
            return jsonify(error="Choose a text file with links"), 400
        text = f.read().decode("utf-8-sig", errors="replace")
        return queue_urls(text, request.form)

    @app.get("/api/jobs")
    def jobs():
        return jsonify(jobs=manager.list_jobs())

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel(job_id):
        if not manager.cancel(job_id):
            abort(404)
        return jsonify(ok=True)

    @app.post("/api/jobs/cancel-all")
    def cancel_all():
        return jsonify(cancelled=manager.cancel_all())

    @app.post("/api/jobs/clear")
    def clear():
        manager.clear_finished()
        return jsonify(ok=True)

    @app.get("/api/library")
    def library():
        return jsonify(items=manager.library())

    @app.delete("/api/library/<path:item_id>")
    def delete(item_id):
        if not manager.delete(item_id):
            abort(404)
        return jsonify(ok=True)

    @app.get("/media/<path:rel>")
    def media(rel):
        path = manager.resolve(rel)
        if path is None or not path.is_file():
            abort(404)
        # conditional=True enables HTTP range requests so the player can seek. ?download=1 saves the file
        # (e.g. to a phone) under a readable name instead of playing it.
        if request.args.get("download"):
            return send_file(path, conditional=True, as_attachment=True, download_name=path.name)
        return send_file(path, conditional=True)

    return app


def _already_running(host: str, port: int) -> bool:
    """True if an Offline Video app is already answering on this port."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=2) as r:
            return "library_dir" in json.loads(r.read())
    except Exception:  # noqa: BLE001 - anything else on the port, or nothing at all
        return False


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def main():
    parser = argparse.ArgumentParser(prog="offline-video", description="Offline Video web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dir", default=None, help="library folder (default: last used folder)")
    parser.add_argument("--workers", type=int, default=1, help="parallel downloads")
    parser.add_argument("--no-browser", action="store_true", help="don't open the web browser on start")
    parser.add_argument("--phone", action="store_true", help="let phones on the same Wi-Fi use the app")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    browse_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    if _already_running(browse_host, args.port):
        print(f"Offline Video is already running: http://{browse_host}:{args.port}")
        if not args.no_browser:
            webbrowser.open(f"http://{browse_host}:{args.port}")
        return
    port = args.port
    while not _port_free(args.host, port) and port < args.port + 20:
        port += 1

    app = create_app(args.dir, args.workers)
    app.config["PORT"] = app.config["phone"].port = port
    manager = app.config["manager"]
    url = f"http://{browse_host}:{port}"
    print(f"Offline Video {__version__} is running at {url}")
    print(f"Saving downloads to: {manager.library_dir}")
    if not ffmpeg_available():
        print("WARNING: FFmpeg not found. Downloads will be limited to single-file formats (often lower quality)")
        print("         and format conversion is off. Install it: brew install ffmpeg  (Mac)")
    settings = app.config["settings"]
    if args.phone or settings.get("phone_access"):
        try:
            phone_base = app.config["phone"].start()
            phone_link = f"{phone_base}/?key={settings['access_key']}"
            print(f"\nPhones on the same Wi-Fi: scan this code or open {phone_link}")
            print_qr(phone_link)
        except OSError as e:
            print(f"Phone access is on but couldn't start: {e}")
    print("Keep this window open while you use the app. Close it (or press Ctrl+C) to quit.")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.run(host=args.host, port=port, threaded=True)


if __name__ == "__main__":
    main()
