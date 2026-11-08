"""Download queue and media library built on top of yt-dlp."""

from __future__ import annotations

import functools
import itertools
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

VIDEO_OUTPUTS = ["mp4", "mkv", "webm", "mov", "avi", "mpeg"]
AUDIO_OUTPUTS = ["mp3", "m4a", "wav", "flac", "opus", "ogg"]
QUALITIES = ["best", "1080", "720", "480", "360"]

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".opus", ".ogg"}
MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".mpg", ".mpeg", ".flv"} | AUDIO_EXTS
THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
URL_RE = re.compile(r"https?://[^\s,;\"'<>()\[\]]+")
ERROR_ID_RE = re.compile(r"^\[[^\]]+\] ([^\s:]+): ")  # "[youtube] dQw4w9WgXcQ: Video unavailable"
FILE_ID_RE = re.compile(r"\[([^\]]+)\]\.[^.]+$")  # "... [dQw4w9WgXcQ].mp4"
MAX_PLAYLIST_LISTING = 5000


def extract_urls(text: str) -> list[str]:
    """All http(s) links in a block of text (one per line, comma separated, or mixed with other text),
    without duplicates, in order."""
    seen, urls = set(), []
    for u in URL_RE.findall(text or ""):
        u = u.rstrip(".")
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls

FFMPEG_HINT = (
    "FFmpeg is not installed. Install it (Mac: brew install ffmpeg, Windows: winget install ffmpeg, "
    "Linux: sudo apt install ffmpeg) and try again."
)


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _height(quality: str) -> str:
    return "" if quality == "best" else f"[height<={quality}]"


def format_selector(quality: str, output: str, has_ffmpeg: bool) -> str:
    h = _height(quality)
    if output in AUDIO_OUTPUTS:
        return "ba[ext=m4a]/ba/b" if has_ffmpeg or output != "m4a" else "ba[ext=m4a]/b[ext=mp4]/b"
    if not has_ffmpeg:
        # Without FFmpeg, separate video and audio streams can't be merged, so only formats that
        # already contain both are usable (on YouTube that usually means up to 360p/720p MP4).
        return f"b{h}[ext=mp4]/b{h}/b[ext=mp4]/b"
    if output == "webm":
        return f"bv*{h}[ext=webm]+ba[ext=webm]/b{h}[ext=webm]/bv*{h}+ba/b{h}/b"
    if output == "mkv":
        return f"bv*{h}+ba/b{h}/b"
    # mp4 / mov / avi / mpeg: start from H.264 + AAC, which plays everywhere and remuxes cheaply.
    return f"bv*{h}[ext=mp4]+ba[ext=m4a]/b{h}[ext=mp4]/bv*{h}+ba/b{h}/b"


class Cancelled(Exception):
    pass


@functools.lru_cache(maxsize=1)
def ffmpeg_encoders() -> frozenset[str]:
    """Names of the encoders the installed FFmpeg supports (builds differ, e.g. Homebrew's lacks libxvid)."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(line.split()[1] for line in out.splitlines() if line[:1] == " " and len(line.split()) > 1)


def reencode_args(output: str) -> list[str]:
    """FFmpeg arguments for formats that need re-encoding, using encoders every FFmpeg build has."""
    if output == "mpeg":
        return ["-c:v", "mpeg2video", "-q:v", "3", "-c:a", "mp2", "-b:a", "192k"]
    if output == "avi":
        audio = "libmp3lame" if "libmp3lame" in ffmpeg_encoders() else "ac3"
        return ["-c:v", "mpeg4", "-q:v", "3", "-vtag", "XVID", "-c:a", audio, "-b:a", "192k"]
    raise ValueError(output)


class ReencodePP(FFmpegPostProcessor):
    """Re-encodes the finished video to AVI or MPEG-2 (.mpg). yt-dlp can't make MPEG at all, and its AVI
    conversion needs libxvid, which many FFmpeg builds don't include."""

    EXTS = {"avi": "avi", "mpeg": "mpg"}

    def __init__(self, downloader, output: str):
        super().__init__(downloader)
        self.output = output

    def run(self, info):
        path = info["filepath"]
        ext = self.EXTS[self.output]
        out = str(Path(path).with_suffix("." + ext))
        self.to_screen(f'Converting video to {self.output.upper()}: "{out}"')
        self.run_ffmpeg(path, out, reencode_args(self.output))
        info["filepath"] = out
        info["ext"] = ext
        return [path], info


