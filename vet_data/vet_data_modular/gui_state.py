"""Safe persistence for the small set of main-window GUI state in UI-2."""

from __future__ import annotations

from collections.abc import Sequence

from PyQt6.QtCore import QByteArray, QRect, QSettings
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QSplitter, QWidget


ORGANIZATION_NAME = "VET_DATA"
APPLICATION_NAME = "VET_DATA"
GEOMETRY_KEY = "main_window/geometry"
HORIZONTAL_SPLITTER_SIZES_KEY = "main_window/horizontal_splitter_sizes"
CAN_PANEL_EXPANDED_KEY = "main_window/panels/can_expanded"
GPS_PANEL_EXPANDED_KEY = "main_window/panels/gps_expanded"

DEFAULT_GEOMETRY = (100, 100, 1450, 950)
DEFAULT_SPLITTER_SIZES = (360, 1080)
MIN_LEFT_WIDTH = 320
MIN_RIGHT_WIDTH = 480
MIN_VISIBLE_WIDTH = 100
MIN_VISIBLE_HEIGHT = 100


def create_gui_settings() -> QSettings:
    return QSettings(ORGANIZATION_NAME, APPLICATION_NAME)


def _valid_geometry(window: QWidget) -> bool:
    geometry = window.frameGeometry()
    # Window managers may clamp a restored window to the available desktop and
    # subtract frame pixels.  Child minimum widths still protect the splitter.
    if geometry.width() < 640:
        return False
    if geometry.height() < 500:
        return False
    screens = QGuiApplication.screens()
    if not screens:
        return True
    for screen in screens:
        intersection = geometry.intersected(screen.availableGeometry())
        if (
            intersection.width() >= MIN_VISIBLE_WIDTH
            and intersection.height() >= MIN_VISIBLE_HEIGHT
        ):
            return True
    return False


def _coerce_splitter_sizes(value) -> tuple[int, int] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    if len(value) != 2:
        return None
    try:
        sizes = tuple(int(item) for item in value)
    except (TypeError, ValueError, OverflowError):
        return None
    left, right = sizes
    if left < MIN_LEFT_WIDTH or right < MIN_RIGHT_WIDTH:
        return None
    if left + right > 100_000:
        return None
    return left, right


def restore_main_window_state(
    window: QWidget, splitter: QSplitter, settings: QSettings
) -> tuple[int, int]:
    """Restore valid state and independently fall back for damaged values."""
    window.setGeometry(*DEFAULT_GEOMETRY)
    splitter.setSizes(list(DEFAULT_SPLITTER_SIZES))

    try:
        geometry = settings.value(GEOMETRY_KEY)
        if isinstance(geometry, QByteArray) and not geometry.isEmpty():
            if not window.restoreGeometry(geometry) or not _valid_geometry(window):
                window.setGeometry(*DEFAULT_GEOMETRY)
    except (TypeError, ValueError, RuntimeError):
        window.setGeometry(*DEFAULT_GEOMETRY)

    try:
        sizes = _coerce_splitter_sizes(
            settings.value(HORIZONTAL_SPLITTER_SIZES_KEY)
        )
        if sizes is not None:
            splitter.setSizes(list(sizes))
            return sizes
    except (TypeError, ValueError, RuntimeError):
        splitter.setSizes(list(DEFAULT_SPLITTER_SIZES))
    return DEFAULT_SPLITTER_SIZES


def save_main_window_state(
    window: QWidget, splitter: QSplitter, settings: QSettings
) -> None:
    """Persist only geometry and the horizontal splitter sizes."""
    settings.setValue(GEOMETRY_KEY, window.saveGeometry())
    sizes = _coerce_splitter_sizes(splitter.sizes())
    settings.setValue(
        HORIZONTAL_SPLITTER_SIZES_KEY,
        list(sizes or DEFAULT_SPLITTER_SIZES),
    )
    settings.sync()


def _coerce_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return default


def restore_collapsible_panel_state(groups: dict, settings: QSettings) -> None:
    """Restore CAN/GPS expansion only; invalid or absent values stay collapsed."""
    for title, key in (
        ("CAN通道配置", CAN_PANEL_EXPANDED_KEY),
        ("GPS轨迹图", GPS_PANEL_EXPANDED_KEY),
    ):
        group = groups.get(title)
        if group is not None:
            group.setChecked(_coerce_bool(settings.value(key), False))


def save_collapsible_panel_state(groups: dict, settings: QSettings) -> None:
    """Persist only the two main-window collapsible panel states."""
    for title, key in (
        ("CAN通道配置", CAN_PANEL_EXPANDED_KEY),
        ("GPS轨迹图", GPS_PANEL_EXPANDED_KEY),
    ):
        group = groups.get(title)
        if group is not None:
            settings.setValue(key, bool(group.isChecked()))
    settings.sync()


__all__ = [
    "APPLICATION_NAME",
    "CAN_PANEL_EXPANDED_KEY",
    "DEFAULT_GEOMETRY",
    "DEFAULT_SPLITTER_SIZES",
    "GEOMETRY_KEY",
    "GPS_PANEL_EXPANDED_KEY",
    "HORIZONTAL_SPLITTER_SIZES_KEY",
    "MIN_LEFT_WIDTH",
    "MIN_RIGHT_WIDTH",
    "ORGANIZATION_NAME",
    "create_gui_settings",
    "restore_main_window_state",
    "restore_collapsible_panel_state",
    "save_main_window_state",
    "save_collapsible_panel_state",
]
