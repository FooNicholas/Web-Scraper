"""Build one native JP Price Checker desktop download for the current platform.

Run this from a curator's isolated build environment after refreshing and
reviewing the local catalogue. PyInstaller deliberately builds only for the
host operating system; make the macOS and Windows downloads on their matching
platforms.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scraperbot.distribution import CatalogueSnapshotError, create_catalogue_snapshot  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a one-click JP Price Checker desktop download.")
    parser.add_argument("--catalogue", type=Path, default=ROOT / "data" / "catalogue.sqlite3")
    parser.add_argument("--version", default=datetime.now(UTC).date().isoformat())
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()

    build_root = ROOT / "build" / "desktop-release"
    snapshot_directory = build_root / "catalogue"
    if build_root.exists():
        shutil.rmtree(build_root)
    try:
        snapshot = create_catalogue_snapshot(args.catalogue, snapshot_directory, version=args.version)
    except CatalogueSnapshotError as error:
        parser.exit(2, f"Desktop build stopped: {error}\n")

    separator = os.pathsep
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        "JP Price Checker",
        "--distpath",
        str(args.output.resolve()),
        "--workpath",
        str((build_root / "pyinstaller-work").resolve()),
        "--specpath",
        str(build_root.resolve()),
        "--paths",
        str(ROOT),
        "--collect-all",
        "selenium",
        "--add-data",
        f"{snapshot_directory.resolve()}{separator}catalogue",
        str(ROOT / "scraperbot" / "desktop.py"),
    ]
    environment = os.environ | {"PYINSTALLER_CONFIG_DIR": str(build_root / "pyinstaller-cache")}
    subprocess.run(command, check=True, cwd=ROOT, env=environment)
    print(
        f"Built JP Price Checker with catalogue {snapshot.catalogue_version} in "
        f"{args.output.resolve()}. Upload that platform's archive plus the two files in "
        f"{snapshot_directory} as GitHub Release assets."
    )


if __name__ == "__main__":
    main()
