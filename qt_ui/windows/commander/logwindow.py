"""Window with a commander's live planning log and its journal."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from game.claudecommander.coordinator import commander_journal
from game.theater.player import Player
from qt_ui.windows.commander.bridge import CommanderBridge, side_label
from qt_ui.windows.commander.logformat import LogKind, LogLine, line_html

#: Returns the Markdown of a side's journal.
JournalSource = Callable[[Player], str]

EMPTY_JOURNAL = "*The commander has not written a journal yet.*"


class CommanderLogWindow(QDialog):
    def __init__(
        self,
        bridge: CommanderBridge,
        player: Player,
        journal_source: Optional[JournalSource] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.player = player
        self.journal_source = journal_source or self._read_journal
        self._placeholder = True

        self.setWindowTitle(f"{side_label(player)} commander")
        self.setMinimumSize(720, 520)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Live tab.
        live = QWidget()
        live_layout = QVBoxLayout(live)
        controls = QHBoxLayout()
        self.show_thinking = QCheckBox("Show thinking")
        self.show_thinking.toggled.connect(self.render_live)
        controls.addWidget(self.show_thinking)
        self.follow = QCheckBox("Follow")
        self.follow.setChecked(True)
        controls.addWidget(self.follow)
        controls.addStretch(1)
        live_layout.addLayout(controls)
        self.live_view = QTextBrowser()
        self.live_view.setOpenExternalLinks(False)
        live_layout.addWidget(self.live_view)
        self.tabs.addTab(live, "Live")

        # Journal tab.
        journal = QWidget()
        journal_layout = QVBoxLayout(journal)
        journal_controls = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_journal)
        journal_controls.addWidget(self.refresh_button)
        journal_controls.addStretch(1)
        journal_layout.addLayout(journal_controls)
        self.journal_view = QTextBrowser()
        journal_layout.addWidget(self.journal_view)
        self.tabs.addTab(journal, "Journal")

        bridge.line_added.connect(self.on_line_added)
        self.render_live()
        self.refresh_journal()

    def _read_journal(self, player: Player) -> str:
        game = self.bridge.game
        if game is None:
            return ""
        return commander_journal(game, player).render()

    def _visible(self, line: LogLine) -> bool:
        return line.kind is not LogKind.THINKING or self.show_thinking.isChecked()

    def render_live(self) -> None:
        lines = [l for l in self.bridge.log(self.player).lines if self._visible(l)]
        self._placeholder = not lines
        if not lines:
            self.live_view.setHtml(
                "<p><i>Nothing yet. Planning progress appears here while the "
                "commander works.</i></p>"
            )
            return
        self.live_view.setHtml("".join(line_html(l) for l in lines))
        self._scroll_to_end()

    def on_line_added(self, player: Player, line: LogLine) -> None:
        if player is not self.player or not self._visible(line):
            return
        if self._placeholder:
            self.render_live()
            return
        self.live_view.append(line_html(line))
        self._scroll_to_end()

    def _scroll_to_end(self) -> None:
        if self.follow.isChecked():
            self.live_view.moveCursor(QTextCursor.MoveOperation.End)
            self.live_view.ensureCursorVisible()

    def refresh_journal(self) -> None:
        try:
            text = self.journal_source(self.player)
        except OSError as ex:
            text = f"*Could not read the journal: {ex}*"
        self.journal_view.setMarkdown(text.strip() or EMPTY_JOURNAL)

    def live_text(self) -> str:
        return self.live_view.toPlainText()

    def journal_text(self) -> str:
        return self.journal_view.toPlainText()

    def closeEvent(self, event: QCloseEvent) -> None:
        try:
            self.bridge.line_added.disconnect(self.on_line_added)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)
