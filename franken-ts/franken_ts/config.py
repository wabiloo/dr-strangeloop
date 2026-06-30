from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .utils import parse_time

TimeValue = Union[str, int, float]


class OutputConfig(BaseModel):
    file: Path
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

    @field_validator("file", mode="before")
    @classmethod
    def resolve_path(cls, v: object) -> Path:
        return Path(str(v))

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

    @property
    def is_ad_break(self) -> bool:
        return self.ad_break is not None


class Config(BaseModel):
    output: OutputConfig
    assets: list[AssetConfig] = Field(min_length=1)
    normalize: bool = False

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
