"""Platform-independent media and experiment inputs."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass
import math
import re
from typing import Literal, TypeAlias


JSONValue: TypeAlias = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]
MediaType: TypeAlias = Literal["video", "image", "stimulus"]
FilterOperator: TypeAlias = Literal["gt", "lt", "eq", "gte", "lte", "is", "is_not"]


class UnsupportedMediaError(ValueError):
    """The requested modality has no execution implementation yet."""


def identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError("IDs must be 1–128 ASCII letters, digits, underscores or hyphens")
    return value


def media_identifier(value: object) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 256
            or not value.isprintable() or value != value.strip()):
        raise ValueError("Media IDs must be normalized printable strings of 1–256 characters")
    return value


def _count(name: str, value: int, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True)
class Media(ABC):
    media_id: str

    def __post_init__(self) -> None:
        media_identifier(self.media_id)

    @property
    @abstractmethod
    def media_type(self) -> MediaType:
        """Wire-format modality identifier."""


@dataclass(frozen=True)
class Video(Media):
    @property
    def media_type(self) -> Literal["video"]:
        return "video"


@dataclass(frozen=True)
class Image(Media):
    """Future modality descriptor; experiment execution is unsupported."""

    @property
    def media_type(self) -> Literal["image"]:
        return "image"


@dataclass(frozen=True)
class Stimulus(Media):
    """Future generated-stimulus descriptor; execution is unsupported."""

    @property
    def media_type(self) -> Literal["stimulus"]:
        return "stimulus"


def media_from_row(row: Mapping[str, JSONValue]) -> Media:
    """Convert a catalog query row or manifest trial into a typed descriptor."""
    media_id = media_identifier(row.get("media_id", row.get("clip_id")))
    media_type = row.get("media_type", "video")
    if media_type == "video":
        return Video(media_id)
    if media_type == "image":
        return Image(media_id)
    if media_type == "stimulus":
        return Stimulus(media_id)
    raise UnsupportedMediaError("Unknown media descriptor type")


@dataclass(frozen=True)
class MetricFilter:
    metric_id: str
    operator: FilterOperator
    value: int | float | bool | None

    def __post_init__(self) -> None:
        if not isinstance(self.metric_id, str) or not self.metric_id or not self.metric_id.isprintable():
            raise ValueError("metric_id must be a nonempty printable string")
        if self.operator not in ("gt", "lt", "eq", "gte", "lte", "is", "is_not"):
            raise ValueError("Unsupported metric operator; consult the metrics inventory")
        if self.value is not None and type(self.value) not in (int, float, bool):
            raise ValueError("Metric values must be numeric, boolean or null")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("Metric values must be finite")

    def to_dict(self) -> JSONObject:
        return {"metric_id": self.metric_id, "operator": self.operator, "value": self.value}


@dataclass(frozen=True)
class ExperimentSpec:
    """Parent counts are TOTAL per subject, never per block; foils are extra."""

    name: str
    seed: str
    subject_count: int
    parents_per_subject: int
    shared_per_subject: int = 0
    repeats_per_subject: int = 0
    foils_per_subject: int = 0
    block_count: int = 1
    foils_per_block: int | None = None
    balance_cuts: bool = False

    def __post_init__(self) -> None:
        for name, maximum in (("name", 120), ("seed", 128)):
            value = getattr(self, name)
            if type(value) is not str or not value.strip() or len(value) > maximum or not value.isprintable():
                raise ValueError(f"{name} must be a printable string of 1–{maximum} characters")
        for name in ("subject_count", "parents_per_subject", "block_count"):
            _count(name, getattr(self, name), 1)
        for name in ("shared_per_subject", "repeats_per_subject", "foils_per_subject"):
            _count(name, getattr(self, name))
        if self.subject_count > 100 or self.parents_per_subject > 10000:
            raise ValueError("At most 100 subjects and 10,000 parents per subject are supported")
        if self.parents_per_subject % self.block_count:
            raise ValueError("Total parents_per_subject must divide evenly into block_count")
        if self.foils_per_block is not None:
            _count("foils_per_block", self.foils_per_block)
            total = self.foils_per_block * self.block_count
            if self.foils_per_subject not in (0, total):
                raise ValueError("foils_per_subject conflicts with foils_per_block * block_count")
            object.__setattr__(self, "foils_per_subject", total)
        for name in ("shared_per_subject", "repeats_per_subject", "foils_per_subject"):
            if getattr(self, name) > self.parents_per_subject:
                raise ValueError(f"{name} cannot exceed total parents_per_subject")
        if self.subject_count * self.trials_per_subject > 50000:
            raise ValueError("Experiment exceeds 50,000 total trials")
        if type(self.balance_cuts) is not bool:
            raise ValueError("balance_cuts must be boolean")
        if self.balance_cuts and (self.parents_per_subject % 2 or self.foils_per_subject % 2
                                 or self.block_count != 1 or self.shared_per_subject or self.repeats_per_subject):
            raise ValueError("Cut balance requires even parent/foil counts, one block, no sharing or repeats")

    @property
    def trials_per_subject(self) -> int:
        return self.parents_per_subject + self.repeats_per_subject + self.foils_per_subject

    def to_dict(self) -> JSONObject:
        result = asdict(self)
        if self.foils_per_block is None:
            result.pop("foils_per_block")
        if not self.balance_cuts:
            result.pop("balance_cuts")
        return result
