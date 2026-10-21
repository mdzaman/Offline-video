@echo off
cd /d "%~dp0"
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -U -r requirements.txt
where ffmpeg >nul 2>nul || echo WARNING: ffmpeg not found - install it for best quality downloads.
python -m offline_video.app %*
