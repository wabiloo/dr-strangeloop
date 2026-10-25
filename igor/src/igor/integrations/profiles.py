"""Output profiles: reusable encoding settings (single bitrate or an ABR
ladder) for the playlist editor's Output section.

Built-ins ship here, read-only. User profiles are YAML files in
`paths.PROFILES_DIR` (data/profiles/<name>.yaml). Applying a profile copies its
values into the playlist, so a playlist YAML never references a profile.
"""

from __future__ import annotations

import re

import yaml

from igor import paths

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._, -]{0,79}$")
_RESOLUTION_RE = re.compile(r"^\d{2,5}x\d{2,5}$")

PROFILES = ("baseline", "main", "high")
LEVELS = (
    "1.0", "1.1", "1.2", "1.3", "2.0", "2.1", "2.2", "3.0", "3.1", "3.2",
    "4.0", "4.1", "4.2", "5.0", "5.1", "5.2", "6.0", "6.1", "6.2",
)

# The 5-rung ladder is broadpeak.io's default offline-transcoding profile; its
# `width: -2` (keep aspect, even width) is spelled out as 16:9 widths, and its
# avc1.XXXXXX codec strings give each rung's H.264 profile and level.
BUILTINS: list[dict] = [
    {
        "name": "1080p single, 5 Mbps",
        "mode": "single",
        "resolution": "1920x1080",
        "bitrate_kbps": 5000,
        "framerate": 25,
        "gop": 50,
        "profile": "high",
        "level": "4.0",
    },
    {
        "name": "broadpeak default, 5-rung ladder",
        "mode": "ladder",
        "renditions": [
            {"name": "232p", "resolution": "412x232", "bitrate_kbps": 500, "profile": "baseline", "level": "1.3"},
            {"name": "360p", "resolution": "640x360", "bitrate_kbps": 1600, "profile": "main", "level": "3.0"},
            {"name": "480p", "resolution": "854x480", "bitrate_kbps": 2300, "profile": "main", "level": "3.1"},
            {"name": "720p", "resolution": "1280x720", "bitrate_kbps": 3200, "profile": "high", "level": "3.2"},
            {"name": "1080p", "resolution": "1920x1080", "bitrate_kbps": 5000, "profile": "high", "level": "4.0"},
        ],
        "framerate": 25,
        "gop": 25,
        "profile": "high",
        "level": "4.0",
    },
]
_BUILTIN_NAMES = {p["name"].casefold() for p in BUILTINS}


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            "Profile name must start with a letter or digit and contain only letters, digits, "
            "spaces, commas, dots, underscores or hyphens (max 80 characters)."
        )
    return name


def _file_for(name: str):
    # Slug keeps the file name filesystem-safe; the real name lives in the file.
    return paths.PROFILES_DIR / (re.sub(r"[^A-Za-z0-9._-]+", "_", name) + ".yaml")


def _positive_int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _validate_resolution(value, label: str) -> str:
    if not isinstance(value, str) or not _RESOLUTION_RE.fullmatch(value):
        raise ValueError(f"{label} must look like 1920x1080.")
    return value


def _validate_h264(profile, level, label: str) -> dict:
    """Optional profile/level pair; returns only the keys that are set."""
    out: dict = {}
    if profile is not None:
        if profile not in PROFILES:
            raise ValueError(f"{label}profile must be one of {', '.join(PROFILES)}.")
        out["profile"] = profile
    if level is not None:
        if level not in LEVELS:
            raise ValueError(f"{label}level must be one of {', '.join(LEVELS)}.")
        out["level"] = level
    return out


def normalize(data: dict) -> dict:
    """Validate a profile body and return only the known fields."""
    mode = data.get("mode")
    if mode not in ("single", "ladder"):
        raise ValueError("mode must be 'single' or 'ladder'.")
    out: dict = {"mode": mode, "framerate": _positive_int(data.get("framerate"), "framerate")}
    if data.get("gop") is not None:
        out["gop"] = _positive_int(data["gop"], "gop")
    out.update(_validate_h264(data.get("profile"), data.get("level"), ""))
    if mode == "single":
        out["resolution"] = _validate_resolution(data.get("resolution"), "resolution")
        out["bitrate_kbps"] = _positive_int(data.get("bitrate_kbps"), "bitrate_kbps")
    else:
        renditions = data.get("renditions")
        if not isinstance(renditions, list) or not renditions:
            raise ValueError("A ladder needs at least one rendition.")
        seen: set[str] = set()
        out["renditions"] = []
        for r in renditions:
            rname = str(r.get("name") or "").strip()
            if not rname or rname in seen:
                raise ValueError("Every rendition needs a unique name.")
            seen.add(rname)
            out["renditions"].append({
                "name": rname,
                "resolution": _validate_resolution(r.get("resolution"), f"rendition {rname} resolution"),
                "bitrate_kbps": _positive_int(r.get("bitrate_kbps"), f"rendition {rname} bitrate_kbps"),
                **_validate_h264(r.get("profile"), r.get("level"), f"rendition {rname} "),
            })
    for key in ("service_provider", "service_name"):
        if data.get(key):
            out[key] = str(data[key])
    return out


def list_profiles() -> list[dict]:
    """Built-ins first, then user profiles by name."""
    result = [{**p, "builtin": True} for p in BUILTINS]
    user: list[dict] = []
    if paths.PROFILES_DIR.is_dir():
        for f in paths.PROFILES_DIR.glob("*.yaml"):
            try:
                raw = yaml.safe_load(f.read_text()) or {}
                user.append({"name": _validate_name(raw["name"]), **normalize(raw), "builtin": False})
            except (OSError, yaml.YAMLError, KeyError, ValueError, AttributeError):
                continue  # a hand-edited file that no longer validates is skipped, not fatal
    return result + sorted(user, key=lambda p: p["name"].casefold())


def save_profile(name: str, data: dict) -> dict:
    name = _validate_name(name)
    if name.casefold() in _BUILTIN_NAMES:
        raise PermissionError(f"'{name}' is a built-in profile; save under another name.")
    body = normalize(data)
    paths.PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    _file_for(name).write_text(yaml.safe_dump({"name": name, **body}, sort_keys=False))
    return {"name": name, **body, "builtin": False}


def delete_profile(name: str) -> None:
    name = _validate_name(name)
    if name.casefold() in _BUILTIN_NAMES:
        raise PermissionError(f"'{name}' is a built-in profile and can't be deleted.")
    path = _file_for(name)
    if not path.is_file():
        raise FileNotFoundError(f"No profile named '{name}'.")
    path.unlink()
