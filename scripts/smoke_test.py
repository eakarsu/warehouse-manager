#!/usr/bin/env python3
"""Start the local server and verify catalog and operational endpoints."""

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
        visible_features = json.loads(features)
        assert visible_features
        query_route = next((
            route
            for feature in visible_features
            for route in feature.get("routes", [])
            if route.startswith("/") and not route.startswith("/api/") and "?" in route
        ), None)
        _, status_body = fetch("/api/product/status")
        product_status = json.loads(status_body)
        assert product_status["status"] == "ok"
        assert product_status["workflowCount"] == 8
        assert product_status["audit"]["valid"]
        _, workflows_body = fetch("/api/workflows")
        assert len(json.loads(workflows_body)["items"]) == 8
        status, homepage = fetch("/")
        assert status == 200 and b"<!doctype html>" in homepage.lower()
        status, workspace = fetch("/workflows")
        assert status == 200 and b"operations workspace" in workspace.lower()
        if query_route:
            status, feature_page = fetch(query_route)
            assert status == 200
            assert b"no feature selected" not in feature_page.lower()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    query_result = " + query route" if query_route else ""
    print(f"Smoke-tested {manifest['id']}: catalog + 8 operational workflows{query_result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
