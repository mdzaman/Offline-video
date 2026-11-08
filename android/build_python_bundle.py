"""Build assets/pyapp.zip for the Android app: the offline_video package plus its pure-Python dependencies.

The phone runs these with the Python 3.12 that ships with youtubedl-android (which already includes
pycryptodomex and mutagen). Compiled extensions from the build machine can't run on Android, so they are
removed; every dependency used here has a pure-Python fallback.

Usage: python3 build_python_bundle.py OUTPUT_DIR
"""

import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PACKAGES = [
    "flask", "werkzeug", "jinja2", "markupsafe", "itsdangerous", "click", "blinker",
    "segno", "yt-dlp", "yt-dlp-ejs",
]
NATIVE_SUFFIXES = (".so", ".pyd", ".dylib")


def main(out_dir: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp) / "pyapp"
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--no-compile",
             "--disable-pip-version-check", "--target", str(site), *PACKAGES],
            check=True,
        )
        for p in list(site.rglob("*")):
            if p.is_file() and (p.suffix in NATIVE_SUFFIXES or ".so." in p.name):
                p.unlink()
            elif p.is_dir() and p.name in ("__pycache__", "bin", "share"):
                shutil.rmtree(p, ignore_errors=True)
        shutil.copytree(root / "offline_video", site / "offline_video",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        zip_path = out_dir / "pyapp.zip"
        digest = hashlib.sha256()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(site.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(site).as_posix()
                    data = p.read_bytes()
                    digest.update(rel.encode() + b"\0" + data)
                    zf.writestr(rel, data)
        # The app re-extracts the bundle only when this changes (i.e. after an update).
        (out_dir / "pyapp.sha256").write_text(digest.hexdigest())
    print(f"Python bundle: {zip_path} ({zip_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
