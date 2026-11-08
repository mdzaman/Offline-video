#!/usr/bin/env sh
# Starts Offline Video. First run creates a Python environment and installs what's needed.
set -e
cd "$(dirname "$0")"

# FFmpeg (merging HD video+audio, format conversion) and Deno (needed by yt-dlp for full YouTube support).
missing=""
command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"
command -v deno >/dev/null 2>&1 || missing="$missing deno"
if [ -n "$missing" ]; then
  if command -v brew >/dev/null 2>&1; then
    printf "Missing:%s. Install now with Homebrew? [Y/n] " "$missing"
    read -r answer
    case "$answer" in [nN]*) ;; *) brew install $missing ;; esac
  else
    echo "WARNING: missing:$missing. Install them for full-quality downloads and format conversion:"
    echo "  Mac: brew install$missing    Linux: see https://ffmpeg.org and https://deno.com"
  fi
fi

if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
pip install -q --disable-pip-version-check -U -r requirements.txt
exec python -m offline_video.app "$@"
