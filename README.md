# Offline Video

A small self-hosted web app for downloading YouTube videos **or whole playlists** and watching them later without an internet connection.

- Paste a single video URL, a playlist URL, or several URLs (one per line)
- Choose the output format, video (**MP4, MKV, WebM, MOV, AVI, MPEG**) or audio only (**MP3, M4A, WAV, FLAC, Opus, OGG**), plus quality (best / 1080p / 720p / 480p / 360p) and optional English subtitles. You can keep the same video in several formats
- See exactly where files are saved, change the folder from the page, and use **Open file** / **Show in folder** (Finder, Explorer) for any download
- Live progress for every download, with per-video counts for playlists; cancel at any time
- Playlists are saved in their own folder, in playlist order; videos that are unavailable are skipped and reported, the rest still download
- Re-adding a playlist only fetches the videos you don't have yet (handy for playlists that grow)
- Built-in library with thumbnails, search, a player with next/previous, autoplay, subtitles and resume-where-you-left-off (formats browsers can't play, such as MKV/AVI/MPEG, open in your computer's player instead)
- Files are ordinary media files in a folder, so you can also copy them to a phone or open them in VLC

Powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp) and Flask.

## Requirements

- Python 3.10+
- [FFmpeg](https://ffmpeg.org/download.html): needed for anything above 360p–720p (YouTube serves HD video and audio separately and FFmpeg joins them) and for every format conversion. Without it the app still works, but saves the best single-file MP4/M4A and shows a warning.
- [Deno](https://deno.com): yt-dlp needs it for full YouTube support.
  - macOS: `brew install ffmpeg deno` · Windows: `winget install Gyan.FFmpeg DenoLand.Deno` · Linux: `sudo apt install ffmpeg` plus [Deno](https://docs.deno.com/runtime/getting_started/installation/)

`run.sh` offers to install both with Homebrew on a Mac, and `run.bat` installs them with winget on Windows (close and reopen the window afterwards so Windows finds them).

## Run it

macOS / Linux:

```bash
./run.sh
```

Windows:

```bat
run.bat
```

Then open <http://127.0.0.1:8000>. The scripts create a virtual environment and install dependencies on first run.

Or manually:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m offline_video.app        # options: --port 8000 --dir ~/Videos/offline --workers 2 --host 0.0.0.0
```

Downloads go to `./downloads` by default. Change it on the page (**Saving to → Change…**; the choice is remembered in `~/.offline_video.json`), or with `--dir` / the `OFFLINE_VIDEO_DIR` environment variable.
Use `--host 0.0.0.0` to reach the app from other devices on your home network (there is no login, so only do this on a network you trust).

## Tips

- **YouTube changes often.** If downloads start failing, update yt-dlp: `pip install -U yt-dlp` (`run.sh` / `run.bat` do this automatically).
- A link like `watch?v=…&list=…` (a video opened from a playlist) downloads the whole playlist. To grab only that one video, remove the `&list=…` part.
- Browsers play MP4, WebM, MOV (Safari) and the audio formats directly. Other formats open in your computer's default player; [VLC](https://www.videolan.org/) plays all of them.
- AVI and MPEG require re-encoding the video, which takes noticeably longer than the other formats.
- **Open file** and **Show in folder** only work in a browser on the computer that runs the app.
- Deleting a video from the library removes it from disk and lets you download it again.

## Layout

```
downloads/
├── Some video [dQw4w9WgXcQ].mp4          # single videos
├── Some video [dQw4w9WgXcQ].info.json    # metadata used by the library
├── Some video [dQw4w9WgXcQ].jpg          # thumbnail
├── Some video [dQw4w9WgXcQ].mp3          # the same video in another format
└── My Playlist/
    ├── 001 - First video [abc123].mp4
    └── 002 - Second video [def456].mp4
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests use a local HTTP server and generated clips, so they don't need YouTube access.

## Please note

Only download videos you have the right to save (your own uploads, Creative Commons content, or where the creator/YouTube permits it). YouTube's Terms of Service restrict downloading in many cases; YouTube Premium also offers official offline viewing in its apps.
