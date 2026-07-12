#!/usr/bin/env python3
"""Start the local server and verify its public, non-AI endpoints."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]


def fetch(path: str):
    with urlopen(f"http://127.0.0.1:4599{path}", timeout=2) as response:
        return response.status, response.read()


def main() -> int:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    environment = os.environ.copy()
    environment.update({"MERGED_APP_ROOT": str(ROOT), "MERGED_HOST": "127.0.0.1", "PORT": "4599"})
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "_runtime" / "server.py")],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(40):
            try:
                _, body = fetch("/api/manifest")
                break
            except (OSError, URLError):
                time.sleep(0.1)
        else:
            raise RuntimeError("server did not become ready")
        assert json.loads(body)["id"] == manifest["id"]
        _, features = fetch("/api/features")
        assert json.loads(features)
        status, homepage = fetch("/")
        assert status == 200 and b"<!doctype html>" in homepage.lower()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    print(f"Smoke-tested {manifest['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
