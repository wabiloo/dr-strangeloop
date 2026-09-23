"""Shared "is the manifest actually being served" check, used by every
backend's `status()` (see _aws_media_ops.py, _ecs_express_ops.py,
_local_docker_ops.py).

The raw infrastructure signal each backend otherwise reports (MediaLive
Channel.State RUNNING, ECS minTaskCount > 0, Docker container State.Status
running, CloudFormation stack COMPLETE) only means the *compute* is up --
not that HlsPlaybackUrl/DashPlaybackUrl are actually returning a manifest.
A MediaPackage endpoint pointed at the wrong channel, an empty ECS target
group, a CloudFront distribution that hasn't finished propagating, or a
wrong bucket/prefix can all leave the infrastructure reporting "running"
while playback returns nothing -- this is the gap between "alive" and
merely "not-stopped" that igor's Phase enum surfaces (see
igor/frontend/src/utils/channelPhase.ts).
"""

import urllib.error
import urllib.request

_TIMEOUT_SECONDS = 5
_USER_AGENT = "its-a-live-health-check/1.0"


def _url_ok(url):
    """True/False if `url` was actually reached and returned a 2xx/other
    status, None if there's no URL to check at all (e.g. outputs not
    populated yet)."""
    if not url:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def check_manifest_reachable(hls_url, dash_url):
    """True if at least one of the playback URLs actually returns a 2xx
    response, False if both are known but neither does, None if neither
    URL is known yet. Callers should only invoke this when the backend's
    own raw status already claims to be running/serving -- there's no
    point network-checking a channel that's intentionally stopped."""
    hls_ok = _url_ok(hls_url)
    dash_ok = _url_ok(dash_url)
    if hls_ok is None and dash_ok is None:
        return None
    return bool(hls_ok) or bool(dash_ok)
