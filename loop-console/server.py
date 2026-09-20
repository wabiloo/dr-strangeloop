#!/usr/bin/env python3
"""Dev entrypoint: `uv run --project loop-console python loop-console/server.py`."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("loop_console.app.main:app", host="0.0.0.0", port=8090, reload=True)
