# Offline Video

A small self-hosted web app for downloading YouTube videos **or whole playlists** and watching them later without an internet connection.

- Paste a single video URL, a playlist URL, or several URLs (one per line)
- Choose quality (best / 1080p / 720p / 480p / 360p / audio only) and optional English subtitles
- Live progress for every download, with per-video counts for playlists; cancel at any time
- Playlists are saved in their own folder, in playlist order; videos that are unavailable are skipped and reported, the rest still download
- Re-adding a playlist only fetches the videos you don't have yet (handy for playlists that grow)
- Built-in library with thumbnails, search, a player with next/previous, autoplay, subtitles and resume-where-you-left-off
- Files are ordinary `.mp4` files in a folder, so you can also copy them to a phone or open them in VLC

Powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp) and Flask.

## Requirements

- Python 3.10+
- [FFmpeg](https://ffmpeg.org/download.html) on your `PATH` (needed to merge YouTube's separate audio and video streams)
  - macOS: `brew install ffmpeg` · Ubuntu/Debian: `sudo apt install ffmpeg` · Windows: `winget install ffmpeg`

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

Downloads go to `./downloads` by default (override with `--dir` or the `OFFLINE_VIDEO_DIR` environment variable).
Use `--host 0.0.0.0` to reach the app from other devices on your home network (there is no login, so only do this on a network you trust).

## Tips

- **YouTube changes often.** If downloads start failing, update yt-dlp: `pip install -U yt-dlp` (`run.sh` / `run.bat` do this automatically).
- A link like `watch?v=…&list=…` (a video opened from a playlist) downloads the whole playlist. To grab only that one video, remove the `&list=…` part.
- Browsers play the default `.mp4` downloads directly. If a file won't play in the browser, use **Save file** and open it in VLC.
- Deleting a video from the library removes it from disk and lets you download it again.

## Layout

```
downloads/
├── Some video [dQw4w9WgXcQ].mp4          # single videos
├── Some video [dQw4w9WgXcQ].info.json    # metadata used by the library
├── Some video [dQw4w9WgXcQ].jpg          # thumbnail
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
