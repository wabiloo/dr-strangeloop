#!/usr/bin/env python3
"""Dev entrypoint: `uv run --project igor python igor/server.py`."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("igor.app.main:app", host="0.0.0.0", port=8090, reload=True)
