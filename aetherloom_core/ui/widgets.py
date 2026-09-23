"""Reusable prompt, drag/drop, slider, and thumbnail widgets."""
import re
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt
from aetherloom_core import autocomplete as auto_complete
from aetherloom_core.paths import current_dir

class CompletionTextEdit(QtWidgets.QTextEdit):
    """QTextEdit 扩展：内置基于 `auto_complete` 的弹出补全列表。"""
    uses_local_completion_service = True
    prompt_weight_syntax = 'sd'

    def __init__(self, parent=None, current_dir=None, limit=50):
        super().__init__(parent)
        self.setCursorWidth(4)
        self.setToolTip('选中文字后按 Ctrl＋↑ / Ctrl＋↓ 调整提示词权重，每次 0.05。')
        try:
            # manager will load autocomplete.txt from current_dir
            self._manager = auto_complete.get_manager(current_dir or globals().get('current_dir'))
        except Exception:
            self._manager = None
        self._limit = limit or 20
        self._popup = auto_complete.AutocompletePopup(self)
        self._popup.setUniformItemSizes(False)
        self._popup_cursor_position = None
        self._popup_context = None
        self._manual_selection_context = None
        self._ime_composing = False
        self._completion_editing = False
        self._completion_revision = 0
        self._completion_timer = QtCore.QTimer(self)
        self._completion_timer.setSingleShot(True)
        self._completion_timer.timeout.connect(self._request_completion)
        self._popup.dismissed.connect(self._hide_popup)
        self._popup.itemClicked.connect(self._on_item_clicked)
        self.textChanged.connect(self._on_text_changed)
        self.cursorPositionChanged.connect(self._on_cursor_position_changed)
        self.selectionChanged.connect(self._on_cursor_position_changed)
        self.verticalScrollBar().valueChanged.connect(self._hide_popup)
        self.horizontalScrollBar().valueChanged.connect(self._hide_popup)
        self._focus_out_timer = QtCore.QTimer(self)
        self._focus_out_timer.setSingleShot(True)
        self._focus_out_timer.setInterval(120)
        self._focus_out_timer.timeout.connect(self._hide_popup_if_unfocused)

    def insertFromMimeData(self, source):
        if source.hasText():
            # source.text() 返回的是去除格式的纯文本
            self.insertPlainText(source.text())
        else:
            super().insertFromMimeData(source)

    def createStandardContextMenu(self, *args):
        menu = super().createStandardContextMenu(*args)
        menu.addSeparator()
        enabled = self._can_adjust_prompt_weight()
        example = '1.05::文本::' if self.prompt_weight_syntax == 'nai' else '(文本:1.05)'
        for label, shortcut, direction in (('提高提示词权重', 'Ctrl+Up', 1),
                                            ('降低提示词权重', 'Ctrl+Down', -1)):
            action = menu.addAction(label)
            action.setShortcut(QtGui.QKeySequence(shortcut))
            action.setEnabled(enabled)
            action.setToolTip('调整所选文本的权重，每次 0.05；格式：' + example)
            action.triggered.connect(lambda _checked=False, delta=direction: self.adjust_prompt_weight(delta))
        from aetherloom_core.selection_text_tools import add_actions
        add_actions(self, menu)
        return menu

    def _can_adjust_prompt_weight(self):
        return (self.isEnabled() and not self.isReadOnly() and self.textCursor().hasSelection()
                and bool(self.textCursor().selectedText().strip()))

    @staticmethod
    def _is_weight_shortcut(event):
        modifiers = event.modifiers() & (QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier |
                                         QtCore.Qt.AltModifier | QtCore.Qt.MetaModifier)
        return (modifiers == QtCore.Qt.ControlModifier and
                event.key() in (QtCore.Qt.Key_Up, QtCore.Qt.Key_Down))

    def event(self, event):
        if hasattr(self, '_popup'):
            if event.type() in (QtCore.QEvent.ReadOnlyChange, QtCore.QEvent.EnabledChange):
                if self.isReadOnly() or not self.isEnabled():
                    self._cancel_suggestions()
            if (event.type() == QtCore.QEvent.ShortcutOverride and not self._ime_composing
                    and self.isEnabled() and not self.isReadOnly()):
                modifiers = event.modifiers() & (QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier |
                                                 QtCore.Qt.AltModifier | QtCore.Qt.MetaModifier)
                manual = event.key() == QtCore.Qt.Key_Space and modifiers == QtCore.Qt.ControlModifier
                candidate = (not modifiers and self._popup.isVisible() and self._popup_context_valid()
                             and event.key() in (QtCore.Qt.Key_Tab, QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter,
                                                 QtCore.Qt.Key_Up, QtCore.Qt.Key_Down, QtCore.Qt.Key_Escape)
                             and (self._popup.count() or event.key() == QtCore.Qt.Key_Escape))
                if manual or candidate:
                    event.accept()
                    return True
        # A selected prompt takes precedence over custom window/canvas actions.
        if (event.type() == QtCore.QEvent.ShortcutOverride and self._is_weight_shortcut(event)
                and self._can_adjust_prompt_weight()):
            event.accept()
            return True
        return super().event(event)

    def inputMethodEvent(self, event):
        self._ime_composing = bool(event.preeditString())
        self._cancel_suggestions()
        self._completion_editing = True
        try:
            super().inputMethodEvent(event)
        finally:
            self._completion_editing = False
        if not self._ime_composing:
            self._on_text_changed()

    def setDocument(self, document):
        if hasattr(self, '_popup'):
            self._cancel_suggestions()
        super().setDocument(document)

    def adjust_prompt_weight(self, direction):
        if not self._can_adjust_prompt_weight():
            return False
        from aetherloom_core.prompt_weights import adjust_weight
        cursor = self.textCursor()
        reverse = cursor.position() < cursor.anchor()
        text = self.toPlainText()
        before = QtGui.QTextCursor(cursor)
        before.setPosition(0)
        before.setPosition(cursor.selectionStart(), QtGui.QTextCursor.KeepAnchor)
        start = len(before.selectedText())
        before.setPosition(0)
        before.setPosition(cursor.selectionEnd(), QtGui.QTextCursor.KeepAnchor)
        end = len(before.selectedText())
        edit = adjust_weight(text, start, end, direction, syntax=self.prompt_weight_syntax)
        self._cancel_suggestions()
        if edit is None:
            QtWidgets.QToolTip.showText(
                self._completion_global_point(self.cursorRect().bottomLeft()),
                '当前选区无法安全调整权重，请分别选择完整的权重组或其中的正文。',
                self, QtCore.QRect(), 2500)
            return False
        begin = len(text[:edit.start].encode('utf-16-le')) // 2
        finish = len(text[:edit.end].encode('utf-16-le')) // 2
        selection_start = begin + len(edit.replacement[:edit.selection_start].encode('utf-16-le')) // 2
        selection_end = begin + len(edit.replacement[:edit.selection_end].encode('utf-16-le')) // 2
        cursor.beginEditBlock()
        cursor.setPosition(begin)
        cursor.setPosition(finish, QtGui.QTextCursor.KeepAnchor)
        cursor.insertText(edit.replacement)
        cursor.setPosition(selection_end if reverse else selection_start)
        cursor.setPosition(selection_start if reverse else selection_end, QtGui.QTextCursor.KeepAnchor)
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self._cancel_suggestions()
        return True

    def contextMenuEvent(self, event):
        self._cancel_suggestions()
        menu = self.createStandardContextMenu(event.pos())
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(event.globalPos())
        event.accept()

    def _completion_options(self):
        # Editors are initially parentless, then attached to cached RH pages.
        # Resolve the live owning window so both existing and new pages update.
        owner = self.parentWidget()
        while owner is not None:
            settings = getattr(owner, 'settings', None)
            if isinstance(settings, dict):
                return auto_complete.completion_options(settings)
            owner = owner.parentWidget()
        return auto_complete.completion_options()

    def _completion_token(self):
        from aetherloom_core.prompt_tokens import TokenSpan, completion_token
        cur = self.textCursor()
        if cur.hasSelection():
            if self._manual_selection_context == self._completion_context():
                before = QtGui.QTextCursor(cur)
                before.setPosition(0)
                before.setPosition(cur.selectionStart(), QtGui.QTextCursor.KeepAnchor)
                start = len(before.selectedText())
                selected = cur.selectedText()
                if selected.strip() and not any(c in selected for c in ',，\r\n\u2028\u2029'):
                    return TokenSpan(start, start + len(selected), selected.strip())
            return None
        cur.movePosition(QtGui.QTextCursor.Start, QtGui.QTextCursor.KeepAnchor)
        return completion_token(self.toPlainText(), len(cur.selectedText()), syntax=self.prompt_weight_syntax)

    def _completion_context(self):
        from PyQt5 import sip
        cursor = self.textCursor()
        return (int(sip.unwrapinstance(self.document())), self.document().revision(),
                cursor.position(), cursor.anchor())

    def _prepare_manual_completion(self):
        self._manual_selection_context = self._completion_context() if self.textCursor().hasSelection() else None

    def _popup_context_valid(self):
        return self._popup_context is not None and self._popup_context == self._completion_context()

    def _get_prefix_before_cursor(self):
        token = self._completion_token()
        return token.query if token else ''

    def _hide_popup(self, *args):
        self._completion_revision += 1
        self._completion_timer.stop()
        self._popup.hide()
        self._popup_cursor_position = None
        self._popup_context = None

    def _cancel_suggestions(self):
        self._manual_selection_context = None
        self._hide_popup()

    def eventFilter(self, receiver, event):
        # Listen while the editor is focused, including the time before the
        # asynchronous popup appears. A no-focus outside click must still
        # cancel a pending query rather than letting it reopen the popup.
        if self.hasFocus():
            kind = event.type()
            if (kind == QtCore.QEvent.KeyPress and event.key() == QtCore.Qt.Key_Escape
                    and not self._ime_composing):
                visible = self._popup.isVisible()
                self._cancel_suggestions()
                if visible:
                    event.accept()
                    return True
            elif kind in (QtCore.QEvent.ApplicationDeactivate,):
                self._cancel_suggestions()
            elif kind in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonDblClick,
                          QtCore.QEvent.NonClientAreaMouseButtonPress,
                          QtCore.QEvent.NonClientAreaMouseButtonDblClick,
                          QtCore.QEvent.TouchBegin, QtCore.QEvent.Wheel):
                in_editor = receiver is self or isinstance(receiver, QtWidgets.QWidget) and self.isAncestorOf(receiver)
                in_popup = (receiver is self._popup or isinstance(receiver, QtWidgets.QWidget)
                            and self._popup.isAncestorOf(receiver))
                if not in_editor and not in_popup:
                    self._cancel_suggestions()
            elif kind in (QtCore.QEvent.Move, QtCore.QEvent.Resize) and receiver is self.window():
                self._cancel_suggestions()
        return super().eventFilter(receiver, event)

    def _on_cursor_position_changed(self):
        if self._ime_composing or self._completion_editing:
            return
        cur = self.textCursor()
        if cur.hasSelection() or cur.position() != self._popup_cursor_position:
            self._hide_popup()
        if self.uses_local_completion_service:
            self._queue_completion()

    def _hide_popup_if_unfocused(self):
        if not self.hasFocus():
            self._hide_popup()

    def _on_text_changed(self):
        self._queue_completion()

    def _queue_completion(self):
        self._hide_popup()
        if (self.uses_local_completion_service and self.hasFocus() and not self.isReadOnly()
                and self.isEnabled() and self._manager and not self._ime_composing
                and not self._completion_editing):
            self._completion_timer.start(90)

    def _request_completion(self):
        if (not self.hasFocus() or self.isReadOnly() or not self.isEnabled()
                or self._ime_composing or self._completion_editing):
            return
        prefix = self._get_prefix_before_cursor()
        if not 1 <= len(prefix) <= 200 or not self._manager:
            return
        from aetherloom_core.completion_search import request_matches
        request_matches(self, prefix, max(self._limit, self._completion_options()['visible_tags']))

    def show_suggestions(self):
        if self.isReadOnly() or not self.isEnabled() or self._ime_composing:
            return
        self._prepare_manual_completion()
        self.setFocus(QtCore.Qt.OtherFocusReason)
        self._queue_completion()

    def _show_popup(self, matches):
        if (not matches or self.isReadOnly() or not self.isEnabled()
                or self._ime_composing or self._completion_editing):
            self._hide_popup()
            return
        self._popup.set_candidates(matches, self._get_prefix_before_cursor(),
                                   getattr(self.window(), '_theme_mode', 'dark'))
        self._popup.ensurePolished()

        # Cursor rectangles are relative to the viewport, not the editor frame.
        rect = self.cursorRect()
        below = self._completion_global_point(rect.bottomLeft()) + QtCore.QPoint(0, 1)
        above = self._completion_global_point(rect.topLeft())
        screen = QtWidgets.QApplication.screenAt(below) or QtWidgets.QApplication.primaryScreen()
        if screen is None:
            self._hide_popup()
            return
        available = screen.availableGeometry()
        width = min(max(self.width(), 360), 560, available.width())
        desired_height = self._popup.layout_height(width, self._completion_options()['visible_tags'])
        below_space = max(0, available.bottom() + 1 - below.y())
        above_space = max(0, above.y() - available.top())
        if below_space >= desired_height or below_space >= above_space:
            height = min(desired_height, max(1, below_space))
            y = below.y()
        else:
            height = min(desired_height, max(1, above_space))
            y = above.y() - height
        height = min(height, available.height())
        x = max(available.left(), min(below.x(), available.right() + 1 - width))
        y = max(available.top(), min(y, available.bottom() + 1 - height))
        self._popup.setFixedSize(width, height)
        self._popup.move(x, y)
        self._popup_cursor_position = self.textCursor().position()
        self._popup_context = self._completion_context()
        self._popup.show()

    def _completion_global_point(self, point):
        # Embedded canvas widgets need the view transform as well as the
        # widget offset. QWidget.mapToGlobal alone drops part of that scaling.
        owner = self
        while owner is not None:
            proxy = owner.graphicsProxyWidget()
            if proxy is not None and proxy.scene() is not None:
                views = [view for view in proxy.scene().views() if view.isVisible()]
                if views:
                    local = self.viewport().mapTo(owner, point)
                    scene_point = proxy.mapToScene(QtCore.QPointF(local))
                    view = views[0]
                    return view.viewport().mapToGlobal(view.mapFromScene(scene_point))
            owner = owner.parentWidget()
        return self.viewport().mapToGlobal(point)

    def _on_item_clicked(self, item):
        try:
            if item is None or not self._popup_context_valid():
                self._cancel_suggestions()
                return
            self._insert_completion(item.text())
        except Exception:
            pass

    def _insert_completion(self, completion):
        if not isinstance(completion, str) or not completion.strip():
            return
        options = self._completion_options()
        self._insert_prompt_tag(auto_complete.format_tag(completion.strip(),
            escape_parentheses=options['escape_parentheses'], replace_spaces=options['replace_spaces']))

    def _insert_prompt_tag(self, tag):
        from aetherloom_core.prompt_tokens import completion_insertion
        if self.isReadOnly() or not self.isEnabled() or self._ime_composing:
            self._cancel_suggestions()
            return
        cur = self.textCursor()
        text = self.toPlainText()
        before = QtGui.QTextCursor(cur)
        before.setPosition(0)
        before.setPosition(cur.selectionStart(), QtGui.QTextCursor.KeepAnchor)
        start = len(before.selectedText())
        before.setPosition(0)
        before.setPosition(cur.selectionEnd(), QtGui.QTextCursor.KeepAnchor)
        end = len(before.selectedText())
        start, end, replacement = completion_insertion(text, start, end, tag,
                                                       syntax=self.prompt_weight_syntax)
        if start == end and not replacement:
            self._cancel_suggestions()
            QtWidgets.QToolTip.showText(self._completion_global_point(self.cursorRect().bottomLeft()),
                                       '请将光标移到提示词正文中，避免改写权重数字或拆开转义符。', self)
            return
        if text[start:end] == replacement:
            self._cancel_suggestions()
            cur.setPosition(len(text[:end].encode('utf-16-le')) // 2)
            self.setTextCursor(cur)
            self._cancel_suggestions()
            return
        self._cancel_suggestions()
        self._completion_editing = True
        cur.beginEditBlock()
        try:
            cur.setPosition(len(text[:start].encode('utf-16-le')) // 2)
            cur.setPosition(len(text[:end].encode('utf-16-le')) // 2, QtGui.QTextCursor.KeepAnchor)
            cur.insertText(replacement)
            self.setTextCursor(cur)
        finally:
            cur.endEditBlock()
            self._completion_editing = False
        self._cancel_suggestions()
        self.setFocus()

    def keyPressEvent(self, event):
        if self._ime_composing:
            super().keyPressEvent(event)
            return
        if self._is_weight_shortcut(event) and self._can_adjust_prompt_weight():
            self.adjust_prompt_weight(1 if event.key() == QtCore.Qt.Key_Up else -1)
            event.accept()
            return
        if (event.key() == QtCore.Qt.Key_Space
                and event.modifiers() == QtCore.Qt.ControlModifier):
            self.show_suggestions()
            event.accept()
            return
        try:
            if self._popup.isVisible() and not self._popup_context_valid():
                self._cancel_suggestions()
            modifiers = event.modifiers() & (QtCore.Qt.ShiftModifier | QtCore.Qt.ControlModifier |
                                             QtCore.Qt.AltModifier | QtCore.Qt.MetaModifier)
            if self._popup.isVisible() and not modifiers:
                key = event.key()
                if key in (QtCore.Qt.Key_Enter, QtCore.Qt.Key_Return, QtCore.Qt.Key_Tab):
                    it = self._popup.currentItem()
                    if it:
                        self._insert_completion(it.text())
                        return
                elif key == QtCore.Qt.Key_Down and self._popup.count():
                    self._popup.setCurrentRow((self._popup.currentRow() + 1) % max(1, self._popup.count()))
                    return
                elif key == QtCore.Qt.Key_Up and self._popup.count():
                    self._popup.setCurrentRow((self._popup.currentRow() - 1 + self._popup.count()) % max(1, self._popup.count()))
                    return
                elif key == QtCore.Qt.Key_Escape:
                    self._hide_popup()
                    return
        except Exception:
            pass
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        QtWidgets.QApplication.instance().removeEventFilter(self)
        self._focus_out_timer.start()
        super().focusOutEvent(event)
        # Some input methods cancel preedit on focus loss without sending a
        # final empty input-method event; do not disable future completion.
        self._ime_composing = False

    def focusInEvent(self, event):
        QtWidgets.QApplication.instance().installEventFilter(self)
        self._focus_out_timer.stop()
        super().focusInEvent(event)
        if self.uses_local_completion_service:
            self._queue_completion()

    def hideEvent(self, event):
        QtWidgets.QApplication.instance().removeEventFilter(self)
        self._hide_popup()
        super().hideEvent(event)

    def resizeEvent(self, event):
        self._hide_popup()
        super().resizeEvent(event)


class _ComboWheelBlocker(QtCore.QObject):
    """Swallow wheel events on combo boxes to avoid accidental selection changes."""

    def eventFilter(self, obj, event):
        try:
            if event.type() == QtCore.QEvent.Wheel:
                return True
        except Exception:
            pass
        return super().eventFilter(obj, event)


class DropListWidget(QtWidgets.QListWidget):
    """A QListWidget that accepts file drops and forwards paths to a callback."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)
        # explicitly allow drops only (no dragging from this list)
        try:
            self.setDragDropMode(QtWidgets.QAbstractItemView.DropOnly)
            self.setDefaultDropAction(Qt.CopyAction)
            self.setDropIndicatorShown(True)
        except Exception:
            pass

    

    

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]        
        if self.drop_callback:
            try:
                self.drop_callback(paths)
            except Exception:
                pass
        event.acceptProposedAction()


class DropLabel(QtWidgets.QLabel):
    """A QLabel that accepts file drops and forwards paths to a callback."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)
        self.drop_callback = None
        # optional callback for double-click events
        self.dblclick_callback = None

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]        
        if self.drop_callback:
            try:
                self.drop_callback(paths)
            except Exception:
                pass
        event.acceptProposedAction()

    def mouseDoubleClickEvent(self, event):
        try:
            if self.dblclick_callback:
                self.dblclick_callback()
                return
        except Exception:
            pass
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        try:
            if event.button() == Qt.LeftButton:
                self._dragging = True
                self._last_mouse_pos = event.pos()
                event.accept()
                return
        except Exception:
            pass
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        try:
            if getattr(self, '_dragging', False) and self._last_mouse_pos is not None:
                cur = event.pos()
                dx = cur.x() - self._last_mouse_pos.x()
                dy = cur.y() - self._last_mouse_pos.y()
                self._last_mouse_pos = cur
                # apply pan
                ox = getattr(self, '_origin_x', 0) + dx
                oy = getattr(self, '_origin_y', 0) + dy
                self._origin_x = int(ox)
                self._origin_y = int(oy)
                self._update_display()
                event.accept()
                return
        except Exception:
            pass
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        try:
            if event.button() == Qt.LeftButton:
                self._dragging = False
                self._last_mouse_pos = None
                event.accept()
                return
        except Exception:
            pass
        super().mouseReleaseEvent(event)

    def set_base_pixmap(self, pixmap: QtGui.QPixmap):
        """Store the original pixmap and refresh display using current zoom/fit."""
        try:
            self._base_pixmap = QtGui.QPixmap(pixmap)
        except Exception:
            # accept QImage as well
            try:
                self._base_pixmap = QtGui.QPixmap.fromImage(pixmap)
            except Exception:
                self._base_pixmap = None
        # reset fit scale and pan/zoom when setting new pixmap
        self._zoom = 1.0
        self._fit_scale = None
        self._origin_x = 0
        self._origin_y = 0
        self._dragging = False
        self._last_mouse_pos = None
        self._update_display()

    def _update_display(self):
        """Scale and show the base pixmap according to fit scale and current zoom."""
        try:
            if not hasattr(self, '_base_pixmap') or self._base_pixmap is None:
                return
            bp = self._base_pixmap
            bw = bp.width()
            bh = bp.height()
            if bw == 0 or bh == 0:
                return
            w = max(1, self.width())
            h = max(1, self.height())
            # compute fit scale if not set
            if self._fit_scale is None:
                self._fit_scale = min(w / bw, h / bh)
            # total scale = fit_scale * zoom
            total_scale = max(0.01, self._fit_scale * getattr(self, '_zoom', 1.0))
            target_w = max(1, int(bw * total_scale))
            target_h = max(1, int(bh * total_scale))
            scaled = bp.scaled(target_w, target_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)

            # create a black canvas same size as widget and draw scaled pixmap at origin
            canvas = QtGui.QPixmap(self.width(), self.height())
            canvas.fill(QtGui.QColor(0, 0, 0))

            # ensure origin exists
            ox = getattr(self, '_origin_x', None)
            oy = getattr(self, '_origin_y', None)
            if ox is None or oy is None:
                # center image by default
                ox = (self.width() - scaled.width()) // 2
                oy = (self.height() - scaled.height()) // 2
                self._origin_x = ox
                self._origin_y = oy

            # clamp origin so image covers view (allow small blank when image smaller)
            def clamp_origin(ox, oy, sw, sh, vw, vh):
                if sw <= vw:
                    ox = (vw - sw) // 2
                else:
                    ox = min(0, max(vw - sw, ox))
                if sh <= vh:
                    oy = (vh - sh) // 2
                else:
                    oy = min(0, max(vh - sh, oy))
                return ox, oy

            ox, oy = clamp_origin(ox, oy, scaled.width(), scaled.height(), self.width(), self.height())
            self._origin_x = ox
            self._origin_y = oy

            painter = QtGui.QPainter(canvas)
            try:
                painter.drawPixmap(ox, oy, scaled)
            finally:
                painter.end()
            super().setPixmap(canvas)
        except Exception:
            pass

    def wheelEvent(self, event):
        """Zoom in/out with mouse wheel. Each notch scales by ~15%%."""
        try:
            # no base pixmap => nothing to do
            if not hasattr(self, '_base_pixmap') or self._base_pixmap is None:
                return
            delta = 0
            # Qt5: angleDelta returns QPoint; y() is vertical
            try:
                delta = event.angleDelta().y()
            except Exception:
                try:
                    delta = event.delta()
                except Exception:
                    delta = 0
            if delta == 0:
                return
            steps = delta / 120.0
            factor = 1.15 ** steps
            cur = getattr(self, '_zoom', 1.0)
            new_zoom = cur * factor
            # clamp zoom between 0.2x and 6x relative to fit
            new_zoom = max(0.2, min(6.0, new_zoom))

            # compute cursor-centered adjustment
            try:
                pos = event.pos()
                mx = pos.x()
                my = pos.y()
                # current total scale
                fit = getattr(self, '_fit_scale', None)
                if fit is None:
                    # fallback to a small default
                    fit = 1.0
                    self._fit_scale = fit
                cur_total = getattr(self, '_fit_scale', 1.0) * cur
                new_total = getattr(self, '_fit_scale', 1.0) * new_zoom
                # image coords of mouse before zoom
                ix = (mx - getattr(self, '_origin_x', 0)) / cur_total if cur_total != 0 else 0
                iy = (my - getattr(self, '_origin_y', 0)) / cur_total if cur_total != 0 else 0
                # new origin so that (ix,iy) maps to same mouse pos
                new_ox = mx - ix * new_total
                new_oy = my - iy * new_total
                self._zoom = new_zoom
                try:
                    if path:
                        self._set_file_info(path, 'orig')
                except Exception:
                    pass
                self._origin_x = int(new_ox)
                self._origin_y = int(new_oy)
            except Exception:
                # fallback: just set zoom and keep centered
                self._zoom = new_zoom
                self._origin_x = None
                self._origin_y = None
            self._update_display()
            event.accept()
        except Exception:
            try:
                event.ignore()
            except Exception:
                pass

    def resizeEvent(self, event):
        try:
            # when widget resizes, recompute fit scale and update display
            # keep current zoom
            self._fit_scale = None
            self._update_display()
        except Exception:
            try:
                if event.type() == QtCore.QEvent.Wheel and getattr(self, 'pages', None) is not None:
                    # only act when local page is current
                    try:
                        if self.pages.currentWidget() is not getattr(self, 'local_page', None):
                            return super().eventFilter(obj, event)
                    except Exception:
                        pass
                    delta = event.angleDelta().y()
                    steps = int(delta / 120)
                    if steps == 0:
                        return super().eventFilter(obj, event)
                    if hasattr(self, 'thumb_size_slider'):
                        cur = self.thumb_size_slider.value()
                        proportional = max(4, int(cur * 0.15))
                        new = max(self.thumb_size_slider.minimum(), min(self.thumb_size_slider.maximum(), cur + steps * proportional))
                        self.thumb_size_slider.setValue(new)
                        try:
                            if hasattr(self, 'thumb_size_spin') and self.thumb_size_spin.value() != new:
                                self.thumb_size_spin.blockSignals(True)
                                self.thumb_size_spin.setValue(new)
                                self.thumb_size_spin.blockSignals(False)
                        except Exception:
                            pass
                    if isinstance(obj, QtWidgets.QListWidget):
                        sb = obj.verticalScrollBar()
                        if sb is not None:
                            icon_h = max(1, obj.iconSize().height())
                            spacing = max(2, obj.spacing())
                            row_span = max(1, icon_h + spacing + 32)
                            px = steps * row_span
                            sb.setValue(sb.value() - px)
                    return True
                    try:
                        self._current_pixmaps['orig'] = None
                        self._current_paths['orig'] = None
                    except Exception:
                        pass
            except Exception:
                pass
        try:
            super().resizeEvent(event)
        except Exception:
            pass


class ClickableSlider(QtWidgets.QSlider):
    """QSlider that jumps to the clicked groove position while preserving drag behavior."""
    def mousePressEvent(self, event):
        try:
            if event.button() == Qt.LeftButton:
                if self.orientation() == Qt.Horizontal:
                    length = max(1, self.width())
                    x = event.pos().x()
                    minv, maxv = self.minimum(), self.maximum()
                    val = int(minv + (maxv - minv) * x / length)
                    self.setValue(val)
                else:
                    length = max(1, self.height())
                    y = event.pos().y()
                    minv, maxv = self.minimum(), self.maximum()
                    val = int(minv + (maxv - minv) * (length - y) / length)
                    self.setValue(val)
        except Exception:
            pass
        super().mousePressEvent(event)


class ThumbnailDelegate(QtWidgets.QStyledItemDelegate):
    """Custom delegate that avoids darkening the thumbnail on selection
    and instead draws a highlighted border around the decoration area.
    """
    def paint(self, painter, option, index):
        try:
            # detect selected state but avoid letting base paint draw selection background
            selected = bool(option.state & QtWidgets.QStyle.State_Selected)
            opt = QtWidgets.QStyleOptionViewItem(option)
            if selected:
                opt.state &= ~QtWidgets.QStyle.State_Selected
            # perform default painting without selection background
            super().paint(painter, opt, index)

            # Draw a highlight border around the icon area when selected
            if selected:
                r = option.rect
                view = option.widget
                try:
                    iconSize = view.iconSize()
                except Exception:
                    iconSize = QtCore.QSize(64, 64)
                # center icon rect horizontally; leave a small top margin
                x = r.x() + max(0, (r.width() - iconSize.width()) // 2)
                y = r.y() + 6
                iconRect = QtCore.QRect(x, y, iconSize.width(), iconSize.height())
                # pen: bright yellow/orange
                # use red highlight; width = half of thumbnail spacing when possible
                pen = QtGui.QPen(QtGui.QColor(255, 0, 0))
                try:
                    spacing = int(view.spacing()) if hasattr(view, 'spacing') else 14
                except Exception:
                    spacing = 14
                pen.setWidth(max(2, int(max(1, spacing // 2))))
                painter.setPen(pen)
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.drawRoundedRect(iconRect.adjusted(-4, -4, 4, 4), 6, 6)
        except Exception:
            try:
                super().paint(painter, option, index)
            except Exception:
                pass
