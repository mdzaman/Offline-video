"""Flask web app: queue YouTube videos/playlists and watch them offline."""

from __future__ import annotations

import argparse
import os

from flask import Flask, abort, jsonify, render_template, request, send_file

from .downloader import QUALITY_FORMATS, DownloadManager


def create_app(library_dir: str | None = None, workers: int = 1) -> Flask:
    app = Flask(__name__)
    library_dir = library_dir or os.environ.get("OFFLINE_VIDEO_DIR", "downloads")
    manager = DownloadManager(library_dir, workers=workers)
    app.config["manager"] = manager

    @app.get("/")
    def index():
        return render_template("index.html", qualities=list(QUALITY_FORMATS))

    @app.post("/api/downloads")
    def add_download():
        data = request.get_json(silent=True) or request.form
        urls = [u for u in (data.get("url") or "").split() if u]
        if not urls:
            return jsonify(error="Enter a video or playlist URL"), 400
        quality = data.get("quality") or "720"
        subtitles = str(data.get("subtitles", "")).lower() in ("1", "true", "on", "yes")
        try:
            jobs = [manager.submit(u, quality, subtitles) for u in urls]
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(jobs=[j.to_dict() for j in jobs]), 201

    @app.get("/api/jobs")
    def jobs():
        return jsonify(jobs=manager.list_jobs())

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel(job_id):
        if not manager.cancel(job_id):
            abort(404)
        return jsonify(ok=True)

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
        # conditional=True enables HTTP range requests so the player can seek.
        return send_file(path, conditional=True)

    return app


def main():
    parser = argparse.ArgumentParser(description="Offline Video web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dir", default=None, help="library folder (default: ./downloads)")
    parser.add_argument("--workers", type=int, default=1, help="parallel downloads")
    args = parser.parse_args()
    app = create_app(args.dir, args.workers)
    print(f"Offline Video running at http://{args.host}:{args.port}  (library: {app.config['manager'].library_dir})")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
