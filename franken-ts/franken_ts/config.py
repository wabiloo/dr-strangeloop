from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .utils import parse_time

TimeValue = Union[str, int, float]


class RenditionConfig(BaseModel):
    """One entry in a multi-rendition ABR ladder (see OutputConfig.renditions).

    Each rendition shares the same timeline/ad-break schedule and produces
    its own <output.dir>/<name>.ts, all sharing a single markers.json (PTS
    values are identical across renditions by construction -- see
    loop-dee-loop's SCOPE.md discussion on cross-rendition keyframe
    alignment). `name` becomes both the output filename stem and the
    rendition's identity downstream (loop-dee-loop URLs/manifest labels).
    """

    name: str
    resolution: str
    bitrate_kbps: int

    @property
    def width(self) -> int:
        return int(self.resolution.split("x")[0])

    @property
    def height(self) -> int:
        return int(self.resolution.split("x")[1])


class OutputConfig(BaseModel):
    file: Optional[Path] = None
    dir: Optional[Path] = None
    renditions: Optional[list[RenditionConfig]] = None
    resolution: str = "1920x1080"
    framerate: int = 25
    bitrate_kbps: int = 10000
    gop: Optional[int] = None
    service_provider: str = "broadpeak"
    service_name: str = "broadpeak.io"

    @model_validator(mode="after")
    def default_gop(self) -> "OutputConfig":
        if self.gop is None:
            self.gop = self.framerate * 2
        return self

    @model_validator(mode="after")
    def validate_single_vs_multi_rendition(self) -> "OutputConfig":
        """Exactly one of two mutually exclusive modes:

        - single-rendition (legacy): `file` set, `dir`/`renditions` unset.
          Writes `<file>` + `<file-with-.markers.json-suffix>`, unchanged
          from before this feature existed.
        - multi-rendition: `dir` + `renditions` (non-empty) set, `file`
          unset. Writes `<dir>/<rendition.name>.ts` per rendition +
          `<dir>/markers.json` (shared, written once).
        """
        has_file = self.file is not None
        has_ladder = self.dir is not None or self.renditions is not None

        if has_file and has_ladder:
            raise ValueError(
                "output: specify either 'file' (single-rendition) or "
                "'dir' + 'renditions' (multi-rendition), not both"
            )
        if not has_file and not has_ladder:
            raise ValueError("output: must specify either 'file' or 'dir' + 'renditions'")
        if has_ladder:
            if self.dir is None or self.renditions is None or len(self.renditions) == 0:
                raise ValueError(
                    "output: multi-rendition mode requires both 'dir' and a "
                    "non-empty 'renditions' list"
                )
            names = [r.name for r in self.renditions]
            if len(names) != len(set(names)):
                raise ValueError(f"output.renditions: duplicate rendition name(s) in {names}")
        return self

    @property
    def is_multi_rendition(self) -> bool:
        return self.renditions is not None

    def for_rendition(self, rendition: "RenditionConfig") -> "OutputConfig":
        """Return an effective single-output OutputConfig for one rendition
        of a multi-rendition ladder: same framerate/gop/service metadata,
        resolution/bitrate_kbps/file overridden from the rendition, dir/
        renditions cleared. This lets extract.py/validate.py/cache.py --
        all of which only ever look at a single OutputConfig -- work
        completely unchanged, whether we're in single- or multi-rendition
        mode; only cli.py's orchestration loop needs to know renditions
        exist at all.
        """
        assert self.dir is not None
        return self.model_copy(
            update={
                "file": self.dir / f"{rendition.name}.ts",
                "resolution": rendition.resolution,
                "bitrate_kbps": rendition.bitrate_kbps,
                "dir": None,
                "renditions": None,
            }
        )

    @property
    def width(self) -> int:
        return int(self.resolution.split("x")[0])

    @property
    def height(self) -> int:
        return int(self.resolution.split("x")[1])


class SegmentationConfig(BaseModel):
    type_id: str = "0x34"
    upid_type: str = "0x09"
    upid_hex: str = ""
    web_delivery_allowed: bool = True
    no_regional_blackout: bool = False
    archive_allowed: bool = False
    device_restrictions: int = 1
    duration: Optional[TimeValue] = None

    def duration_seconds(self) -> Optional[float]:
        if self.duration is None:
            return None
        return parse_time(self.duration)


class AdBreakConfig(BaseModel):
    event_id: int
    splice_type: Literal["splice_insert", "time_signal"] = "splice_insert"
    unique_program_id: str = "0x0001"
    avail_num: int = 18
    avails_expected: int = 255
    provider_avail_id: str = "0x00000012"
    segmentation: Optional[SegmentationConfig] = None

    @model_validator(mode="after")
    def segmentation_required_for_time_signal(self) -> "AdBreakConfig":
        if self.splice_type == "time_signal" and self.segmentation is None:
            raise ValueError("'segmentation' is required when splice_type is 'time_signal'")
        return self


class AssetConfig(BaseModel):
    file: Path
    start: Optional[TimeValue] = None
    duration: Optional[TimeValue] = None
    ad_break: Optional[AdBreakConfig] = None
    countdown: Optional[TimeValue] = None
    fade_in: Optional[TimeValue] = None
    fade_out: Optional[TimeValue] = None
    slate_image: Optional[Path] = None

    @field_validator("file", mode="before")
    @classmethod
    def resolve_path(cls, v: object) -> Path:
        return Path(str(v))

    @field_validator("slate_image", mode="before")
    @classmethod
    def resolve_slate_path(cls, v: object) -> Optional[Path]:
        return Path(str(v)) if v is not None else None

    def start_seconds(self) -> Optional[float]:
        if self.start is None:
            return None
        return parse_time(self.start)

    def duration_seconds(self) -> Optional[float]:
        if self.duration is None:
            return None
        return parse_time(self.duration)

    def countdown_seconds(self) -> Optional[float]:
        """Return the countdown window in seconds, or None if not set.

        Returns -1.0 as a sentinel meaning "full clip duration" when the
        configured value is negative.  Callers must clamp this to the actual
        clip duration themselves.
        """
        if self.countdown is None:
            return None
        v = self.countdown
        # Allow -1 (integer or float) as a sentinel for "full clip duration".
        if isinstance(v, (int, float)) and float(v) < 0:
            return -1.0
        return parse_time(v)

    def fade_in_seconds(self) -> Optional[float]:
        if self.fade_in is None:
            return None
        return parse_time(self.fade_in)

    def fade_out_seconds(self) -> Optional[float]:
        if self.fade_out is None:
            return None
        return parse_time(self.fade_out)

    @property
    def is_ad_break(self) -> bool:
        return self.ad_break is not None


class Config(BaseModel):
    output: OutputConfig
    assets: list[AssetConfig] = Field(min_length=1)
    normalize: bool = False
    slate_image: Optional[Path] = None

    @field_validator("slate_image", mode="before")
    @classmethod
    def resolve_global_slate_path(cls, v: object) -> Optional[Path]:
        return Path(str(v)) if v is not None else None

    @model_validator(mode="after")
    def validate_event_ids_unique(self) -> "Config":
        seen: set[int] = set()
        for asset in self.assets:
            if asset.ad_break is not None:
                eid = asset.ad_break.event_id
                if eid in seen:
                    raise ValueError(f"Duplicate ad_break event_id: {eid}")
                seen.add(eid)
        return self


def load_config(path: str | Path) -> Config:
    """Load and validate a YAML config file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open() as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
