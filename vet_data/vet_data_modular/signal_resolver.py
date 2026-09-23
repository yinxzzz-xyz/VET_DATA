"""GUI-independent resolution of entries in the VET_DATA signal catalog."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
from asammdf import Signal
from asammdf.blocks.utils import MdfException


class SignalSource(str, Enum):
    MDF = "mdf"
    CSV = "csv"
    VBO = "vbo"
    CAN = "can"
    LEGACY_MATH = "legacy_math"


class SampleKind(str, Enum):
    NUMERIC = "numeric"
    TEXT = "text"
    UNSUPPORTED = "unsupported"


class SignalResolutionError(LookupError):
    """Base class for deterministic signal resolution failures."""


class SignalKeyNotFoundError(SignalResolutionError):
    def __init__(self, signal_key: str):
        super().__init__(f"信号目录中不存在唯一 key: {signal_key}")
        self.signal_key = signal_key


class SignalDataUnavailableError(SignalResolutionError):
    def __init__(self, signal_key: str, reason: str):
        super().__init__(f"信号 {signal_key} 无法解析: {reason}")
        self.signal_key = signal_key
        self.reason = reason


@dataclass(frozen=True)
class ResolvedSignal:
    key: str
    name: str
    display_name: str
    signal: Signal
    source: SignalSource
    unit: str = ""
    group: int | None = None
    channel: int | None = None
    comment: str = ""
    sample_kind: SampleKind = SampleKind.NUMERIC

    @property
    def samples(self) -> np.ndarray:
        return self.signal.samples

    @property
    def timestamps(self) -> np.ndarray:
        return self.signal.timestamps


class SignalResolver:
    """Resolve unique catalog keys without depending on Qt or a main window."""

    def __init__(
        self,
        signal_catalog: Mapping[str, Mapping[str, Any]],
        *,
        data_source: Any = None,
        data_path: str = "",
        can_data: Mapping[str, Mapping[str, Any]] | None = None,
        legacy_math_data: Mapping[str, Mapping[str, Any]] | None = None,
    ):
        self.signal_catalog = signal_catalog
        self.data_source = data_source
        self.data_path = data_path
        self.can_data = can_data or {}
        self.legacy_math_data = legacy_math_data or {}

    def resolve(self, signal_key: str) -> ResolvedSignal:
        try:
            signal_info = self.signal_catalog[signal_key]
        except KeyError as exc:
            raise SignalKeyNotFoundError(signal_key) from exc
        return self._resolve_info(signal_key, signal_info)

    def resolve_info(self, signal_info: Mapping[str, Any]) -> ResolvedSignal:
        """Compatibility entry for callers that only retain catalog metadata."""
        signal_key = next(
            (key for key, value in self.signal_catalog.items() if value is signal_info),
            str(signal_info.get("name", "")),
        )
        return self._resolve_info(signal_key, signal_info)

    @staticmethod
    def classify_samples(samples: Any) -> SampleKind:
        kind = np.asarray(samples).dtype.kind
        if kind in "biufc":
            return SampleKind.NUMERIC
        if kind in "USO":
            return SampleKind.TEXT
        return SampleKind.UNSUPPORTED

    def _resolve_info(self, signal_key: str, info: Mapping[str, Any]) -> ResolvedSignal:
        name = str(info.get("name", signal_key))
        data_key = signal_key if signal_key in self.legacy_math_data else name
        if data_key in self.legacy_math_data:
            item = self.legacy_math_data[data_key]
            signal = Signal(
                samples=item["samples"], timestamps=item["timestamps"], name=name, unit="Math"
            )
            return self._result(signal_key, info, signal, SignalSource.LEGACY_MATH)

        data_key = signal_key if signal_key in self.can_data else name
        if data_key in self.can_data:
            item = self.can_data[data_key]
            signal = Signal(
                samples=item["samples"], timestamps=item["timestamps"],
                name=name, unit=item.get("unit", ""),
            )
            return self._result(signal_key, info, signal, SignalSource.CAN)

        suffix = Path(self.data_path).suffix.lower()
        if suffix in {".mdf", ".mf4"}:
            if self.data_source is None or not hasattr(self.data_source, "get"):
                raise SignalDataUnavailableError(signal_key, "MDF 数据源不可用")
            try:
                signal = self.data_source.get(
                    name, group=info.get("group"), index=info.get("channel")
                )
            except MdfException as exc:
                raise SignalDataUnavailableError(signal_key, str(exc)) from exc
            if signal is None:
                raise SignalDataUnavailableError(signal_key, "MDF 未返回信号")
            return self._result(signal_key, info, signal, SignalSource.MDF)

        if suffix in {".csv", ".vbo"}:
            if not isinstance(self.data_source, pd.DataFrame) or name not in self.data_source.columns:
                raise SignalDataUnavailableError(signal_key, f"数据列不存在: {name}")
            values = self.data_source[name]
            timestamps = self.data_source.index.to_numpy()
            if len(timestamps) == 0 or len(values) == 0:
                raise SignalDataUnavailableError(signal_key, "信号数据为空")
            signal = Signal(
                samples=values.to_numpy(), timestamps=timestamps.astype(float),
                name=name, unit=str(info.get("unit", "")),
            )
            source = SignalSource.CSV if suffix == ".csv" else SignalSource.VBO
            return self._result(signal_key, info, signal, source)

        raise SignalDataUnavailableError(
            signal_key, f"不支持的数据来源: {suffix or 'unknown'}"
        )

    def _result(
        self, signal_key: str, info: Mapping[str, Any], signal: Signal,
        source: SignalSource,
    ) -> ResolvedSignal:
        signal, sample_kind = self._normalize_legacy_signal(signal)
        return ResolvedSignal(
            key=signal_key,
            name=str(info.get("name", signal.name)),
            display_name=str(info.get("display_name", info.get("name", signal_key))),
            signal=signal,
            source=source,
            unit=str(getattr(signal, "unit", "") or ""),
            group=info.get("group"), channel=info.get("channel"),
            comment=str(info.get("comment", "") or ""), sample_kind=sample_kind,
        )

    @classmethod
    def _normalize_legacy_signal(cls, signal: Signal) -> tuple[Signal, SampleKind]:
        if signal is None or len(signal.samples) == 0:
            return signal, SampleKind.UNSUPPORTED
        kind = cls.classify_samples(signal.samples)
        if kind is SampleKind.TEXT:
            string_samples = np.array([str(sample) for sample in signal.samples], dtype=str)
            unique_values = list(dict.fromkeys(string_samples))
            value_map = {value: index for index, value in enumerate(unique_values)}
            normalized = Signal(
                samples=np.array([value_map[value] for value in string_samples], dtype=float),
                timestamps=signal.timestamps.copy(), name=signal.name,
                unit=getattr(signal, "unit", ""),
            )
            normalized.text_mapping = unique_values
            normalized.raw_text_values = string_samples.tolist()
            normalized.is_text_signal = True
            return normalized, kind
        try:
            signal.samples = signal.samples.astype(float)
        except (ValueError, TypeError):
            signal.samples = np.array(
                [cls._float_or_nan(sample) for sample in signal.samples], dtype=float
            )
        signal.is_text_signal = False
        return signal, kind

    @staticmethod
    def _float_or_nan(value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return np.nan
