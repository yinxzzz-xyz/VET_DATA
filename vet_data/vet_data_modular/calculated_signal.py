"""Data contracts for formula-based calculated signals."""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

import numpy as np


CALCULATED_SIGNAL_SCHEMA_VERSION = 1


class CalculationStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    ERROR = "error"


@dataclass(frozen=True)
class CalculatedSignalDefinition:
    stable_id: str
    display_name: str
    user_formula: str
    normalized_formula: str = ""
    signal_tokens: dict[str, str] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    result_unit: str = ""
    alignment_policy: str = "intersection_union"
    interpolation_policy: dict[str, str] = field(
        default_factory=lambda: {"continuous": "linear", "discrete": "previous"}
    )
    numeric_policy: dict[str, str] = field(
        default_factory=lambda: {"invalid": "nan", "divide_by_zero": "nan"}
    )
    comment: str = ""
    schema_version: int = CALCULATED_SIGNAL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible definition data, never calculated arrays."""
        data = asdict(self)
        data["dependencies"] = list(self.dependencies)
        return data


@dataclass
class CalculatedSignalResult:
    timestamps: np.ndarray = field(default_factory=lambda: np.array([], dtype=float))
    samples: np.ndarray = field(default_factory=lambda: np.array([], dtype=float))
    unit: str = ""
    status: CalculationStatus = CalculationStatus.PENDING
    error: str | None = None
    nan_count: int = 0
    inf_count: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)
