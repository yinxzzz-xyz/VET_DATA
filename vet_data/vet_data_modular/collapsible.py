"""Compatibility hook for legacy collapsible panel setup."""

from PyQt6.QtWidgets import QGroupBox


class CollapsiblePanelsMixin:
    def _setup_collapsible_panels(self):
        # CAN is a permanent left-panel tab; the legacy checkable groupbox
        # used to hide the whole configuration card and is no longer needed.
        self.collapsible_groups = {}
