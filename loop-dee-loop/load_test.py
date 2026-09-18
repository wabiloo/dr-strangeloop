"""Lightweight concurrent load generator for resource-usage evaluation.

Simulates N concurrent "viewers" continuously polling the HLS/DASH
manifests and downloading whatever new segments they advertise, for a
fixed duration. Used alongside a process-sampling script to characterize
CPU/memory usage under realistic access patterns.
"""

from __future__ import annotations

import argparse
import re
import threading
import time
import urllib.request

STOP = threading.Event()
stats_lock = threading.Lock()
stats = {"requests": 0, "bytes": 0, "errors": 0}


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.read()


def viewer_loop(base_url: str, viewer_id: int):
    seen_segments: set[str] = set()
    while not STOP.is_set():
        try:
            hls = _get(f"{base_url}/live.m3u8").decode("utf-8", "replace")
            with stats_lock:
                stats["requests"] += 1
                stats["bytes"] += len(hls)

            seg_paths = re.findall(r"(/seg/\d+\.m4s)", hls)
            for seg_path in seg_paths:
                if seg_path in seen_segments:
                    continue
                seen_segments.add(seg_path)
                data = _get(f"{base_url}{seg_path}")
                with stats_lock:
                    stats["requests"] += 1
                    stats["bytes"] += len(data)
                if len(seen_segments) > 200:
                    seen_segments.clear()
        except Exception:
            with stats_lock:
                stats["errors"] += 1
        time.sleep(2.0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--viewers", type=int, default=10)
    parser.add_argument("--duration", type=float, default=180.0)
    args = parser.parse_args()

    threads = [
        threading.Thread(target=viewer_loop, args=(args.base_url, i), daemon=True)
        for i in range(args.viewers)
    ]
    for t in threads:
        t.start()

    start = time.time()
    while time.time() - start < args.duration:
        time.sleep(5)
        with stats_lock:
            elapsed = time.time() - start
            print(
                f"[{elapsed:6.1f}s] requests={stats['requests']} "
                f"bytes={stats['bytes']/1e6:.1f}MB errors={stats['errors']}"
            )

    STOP.set()
    for t in threads:
        t.join(timeout=5)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