class RenameExtPP(PostProcessor):
    """Gives the finished file a different extension (e.g. an Ogg Opus file saved as .ogg)."""

    def __init__(self, downloader, ext: str):
        super().__init__(downloader)
        self.ext = ext

    def run(self, info):
        path = Path(info["filepath"])
        out = path.with_suffix("." + self.ext)
        os.replace(path, out)
        info["filepath"] = str(out)
        info["ext"] = self.ext
        return [], info


@dataclass
class Job:
    url: str
    quality: str = "720"
    output: str = "mp4"
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
    files: list[str] = field(default_factory=list)  # library-relative paths of finished files
    # For playlists: one entry per video, {"index", "id", "title", "status", "error"} where status is
    # pending | downloading | done | skipped (already in library) | failed | cancelled.
    items: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_requested: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("cancel_requested")
        return d


SINGLE_TEMPLATE = "%(title).150B [%(id)s].%(ext)s"
PLAYLIST_TEMPLATE = "%(playlist_title,playlist|Playlist).100B/%(playlist_index)03d - %(title).150B [%(id)s].%(ext)s"


def build_ydl_opts(job: Job, out_dir: Path, hook, pp_hook, post_hook, logger=None, has_ffmpeg=None,
                   work_dir: Path | None = None) -> dict:
    """yt-dlp options. Files are written to work_dir (a per-job staging folder) and the archive lives
    in out_dir (the library)."""
    if job.quality not in QUALITIES:
        raise ValueError(f"Unknown quality: {job.quality}")
    if job.output not in VIDEO_OUTPUTS + AUDIO_OUTPUTS:
        raise ValueError(f"Unknown output format: {job.output}")
    if has_ffmpeg is None:
        has_ffmpeg = ffmpeg_available()
    opts = {
        "format": format_selector(job.quality, job.output, has_ffmpeg),
        # Single videos go straight into the library root; playlist items get their own folder.
        "outtmpl": {"default": PLAYLIST_TEMPLATE if job.is_playlist else SINGLE_TEMPLATE},
        # Staging in a per-job folder stops yt-dlp from reusing (and then deleting) a library file of
        # the same video in another format as its download.
        "paths": {"home": str(work_dir or out_dir), "temp": str((work_dir or out_dir) / "part")},
        "windowsfilenames": True,
        "writeinfojson": True,
        "allow_playlist_files": False,
        "writethumbnail": True,
        "ignoreerrors": True,  # keep going if one playlist entry is unavailable
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "color": {"stdout": "no_color", "stderr": "no_color"},
        # One archive per output format, so the same video can be kept as e.g. both MP4 and MP3.
        "download_archive": str(out_dir / f".archive-{job.output}.txt"),
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
        "post_hooks": [post_hook],
        "postprocessors": [],
    }
    if logger is not None:
        opts["logger"] = logger
    if job.subtitles:
        opts.update(
            {
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["en.*", "en"],
                "subtitlesformat": "vtt/best",
            }
        )
    if not has_ffmpeg:
        return opts

    pps = opts["postprocessors"]
    pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
    if job.subtitles:
        pps.append({"key": "FFmpegSubtitlesConvertor", "format": "vtt"})
    if job.output in AUDIO_OUTPUTS:
        codec = job.output
        if job.output == "ogg":
            # Vorbis if this FFmpeg has it (Homebrew's doesn't); otherwise Opus, which is also an .ogg file.
            codec = "vorbis" if "libvorbis" in ffmpeg_encoders() else "opus"
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": "192"})
    elif job.output in ("mp4", "mkv"):
        # Merging covers separate video+audio streams; the remux covers single-file downloads.
        opts["merge_output_format"] = job.output
        pps.append({"key": "FFmpegVideoRemuxer", "preferedformat": job.output})
    elif job.output == "webm":
        opts["merge_output_format"] = "webm/mkv"
        pps.append({"key": "FFmpegVideoConvertor", "preferedformat": "webm"})
    elif job.output == "mov":
        opts["merge_output_format"] = "mp4"
        pps.append({"key": "FFmpegVideoRemuxer", "preferedformat": "mov"})
    elif job.output in ("avi", "mpeg"):
        opts["merge_output_format"] = "mp4"  # then re-encoded by ReencodePP
    return opts


