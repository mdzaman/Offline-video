#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
pip install -q -U -r requirements.txt
command -v ffmpeg >/dev/null 2>&1 || echo "WARNING: ffmpeg not found - install it for best quality downloads."
exec python -m offline_video.app "$@"
