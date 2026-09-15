"""Search-enabled custom calculation channel dialog."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCompleter

from .legacy import baseline


class SearchableMathChannelDialog(baseline.MathChannelDialog):
    def init_ui(self):
        super().init_ui()
        for combo in (self.combo_a, self.combo_b):
            combo.setEditable(True)
            values = [combo.itemText(i) for i in range(combo.count())]
            completer = QCompleter(values, combo)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            combo.setCompleter(completer)


# Baseline methods resolve this class through their defining module globals.
baseline.MathChannelDialog = SearchableMathChannelDialog
