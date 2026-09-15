"""Collapsible CAN configuration and GPS sections."""

from PyQt6.QtWidgets import QGroupBox


class CollapsiblePanelsMixin:
    def _setup_collapsible_panels(self):
        for group in self.findChildren(QGroupBox):
            if group.title() not in {"CAN通道配置", "CAN总线配置", "GPS轨迹图"}: continue
            group.setCheckable(True); group.setChecked(True)
            group.toggled.connect(lambda checked, box=group: self._toggle_group_contents(box, checked))

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
