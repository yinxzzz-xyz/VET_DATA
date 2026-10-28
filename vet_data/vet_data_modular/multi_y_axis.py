"""Manage per-signal ViewBoxes and Y axes for one shared plotting area."""

from dataclasses import dataclass
import math
from typing import Hashable, Mapping

from PyQt6.QtCore import QSignalBlocker, Qt
import pyqtgraph as pg


@dataclass(frozen=True)
class SignalYAxisState:
    """Persistable UI state for one signal's independent Y axis."""

    y_range: tuple[float, float]
    axis_visible: bool = False


class MultiYAxisManager:
    """Own per-signal ViewBoxes linked to one reference ViewBox's X axis.

    The reference ViewBox supplies the shared X range and plot geometry. Curves
    should be added to the ViewBox returned by :meth:`register_signal` by the
    caller. ``signal_key`` must be the stable signal identifier, never a row or
    curve index.
    """

    def __init__(self, reference_view_box, scene, initial_state=None,
                 plot_item=None, plot_widget=None, scroll_bar=None,
                 initial_scroll_position=0):
        if reference_view_box is None:
            raise ValueError("reference_view_box is required")
        if scene is None:
            raise ValueError("scene is required")

        self._reference_view_box = reference_view_box
        self._scene = scene
        self._plot_item = plot_item
        self._plot_widget = plot_widget
        self._scroll_bar = scroll_bar
        self._view_boxes = {}
        self._axis_items = {}
        self._signal_key_by_view_id = {}
        self._states = {}
        self._current_signal_key = None
        self._closed = False
        self._geometry_sync_count = 0
        self._axis_layout_installed = False
        self._displayed_axis_keys = set()
        self._layout_in_progress = False
        self._axis_width = 52
        self._scroll_position = max(0, int(initial_scroll_position))
        self._layout_slot_count = None
        self._layout_displayed_keys = ()
        self._base_layout_items = None

        for signal_key, state in (initial_state or {}).items():
            self._validate_key(signal_key)
            self._states[signal_key] = self._coerce_state(state)

        self._reference_view_box.sigResized.connect(self._sync_geometry)
        if self._scroll_bar is not None:
            self._scroll_bar.valueChanged.connect(self._scroll_bar_changed)

    @staticmethod
    def _validate_key(signal_key):
        try:
            hash(signal_key)
        except TypeError as exc:
            raise TypeError("signal_key must be a stable, hashable identifier") from exc

    @staticmethod
    def _validate_range(y_range):
        try:
            low, high = (float(value) for value in y_range)
        except (TypeError, ValueError) as exc:
            raise ValueError("y_range must contain two finite numbers") from exc
        if not math.isfinite(low) or not math.isfinite(high) or low >= high:
            raise ValueError("y_range must be finite and strictly increasing")
        return low, high

    @classmethod
    def _coerce_state(cls, state):
        if isinstance(state, SignalYAxisState):
            return SignalYAxisState(
                cls._validate_range(state.y_range), bool(state.axis_visible)
            )
        if isinstance(state, Mapping):
            return SignalYAxisState(
                cls._validate_range(state["y_range"]),
                bool(state.get("axis_visible", False)),
            )
        raise TypeError("initial state values must be SignalYAxisState or mappings")

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("MultiYAxisManager is closed")

    def register_signal(self, signal_key, y_range, axis_item=None, axis_visible=None,
                        axis_color=None):
        """Create and return a signal ViewBox, restoring saved state by key."""
        self._ensure_open()
        self._validate_key(signal_key)
        if signal_key in self._view_boxes:
            raise ValueError(f"signal is already registered: {signal_key!r}")

        requested_range = self._validate_range(y_range)
        state = self._states.get(signal_key)
        if state is None:
            state = SignalYAxisState(
                requested_range,
                False if axis_visible is None else bool(axis_visible),
            )
        elif axis_visible is not None:
            state = SignalYAxisState(state.y_range, state.axis_visible)

        view_box = pg.ViewBox()
        # Keep the reference ViewBox on top for the existing cursor/drag
        # interactions; the transparent managed views only supply mapping.
        view_box.setZValue(-1)
        # Preserve the existing plot-area X wheel zoom; only Y interaction is
        # delegated to the signal-specific axes in a later UI stage.
        view_box.setMouseEnabled(x=True, y=False)
        view_box.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._scene.addItem(view_box)
        view_box.setXLink(self._reference_view_box)
        view_box.setYRange(*state.y_range, padding=0)
        view_box.sigYRangeChanged.connect(self._cache_live_y_range)

        self._view_boxes[signal_key] = view_box
        self._signal_key_by_view_id[id(view_box)] = signal_key
        self._states[signal_key] = state
        if axis_item is None and self._plot_item is not None:
            axis_item = pg.AxisItem(orientation="left")
        if axis_item is not None:
            if axis_color is not None:
                axis_item.setTextPen(axis_color)
                axis_item.setPen(axis_color)
                axis_item.setTickPen(axis_color)
            axis_item.linkToView(view_box)
            axis_item.setVisible(state.axis_visible)
            self._axis_items[signal_key] = axis_item
        self._sync_one_geometry(view_box)
        if self._axis_layout_installed:
            self._layout_axes()
        return view_box

    def install_axis_layout(self):
        """Place signal axes in PlotItem's left grid columns, aligned to its ViewBox."""
        if self._plot_item is None:
            raise RuntimeError("plot_item is required to install the axis layout")
        if self._axis_layout_installed:
            return
        layout = self._plot_item.layout
        known_positions = {
            self._plot_item.vb: (2, 1, 1, 1),
            self._plot_item.titleLabel: (0, 1, 1, 1),
        }
        for axis_data in self._plot_item.axes.values():
            axis = axis_data["item"]
            row, column = axis_data["pos"]
            known_positions[axis] = (row, column, 1, 1)
        old_items = []
        default_left_axis = self._plot_item.getAxis("left")
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item is default_left_axis:
                continue
            if item not in known_positions:
                raise RuntimeError(
                    f"Cannot safely reposition PlotItem child {type(item).__name__}"
                )
            row, column, row_span, column_span = known_positions[item]
            old_items.append((item, row, column, row_span, column_span))
        for index in reversed(range(layout.count())):
            item = layout.itemAt(index)
            layout.removeItem(item)
        default_left_axis.setVisible(False)
        default_left_axis.setWidth(0)
        self._base_layout_items = old_items
        self._axis_layout_installed = True
        self._layout_axes()

    @property
    def visible_axis_keys(self):
        """Axes the user has chosen to keep visible, independent of scrolling."""
        return tuple(key for key in self._view_boxes if self._states[key].axis_visible)

    @property
    def scroll_position(self):
        return self._scroll_position

    @property
    def axis_capacity(self):
        width = max(1, self._plot_widget.width() if self._plot_widget else 800)
        return max(1, int((width * 0.5) // self._axis_width))

    @property
    def scroll_bar(self):
        return self._scroll_bar

    def _scroll_bar_changed(self, value):
        if self._layout_in_progress:
            return
        self._scroll_position = max(0, int(value))
        self._layout_axes()

    def _set_scroll_bar_state(self, visible_count, capacity):
        if self._scroll_bar is None:
            return
        maximum = max(0, visible_count - capacity)
        self._scroll_position = min(self._scroll_position, maximum)
        blocker = QSignalBlocker(self._scroll_bar)
        self._scroll_bar.setPageStep(capacity)
        self._scroll_bar.setSingleStep(1)
        self._scroll_bar.setRange(0, maximum)
        self._scroll_bar.setValue(self._scroll_position)
        width = max(1, min(
            int(self._plot_widget.width() * 0.5), capacity * self._axis_width
        ))
        self._scroll_bar.setMaximumWidth(width)
        self._scroll_bar.setFixedWidth(width)
        self._scroll_bar.setVisible(visible_count > capacity)
        del blocker

    def _ensure_signal_in_scroll_window(self, signal_key, visible_keys, capacity):
        if signal_key not in visible_keys:
            return
        index = visible_keys.index(signal_key)
        if index < self._scroll_position:
            self._scroll_position = index
        elif index >= self._scroll_position + capacity:
            self._scroll_position = index - capacity + 1

    def _rebuild_axis_grid(self, displayed, slot_count):
        layout = self._plot_item.layout
        for index in reversed(range(layout.count())):
            layout.removeItem(layout.itemAt(index))
        shift = max(0, slot_count - 1)
        for item, row, column, row_span, column_span in self._base_layout_items:
            layout.addItem(item, row, column + shift, row_span, column_span)
        for column, signal_key in enumerate(displayed):
            layout.addItem(self._axis_items[signal_key], 2, column)
        layout.setColumnStretchFactor(slot_count, 100)
        layout.activate()
        self._layout_slot_count = slot_count
        self._layout_displayed_keys = tuple(displayed)
        self._sync_geometry()

    def _layout_axes(self):
        if (not self._axis_layout_installed or self._closed
                or self._layout_in_progress):
            return
        self._layout_in_progress = True
        try:
            visible = list(self.visible_axis_keys)
            capacity = self.axis_capacity
            self._scroll_position = min(
                self._scroll_position, max(0, len(visible) - capacity)
            )
            displayed = visible[self._scroll_position:self._scroll_position + capacity]
            self._displayed_axis_keys = set(displayed)
            self._set_scroll_bar_state(len(visible), capacity)
            slot_count = max(1, len(displayed))
            available_width = max(
                1, int((self._plot_widget.width() if self._plot_widget else 800) * 0.5)
            )
            per_axis_width = max(
                1,
                min(self._axis_width,
                    available_width // max(1, slot_count)),
            )
            for key, axis_item in self._axis_items.items():
                shown = key in self._displayed_axis_keys
                if shown:
                    if axis_item.fixedWidth != per_axis_width:
                        axis_item.setWidth(per_axis_width)
                    if not axis_item.isVisible():
                        axis_item.setVisible(True)
                else:
                    if axis_item.isVisible():
                        axis_item.setVisible(False)
                    if axis_item.fixedWidth != 0:
                        axis_item.setWidth(0)
            if (self._layout_slot_count != slot_count
                    or tuple(displayed) != self._layout_displayed_keys):
                self._rebuild_axis_grid(displayed, slot_count)
            else:
                self._plot_item.layout.activate()
        finally:
            self._layout_in_progress = False

    @property
    def displayed_axis_keys(self):
        """Signal axes in the current viewport of the axis strip."""
        return tuple(getattr(self, "_layout_displayed_keys", ()))

    def _cache_live_y_range(self, view_box, y_range):
        signal_key = self._signal_key_by_view_id.get(id(view_box))
        if signal_key is None or signal_key not in self._states:
            return
        y_range = self._validate_range(y_range)
        state = self._states[signal_key]
        self._states[signal_key] = SignalYAxisState(y_range, state.axis_visible)
        self._layout_axes()

    def _sync_one_geometry(self, view_box):
        view_box.setGeometry(self._reference_view_box.sceneBoundingRect())
        view_box.linkedViewChanged(self._reference_view_box, pg.ViewBox.XAxis)

    def _sync_geometry(self, *_args):
        if self._closed:
            return
        self._geometry_sync_count += 1
        for view_box in tuple(self._view_boxes.values()):
            self._sync_one_geometry(view_box)
        self._layout_axes()

    def view_box(self, signal_key):
        return self._view_boxes[signal_key]

    def axis_item(self, signal_key):
        return self._axis_items.get(signal_key)

    @property
    def signal_keys(self):
        return tuple(self._view_boxes)

    def y_range(self, signal_key):
        view_box = self._view_boxes.get(signal_key)
        if view_box is not None:
            return self._validate_range(view_box.viewRange()[1])
        return self._states[signal_key].y_range

    def set_y_range(self, signal_key, y_range):
        y_range = self._validate_range(y_range)
        view_box = self._view_boxes[signal_key]
        view_box.setYRange(*y_range, padding=0)
        state = self._states[signal_key]
        self._states[signal_key] = SignalYAxisState(y_range, state.axis_visible)
        self._layout_axes()

    def set_axis_visible(self, signal_key, visible):
        state = self._states[signal_key]
        updated = SignalYAxisState(state.y_range, bool(visible))
        self._states[signal_key] = updated
        axis_item = self._axis_items.get(signal_key)
        if self._axis_layout_installed:
            if updated.axis_visible and self._current_signal_key in self.visible_axis_keys:
                self._ensure_signal_in_scroll_window(
                    self._current_signal_key,
                    list(self.visible_axis_keys),
                    self.axis_capacity,
                )
            self._layout_axes()
        elif axis_item is not None:
            axis_item.setVisible(updated.axis_visible)

    def axis_visible(self, signal_key):
        return self._states[signal_key].axis_visible

    @property
    def current_signal_key(self):
        return self._current_signal_key

    def set_current_signal(self, signal_key):
        """Select the operation target without hiding other visible axes."""
        if signal_key not in self._view_boxes:
            raise KeyError(signal_key)
        self._current_signal_key = signal_key
        self.set_axis_visible(signal_key, True)
        self._ensure_signal_in_scroll_window(
            signal_key, list(self.visible_axis_keys), self.axis_capacity
        )
        self._layout_axes()

    def snapshot_state(self):
        """Return Y ranges and axis visibility keyed by stable signal ID."""
        snapshot = dict(self._states)
        for signal_key, view_box in self._view_boxes.items():
            state = snapshot[signal_key]
            snapshot[signal_key] = SignalYAxisState(
                self._validate_range(view_box.viewRange()[1]), state.axis_visible
            )
        return snapshot

    def remove_signal(self, signal_key, *, preserve_state=True):
        """Detach a signal's graphics objects, optionally retaining its state."""
        view_box = self._view_boxes.pop(signal_key, None)
        axis_item = self._axis_items.pop(signal_key, None)
        if view_box is None:
            if not preserve_state:
                self._states.pop(signal_key, None)
                if self._current_signal_key == signal_key:
                    self._current_signal_key = None
            return False

        state = self._states.get(signal_key)
        if state is not None and preserve_state:
            self._states[signal_key] = SignalYAxisState(
                self._validate_range(view_box.viewRange()[1]), state.axis_visible
            )
        elif not preserve_state:
            self._states.pop(signal_key, None)
            if self._current_signal_key == signal_key:
                self._current_signal_key = None

        try:
            view_box.sigYRangeChanged.disconnect(self._cache_live_y_range)
        except (TypeError, RuntimeError):
            pass
        view_box.setXLink(None)
        if axis_item is not None:
            axis_item.unlinkFromView()
            axis_item.setVisible(False)
        self._signal_key_by_view_id.pop(id(view_box), None)
        self._scene.removeItem(view_box)
        view_box.deleteLater()
        if self._axis_layout_installed:
            self._layout_slot_count = None
        self._layout_axes()
        if axis_item is not None:
            if axis_item.scene() is not None:
                axis_item.scene().removeItem(axis_item)
            axis_item.deleteLater()
        return True

    def close(self, *, preserve_state=True):
        """Release all live views and stop listening to the reference view."""
        if self._closed:
            return self.snapshot_state()
        for signal_key in tuple(self._view_boxes):
            self.remove_signal(signal_key, preserve_state=preserve_state)
        try:
            self._reference_view_box.sigResized.disconnect(self._sync_geometry)
        except (TypeError, RuntimeError):
            pass
        if self._scroll_bar is not None:
            try:
                self._scroll_bar.valueChanged.disconnect(self._scroll_bar_changed)
            except (TypeError, RuntimeError):
                pass
        if not preserve_state:
            self._states.clear()
            self._current_signal_key = None
        self._closed = True
        return self.snapshot_state()
