#!/usr/bin/env python3
"""Dev entrypoint: `uv run --project igor python igor/server.py`."""

import os

import uvicorn

if __name__ == "__main__":
    # IGOR_PORT (default 8090) lets the dev backend dodge a port something else holds;
    # frontend/vite.config.ts reads the same variable for its /api proxy.
    uvicorn.run("igor.app.main:app", host="0.0.0.0", port=int(os.environ.get("IGOR_PORT", "8090")), reload=True)
