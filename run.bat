@echo off
cd /d "%~dp0"
where ffmpeg >nul 2>nul || (echo FFmpeg not found - installing... & winget install -e --id Gyan.FFmpeg)
where deno >nul 2>nul || (echo Deno not found - installing... & winget install -e --id DenoLand.Deno)
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
pip install -q --disable-pip-version-check -U -r requirements.txt
python -m offline_video.app %*