class _Logger:
    """Collects yt-dlp errors (and archive skips) so a job can report what happened."""

    def __init__(self, on_error=None, on_skip=None):
        self.errors: list[str] = []
        self.skipped = 0
        self.on_error = on_error
        self.on_skip = on_skip

    def debug(self, msg):
        msg = str(msg)
        if "has already been recorded in the archive" in msg:
            self.skipped += 1
            if self.on_skip:
                title = msg.removeprefix("[download] ").split(" has already been recorded")[0]
                self.on_skip(title)

    info = debug

    def warning(self, msg):
        pass

    def error(self, msg):
        msg = ANSI_RE.sub("", str(msg)).strip().removeprefix("ERROR:").strip().removeprefix("post hooks: ")
        if "ffmpeg not found" in msg.lower() or "ffprobe not found" in msg.lower():
            msg = FFMPEG_HINT
        self.errors.append(msg)
        if self.on_error:
            m = ERROR_ID_RE.match(msg)
            self.on_error(m.group(1) if m else None, msg)


def open_in_os(path: Path, reveal: bool = False) -> None:
    """Open a file with its default app, or show it in Finder/Explorer/the file manager."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if reveal else ["open", str(path)])
    elif sys.platform == "win32":
        if reveal and path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # noqa: S606 - opening a library file the user asked for
    else:
        target = path.parent if reveal and path.is_file() else path
        subprocess.Popen(["xdg-open", str(target)])


class DownloadManager:
    def __init__(self, library_dir: str | os.PathLike, workers: int = 1):
        self.set_library_dir(library_dir)
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue()
        for i in range(max(1, workers)):
            threading.Thread(target=self._worker, name=f"dl-worker-{i}", daemon=True).start()

    def set_library_dir(self, library_dir: str | os.PathLike) -> Path:
        path = Path(library_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        self.library_dir = path
        return path

    # ---- jobs -------------------------------------------------------------

    def submit(self, url: str, quality: str = "720", subtitles: bool = False, output: str = "mp4") -> Job:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("Please enter a full http(s) URL")
        if quality == "audio":  # older clients
            quality, output = "best", "m4a"
        if quality not in QUALITIES:
            raise ValueError(f"Unknown quality: {quality}")
        if output not in VIDEO_OUTPUTS + AUDIO_OUTPUTS:
            raise ValueError(f"Unknown output format: {output}")
        job = Job(url=url, quality=quality, subtitles=subtitles, output=output)
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

    def cancel_all(self) -> int:
        with self._lock:
            active = [j.id for j in self.jobs.values() if j.status not in ("done", "error", "cancelled")]
        return sum(self.cancel(jid) for jid in active)

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
        out_dir = self.library_dir

        def check_cancel():
            if job.cancel_requested:
                raise Cancelled()

        by_id: dict[str, dict] = {}

        def set_item(video_id, status, error=None):
            item = by_id.get(video_id)
            if item and item["status"] not in ("done", "failed", "skipped"):
                item["status"] = status
                if error:
                    item["error"] = error

        def first_unfinished(title=None):
            # Playlist videos are processed in order, so an event that doesn't name its video belongs
            # to the earliest one not yet finished.
            open_items = [i for i in job.items if i["status"] in ("pending", "downloading")]
            named = [i for i in open_items if title and i["title"] == title]
            return (named or open_items or [None])[0]

        def on_skip(title):
            item = first_unfinished(title)
            if item:
                item["status"] = "skipped"

        def on_error(video_id, msg):
            item = by_id.get(video_id) or first_unfinished()
            if item and item["status"] in ("pending", "downloading"):
                item["status"] = "failed"
                item["error"] = msg

        def hook(d):
            check_cancel()
            info = d.get("info_dict") or {}
            job.current_item = info.get("title") or job.current_item
            idx = info.get("playlist_index")
            if info.get("id") not in by_id and idx and 0 < idx <= len(job.items):
                by_id[info["id"]] = job.items[idx - 1]  # listing had no id (non-YouTube sites)
            if info.get("id") in by_id:
                set_item(info["id"], "downloading")
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
            if d["status"] == "started" and d.get("postprocessor") not in ("MoveFiles", "FFmpegThumbnailsConvertor"):
                job.status = "processing"

        def post_hook(filepath):
            # Called by yt-dlp once a video has finished downloading and post-processing.
            # Move it and its metadata/thumbnail/subtitles from the staging folder into the library.
            p = Path(filepath).resolve()
            expected = "." + {"mpeg": "mpg"}.get(job.output, job.output)
            if has_ffmpeg and p.suffix.lower() != expected:
                # yt-dlp carries on after a failed conversion; raising here makes it count the video as
                # failed (and not as downloaded, so it can be retried) instead of keeping the wrong format.
                raise RuntimeError(f"Could not convert to {job.output.upper()}; the download was discarded")
            if p.is_file():
                stem = p.name[: -len(p.suffix)]
                dest_dir = out_dir / p.parent.relative_to(work_dir)
                dest_dir.mkdir(parents=True, exist_ok=True)
                for f in list(p.parent.iterdir()):
                    if f.is_file() and f.name.startswith(stem + "."):
                        os.replace(f, dest_dir / f.name)
                job.completed_items += 1
                job.files.append((dest_dir / p.name).relative_to(out_dir).as_posix())
                m = FILE_ID_RE.search(p.name)
                if m:
                    set_item(m.group(1), "done")
            job.status = "downloading"
            job.percent = 0.0

        logger = _Logger(on_error=on_error, on_skip=on_skip)
        has_ffmpeg = ffmpeg_available()
        work_dir = (out_dir / ".tmp" / job.id).resolve()
        try:
            # Peek at the URL first (cheap, no format resolution) to learn whether it is a playlist,
            # which decides the output folder layout.
            probe_logger = _Logger()
            probe_opts = {"quiet": True, "no_warnings": True, "logger": probe_logger,
                          "color": {"stdout": "no_color", "stderr": "no_color"}}
            with yt_dlp.YoutubeDL(probe_opts) as ydl:
                try:
                    info = ydl.extract_info(job.url, download=False, process=False)
                except yt_dlp.utils.DownloadError:
                    info = None
            if info is None:
                raise RuntimeError(probe_logger.errors[-1] if probe_logger.errors else "Could not read URL")
            job.title = info.get("title") or job.title
            if info.get("_type") in ("playlist", "multi_video"):
                job.is_playlist = True
                # List the playlist's videos up front so each one's progress can be shown.
                entries = info.get("entries") or []
                try:
                    for n, e in enumerate(itertools.islice(entries, MAX_PLAYLIST_LISTING), 1):
                        check_cancel()
                        e = e or {}
                        item = {"index": n, "id": e.get("id"), "title": e.get("title") or e.get("url") or f"Video {n}",
                                "status": "pending", "error": None}
                        job.items.append(item)
                        if item["id"]:
                            by_id[item["id"]] = item
                except Cancelled:
                    raise
                except Exception:  # noqa: BLE001 - the listing is a nicety; the download itself still runs
                    pass
                job.total_items = len(job.items) or info.get("playlist_count") or job.total_items
            check_cancel()
            opts = build_ydl_opts(job, out_dir, hook, pp_hook, post_hook, logger, has_ffmpeg, work_dir)
            with yt_dlp.YoutubeDL(opts) as ydl:
                if has_ffmpeg and job.output in ReencodePP.EXTS:
                    ydl.add_post_processor(ReencodePP(ydl, job.output), when="post_process")
                if has_ffmpeg and job.output == "ogg" and "libvorbis" not in ffmpeg_encoders():
                    ydl.add_post_processor(RenameExtPP(ydl, "ogg"), when="post_process")
                ydl.download([job.url])
            if job.items:
                job.failed_items = sum(i["status"] == "failed" for i in job.items)
            else:
                job.failed_items = 0 if job.completed_items else min(len(logger.errors), 1)
            notes = []
            if not has_ffmpeg and job.completed_items:
                notes.append("Saved without FFmpeg: this is the best single-file version (often lower quality) "
                             "and was not converted. For full quality and other formats, install FFmpeg "
                             "(Mac: brew install ffmpeg)")
            if logger.skipped:
                notes.append(f"{logger.skipped} already in your library")
            if logger.errors:
                failed = job.failed_items or len(logger.errors)
                # For a single video the first error is the cause (e.g. FFmpeg's reason a conversion failed).
                reason = "; ".join(logger.errors) if not job.items else logger.errors[-1]
                notes.append(f"{failed} failed ({'last error: ' if job.items else ''}{reason})")
            job.error = "; ".join(notes) or None
            job.status = "error" if job.completed_items == 0 and not logger.skipped else "done"
        except Cancelled:
            job.status = "cancelled"
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI
            job.status = "error"
            job.error = ANSI_RE.sub("", str(e)).removeprefix("ERROR: ")
        finally:
            leftover = "cancelled" if job.status == "cancelled" else "failed"
            for item in job.items:
                if item["status"] in ("pending", "downloading"):
                    item["status"] = leftover
            shutil.rmtree(work_dir, ignore_errors=True)
            job.finished_at = time.time()
            job.speed = job.eta = None

    # ---- library ----------------------------------------------------------

    def library(self) -> list[dict]:
        items = []
        for info_path in self.library_dir.rglob("*.info.json"):
            if ".tmp" not in info_path.relative_to(self.library_dir).parts:
                items.extend(self._library_items(info_path))
        items.sort(key=lambda i: (i["playlist"] or "", i["playlist_index"] or 0, -i["downloaded_at"]))
        return items

    def _siblings(self, info_path: Path):
        """Files that belong to the same video as info_path: (media files, thumbnail, subtitles)."""
        stem = info_path.name[: -len(".info.json")]
        media, thumb, subs = [], None, []
        for p in info_path.parent.iterdir():
            if not p.name.startswith(stem + "."):
                continue
            rest = p.name[len(stem):]
            if p.suffix.lower() in MEDIA_EXTS and rest.count(".") == 1:
                media.append(p)
            elif p.suffix.lower() in THUMB_EXTS and rest.count(".") == 1:
                thumb = p
            elif p.suffix == ".vtt":
                subs.append({"lang": rest[1:-4], "url": self._rel(p)})
        return sorted(media), thumb, subs

    def _library_items(self, info_path: Path) -> list[dict]:
        media_files, thumb, subs = self._siblings(info_path)
        if not media_files:
            return []
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        return [self._item(info, info_path, media, thumb, subs) for media in media_files]

    def _item(self, info, info_path, media, thumb, subs) -> dict:
        return {
            "id": self._rel(media),
            "video_id": info.get("id"),
            "title": info.get("title") or info_path.name[: -len(".info.json")],
            "channel": info.get("channel") or info.get("uploader"),
            "duration": info.get("duration"),
            "description": info.get("description") or "",
            "upload_date": info.get("upload_date"),
            "webpage_url": info.get("webpage_url"),
            "playlist": (info.get("playlist_title") or info.get("playlist")) if info.get("playlist_index") else None,
            "playlist_index": info.get("playlist_index"),
            "media_url": self._rel(media),
            "path": str(media),
            "folder": str(media.parent),
            "format": media.suffix[1:].lower(),
            "is_audio": media.suffix.lower() in AUDIO_EXTS,
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
        """Delete one downloaded file (item_id is its library-relative path)."""
        media = self.resolve(item_id)
        if media is None or not media.is_file() or media.suffix.lower() not in MEDIA_EXTS:
            return False
        stem = media.name[: -len(media.suffix)]
        info_path = media.with_name(stem + ".info.json")
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        media.unlink()
        # Remove metadata, thumbnail and subtitles once no other format of this video is left.
        if info_path.exists() and not self._siblings(info_path)[0]:
            for p in media.parent.iterdir():
                if p.name.startswith(stem + "."):
                    p.unlink(missing_ok=True)
        if media.parent != self.library_dir and not any(media.parent.iterdir()):
            media.parent.rmdir()
        # Forget it in the download archives so it can be downloaded again later.
        video_id = info.get("id")
        entry = f"{(info.get('extractor_key') or '').lower()} {video_id}"
        fmt = {"mpg": "mpeg"}.get(media.suffix[1:].lower(), media.suffix[1:].lower())
        archives = [self.library_dir / f".archive-{fmt}.txt", self.library_dir / ".archive.txt"]
        if fmt not in VIDEO_OUTPUTS + AUDIO_OUTPUTS:  # e.g. a fallback format saved without FFmpeg
            archives = list(self.library_dir.glob(".archive*.txt"))
        for archive in archives:
            if not video_id or not archive.exists():
                continue
            lines = archive.read_text(encoding="utf-8").splitlines()
            keep = [ln for ln in lines if ln.strip() != entry]
            if len(keep) != len(lines):
                archive.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
        return True
