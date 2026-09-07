"""Per-editor text snapshots for the current client session; no disk storage."""
from dataclasses import dataclass

from PyQt5 import QtCore, QtGui, QtWidgets


@dataclass(frozen=True)
class TextSnapshot:
    text: str
    source: str


class PromptHistory(QtCore.QObject):
    """Navigate complete prompts independently of QTextDocument's edit undo."""

    def __init__(self, editor, back_button, forward_button, entries=None):
        super().__init__(editor)
        self.editor = editor
        self.back_button, self.forward_button = back_button, forward_button
        self.entries = entries if entries is not None else []
        if not self.entries:
            self.entries.append(TextSnapshot(editor.toPlainText(), 'initial'))
        self.index = len(self.entries) - 1
        self.closed = False
        editor._prompt_history = self
        editor.textChanged.connect(self.update_buttons)
        back_button.clicked.connect(self.back)
        forward_button.clicked.connect(self.forward)
        back_button.setToolTip('回退到上一个运行、翻译或扩写文本（本次会话）')
        forward_button.setToolTip('前进到下一个文本快照（本次会话）')
        self.update_buttons()

    def record(self, text=None, source='edit', *, force=False):
        if self.closed:
            return
        text = self.editor.toPlainText() if text is None else text
        if (not force and self.entries and self.index == len(self.entries) - 1
                and self.entries[self.index].text == text):
            return
        # Preserve earlier run snapshots even after navigating back and editing.
        self.entries.append(TextSnapshot(text, source))
        self.index = len(self.entries) - 1
        self.update_buttons()

    def record_run(self):
        self.record(source='run', force=True)

    def apply_result(self, text, source):
        if self.closed or not isinstance(text, str) or not text:
            return
        self.record()  # Keep any draft typed while the remote request ran.
        self._replace(text)
        self.record(text, source)

    def _replace(self, text):
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.select(QtGui.QTextCursor.Document)
        cursor.insertText(text)
        cursor.endEditBlock()
        self.editor.setTextCursor(cursor)
        hide = getattr(self.editor, '_hide_popup', None)
        if hide:
            hide()

    def back(self):
        if self.closed or not self.entries:
            return
        current = self.editor.toPlainText()
        if current != self.entries[self.index].text:
            # Save the current draft before leaving it; Forward can recover it.
            self.entries.append(TextSnapshot(current, 'draft'))
        elif self.index > 0:
            self.index -= 1
        else:
            return
        self._replace(self.entries[self.index].text)
        self.update_buttons()

    def forward(self):
        if self.closed or self.index >= len(self.entries) - 1:
            return
        current = self.editor.toPlainText()
        if current != self.entries[self.index].text:
            self.entries.append(TextSnapshot(current, 'draft'))
        self.index += 1
        self._replace(self.entries[self.index].text)
        self.update_buttons()

    def update_buttons(self):
        active = not self.closed and bool(self.entries)
        changed = active and self.editor.toPlainText() != self.entries[self.index].text
        self.back_button.setEnabled(bool(active and (changed or self.index > 0)))
        self.forward_button.setEnabled(bool(active and self.index < len(self.entries) - 1))

    def close(self):
        self.closed = True
        self.entries.clear()
        self.index = -1
        self.update_buttons()


def record_run_inputs(node_widgets):
    for fields in node_widgets.values():
        editor = fields.get('te')
        history = getattr(editor, '_prompt_history', None)
        if history is not None:
            history.record_run()


def input_history_entries(window, app_id, node, index):
    """Keep only immutable text in the session store, never cached page widgets."""
    if not hasattr(window, '_prompt_history_entries'):
        window._prompt_history_entries = {}
    identity = (str(app_id), str(node.get('nodeId', index)), str(node.get('fieldName', index)))
    return window._prompt_history_entries.setdefault(identity, [])


def clear_histories(window):
    for editor in window.findChildren(QtWidgets.QTextEdit):
        history = getattr(editor, '_prompt_history', None)
        if history is not None:
            history.close()
    store = getattr(window, '_prompt_history_entries', {})
    for entries in store.values():
        entries.clear()
    store.clear()
