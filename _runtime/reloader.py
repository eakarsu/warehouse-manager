#!/usr/bin/env python3
"""Resilient supervisor for generated merged-app servers."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


APP_ROOT = Path(os.environ.get("MERGED_APP_ROOT", os.getcwd())).resolve()
RUNTIME_ROOT = Path(__file__).resolve().parent
SERVER = RUNTIME_ROOT / "server.py"
LOCAL_ENV = APP_ROOT / ".env"
STOPPING = False


def watched_paths():
    paths = list(RUNTIME_ROOT.glob("*.py"))
    paths.extend((APP_ROOT / "manifest.json", APP_ROOT / "workflows.json", LOCAL_ENV))
    return paths


def snapshot():
    state = {}
    for path in watched_paths():
        try:
            stat = path.stat()
            state[str(path)] = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            state[str(path)] = None
    return state


def stop_child(child):
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def request_stop(signum, frame):
    global STOPPING
    STOPPING = True


def start_child():
    environment = os.environ.copy()
    environment["MERGED_APP_ROOT"] = str(APP_ROOT)
    environment["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen([sys.executable, "-u", str(SERVER)], env=environment)


def main():
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    print("Auto-reload enabled for shared runtime, manifest, and environment changes.", flush=True)
    child = start_child()
    state = snapshot()
    while not STOPPING:
        time.sleep(0.5)
        current = snapshot()
        if current != state:
            print("Change detected; restarting app…", flush=True)
            stop_child(child)
            child = start_child()
            state = current
            continue
        if child.poll() is not None:
            code = child.returncode
            print(f"Server exited with code {code}; retrying in 1 second…", flush=True)
            time.sleep(1)
            if not STOPPING:
                child = start_child()
                state = snapshot()
    stop_child(child)


if __name__ == "__main__":
    main()
