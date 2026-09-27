"""Collapsible CAN configuration and GPS sections."""

from PyQt6.QtWidgets import QGroupBox, QSizePolicy


class CollapsiblePanelsMixin:
    def _setup_collapsible_panels(self):
        self.collapsible_groups = {}
        for group in self.findChildren(QGroupBox):
            if group.title() not in {"CAN通道配置", "CAN总线配置", "GPS轨迹图"}: continue
            self.collapsible_groups[group.title()] = group
            group.setCheckable(True)
            group.toggled.connect(lambda checked, box=group: self._toggle_group_contents(box, checked))
            group.setChecked(False)
            self._toggle_group_contents(group, False)

    @staticmethod
    def _toggle_group_contents(group, visible):
        layout = group.layout()
        if layout is None: return
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.widget() is not None: item.widget().setVisible(visible)
            child = item.layout()
            if child is not None:
                for sub_index in range(child.count()):
                    if child.itemAt(sub_index).widget() is not None:
                        child.itemAt(sub_index).widget().setVisible(visible)
        if visible:
            group.setMinimumHeight(0)
            group.setMaximumHeight(16777215)
            group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        else:
            collapsed_height = group.fontMetrics().height() + 18
            group.setMinimumHeight(collapsed_height)
            group.setMaximumHeight(collapsed_height)
            group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        group.updateGeometry()
        parent = group.parentWidget()
        if parent is not None:
            parent.updateGeometry()
