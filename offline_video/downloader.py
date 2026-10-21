"""Download queue and media library built on top of yt-dlp."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yt_dlp

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".m4a", ".mp3", ".opus", ".ogg"}
THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# Formats are chosen so the result plays in a browser <video> element (mp4/h264 + m4a/aac)
# when possible, falling back to whatever is available.
QUALITY_FORMATS = {
    "best": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
    "1080": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b[height<=1080]/b",
    "720": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/bv*[height<=720]+ba/b[height<=720]/b",
    "480": "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/bv*[height<=480]+ba/b[height<=480]/b",
    "360": "bv*[height<=360][ext=mp4]+ba[ext=m4a]/b[height<=360][ext=mp4]/bv*[height<=360]+ba/b[height<=360]/b",
    "audio": "ba[ext=m4a]/ba/b",
}


class Cancelled(Exception):
    pass


@dataclass
class Job:
    url: str
    quality: str = "720"
    subtitles: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "queued"  # queued | downloading | processing | done | error | cancelled
    title: str | None = None
    is_playlist: bool = False
    total_items: int = 1
    completed_items: int = 0
    failed_items: int = 0
    current_item: str | None = None
    current_index: int | None = None
    percent: float = 0.0
    speed: float | None = None
    eta: int | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_requested: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("cancel_requested")
        return d


SINGLE_TEMPLATE = "%(title).150B [%(id)s].%(ext)s"
PLAYLIST_TEMPLATE = "%(playlist_title,playlist|Playlist).100B/%(playlist_index)03d - %(title).150B [%(id)s].%(ext)s"


def build_ydl_opts(job: Job, out_dir: Path, hook, pp_hook, logger=None) -> dict:
    if job.quality not in QUALITY_FORMATS:
        raise ValueError(f"Unknown quality: {job.quality}")
    opts = {
        "format": QUALITY_FORMATS[job.quality],
        # Single videos go straight into the library root; playlist items get their own folder.
        "outtmpl": {"default": PLAYLIST_TEMPLATE if job.is_playlist else SINGLE_TEMPLATE},
        "paths": {"home": str(out_dir), "temp": str(out_dir / ".tmp")},
        "windowsfilenames": True,
        "writeinfojson": True,
        "allow_playlist_files": False,
        "writethumbnail": True,
        "ignoreerrors": True,  # keep going if one playlist entry is unavailable
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "download_archive": str(out_dir / ".archive.txt"),
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
        ],
    }
    if logger is not None:
        opts["logger"] = logger
    if job.quality == "audio":
        opts["postprocessors"].append(
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}
        )
    else:
        opts["merge_output_format"] = "mp4"
    if job.subtitles:
        opts.update(
            {
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["en.*", "en"],
                "subtitlesformat": "vtt/best",
            }
        )
        opts["postprocessors"].append({"key": "FFmpegSubtitlesConvertor", "format": "vtt"})
    return opts


class _Logger:
    """Collects yt-dlp errors (and archive skips) so a job can report what happened."""

    def __init__(self):
        self.errors: list[str] = []
        self.skipped = 0

    def debug(self, msg):
        if "has already been recorded in the archive" in str(msg):
            self.skipped += 1

    info = debug

    def warning(self, msg):
        pass

    def error(self, msg):
        self.errors.append(str(msg).removeprefix("ERROR: "))


class DownloadManager:
    def __init__(self, library_dir: str | os.PathLike, workers: int = 1):
        self.library_dir = Path(library_dir).resolve()
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue()
        for i in range(max(1, workers)):
            threading.Thread(target=self._worker, name=f"dl-worker-{i}", daemon=True).start()

    # ---- jobs -------------------------------------------------------------

    def submit(self, url: str, quality: str = "720", subtitles: bool = False) -> Job:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("Please enter a full http(s) URL")
        if quality not in QUALITY_FORMATS:
            raise ValueError(f"Unknown quality: {quality}")
        job = Job(url=url, quality=quality, subtitles=subtitles)
        with self._lock:
            self.jobs[job.id] = job
        self._queue.put(job.id)
        return job

    def list_jobs(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [j.to_dict() for j in jobs]

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job or job.status in ("done", "error", "cancelled"):
                return False
            job.cancel_requested = True
            if job.status == "queued":
                job.status = "cancelled"
                job.finished_at = time.time()
            return True

    def clear_finished(self) -> None:
        with self._lock:
            for jid in [j.id for j in self.jobs.values() if j.status in ("done", "error", "cancelled")]:
                del self.jobs[jid]

    def _worker(self):
        while True:
            job_id = self._queue.get()
            job = self.jobs.get(job_id)
            if job and not job.cancel_requested:
                self._run(job)
            self._queue.task_done()

    def _run(self, job: Job):
        job.status = "downloading"
        seen_done: set[str] = set()

        def check_cancel():
            if job.cancel_requested:
                raise Cancelled()

        def hook(d):
            check_cancel()
            info = d.get("info_dict") or {}
            job.current_item = info.get("title") or job.current_item
            if info.get("playlist_index"):
                job.current_index = info["playlist_index"]
                job.is_playlist = True
                job.total_items = info.get("n_entries") or job.total_items
                job.title = info.get("playlist_title") or info.get("playlist") or job.title
            elif not job.is_playlist:
                job.title = info.get("title") or job.title
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    job.percent = round(d.get("downloaded_bytes", 0) * 100 / total, 1)
                job.speed = d.get("speed")
                job.eta = d.get("eta")
            elif d["status"] == "finished":
                job.percent = 100.0

        def pp_hook(d):
            check_cancel()
            info = d.get("info_dict") or {}
            if d["status"] == "started" and d.get("postprocessor") in ("Merger", "ExtractAudio"):
                job.status = "processing"
            if d["status"] == "finished" and d.get("postprocessor") == "MoveFiles":
                key = info.get("id")
                if key and key not in seen_done:
                    seen_done.add(key)
                    job.completed_items += 1
                job.status = "downloading"
                job.percent = 0.0

        logger = _Logger()
        try:
            # Peek at the URL first (cheap, no format resolution) to learn whether it is a playlist,
            # which decides the output folder layout.
            probe_logger = _Logger()
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": probe_logger}) as ydl:
                try:
                    info = ydl.extract_info(job.url, download=False, process=False)
                except yt_dlp.utils.DownloadError:
                    info = None
            if info is None:
                raise RuntimeError(probe_logger.errors[-1] if probe_logger.errors else "Could not read URL")
            job.title = info.get("title") or job.title
            if info.get("_type") in ("playlist", "multi_video"):
                job.is_playlist = True
                entries = info.get("entries")
                if isinstance(entries, list):
                    job.total_items = len(entries)
                elif info.get("playlist_count"):
                    job.total_items = info["playlist_count"]
            check_cancel()
            with yt_dlp.YoutubeDL(build_ydl_opts(job, self.library_dir, hook, pp_hook, logger)) as ydl:
                ydl.download([job.url])
            job.failed_items = len(logger.errors)
            notes = []
            if logger.skipped:
                notes.append(f"{logger.skipped} already in your library")
            if logger.errors:
                notes.append(f"{len(logger.errors)} failed (last error: {logger.errors[-1]})")
            job.error = "; ".join(notes) or None
            job.status = "error" if job.completed_items == 0 and not logger.skipped else "done"
        except Cancelled:
            job.status = "cancelled"
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI
            job.status = "error"
            job.error = str(e).removeprefix("ERROR: ")
        finally:
            job.finished_at = time.time()
            job.speed = job.eta = None

    # ---- library ----------------------------------------------------------

    def library(self) -> list[dict]:
        items = []
        for info_path in self.library_dir.rglob("*.info.json"):
            if ".tmp" in info_path.parts:
                continue
            item = self._library_item(info_path)
            if item:
                items.append(item)
        items.sort(key=lambda i: (i["playlist"] or "", i["playlist_index"] or 0, -i["downloaded_at"]))
        return items

    def _library_item(self, info_path: Path) -> dict | None:
        stem = info_path.name[: -len(".info.json")]
        folder = info_path.parent
        media = None
        thumb = None
        subs = []
        for p in folder.iterdir():
            if not p.name.startswith(stem + "."):
                continue
            rest = p.name[len(stem):]
            if p.suffix in VIDEO_EXTS and rest.count(".") == 1:
                media = p
            elif p.suffix in THUMB_EXTS and rest.count(".") == 1:
                thumb = p
            elif p.suffix == ".vtt":
                subs.append({"lang": rest[1:-4], "url": self._rel(p)})
        if media is None:
            return None
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        return {
            "id": self._rel(info_path)[: -len(".info.json")],
            "video_id": info.get("id"),
            "title": info.get("title") or stem,
            "channel": info.get("channel") or info.get("uploader"),
            "duration": info.get("duration"),
            "description": info.get("description") or "",
            "upload_date": info.get("upload_date"),
            "webpage_url": info.get("webpage_url"),
            "playlist": (info.get("playlist_title") or info.get("playlist")) if info.get("playlist_index") else None,
            "playlist_index": info.get("playlist_index"),
            "media_url": self._rel(media),
            "is_audio": media.suffix in (".m4a", ".mp3", ".opus", ".ogg"),
            "thumbnail_url": self._rel(thumb) if thumb else None,
            "subtitles": subs,
            "size": media.stat().st_size,
            "downloaded_at": media.stat().st_mtime,
        }

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.library_dir).as_posix()

    def resolve(self, rel: str) -> Path | None:
        """Turn a library-relative path into an absolute path, refusing anything outside the library."""
        p = (self.library_dir / rel).resolve()
        if p != self.library_dir and self.library_dir not in p.parents:
            return None
        return p

    def delete(self, item_id: str) -> bool:
        base = self.resolve(item_id + ".info.json")
        if base is None or not base.exists():
            return False
        stem = base.name[: -len(".info.json")]
        try:
            info = json.loads(base.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        video_id = info.get("id")
        extractor = (info.get("extractor_key") or "").lower()
        for p in base.parent.iterdir():
            if p.name.startswith(stem + "."):
                p.unlink(missing_ok=True)
        if base.parent != self.library_dir and not any(base.parent.iterdir()):
            base.parent.rmdir()
        # Forget it in the archive so it can be downloaded again later.
        archive = self.library_dir / ".archive.txt"
        if video_id and archive.exists():
            lines = archive.read_text(encoding="utf-8").splitlines()
            keep = [ln for ln in lines if ln.strip() != f"{extractor} {video_id}"]
            archive.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
        return True
