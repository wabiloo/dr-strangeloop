"""Route registration: one `register(app, ctx)` per module, each binding the shared ServeContext state."""

from __future__ import annotations

from flask import Flask

from serving.context import ServeContext
from serving.routes import lowlatency, manifests, meta, segments


def register_all(app: Flask, ctx: ServeContext) -> None:
    meta.register(app, ctx)
    manifests.register(app, ctx)
    segments.register(app, ctx)
    lowlatency.register(app, ctx)
