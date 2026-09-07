"""Reusable prompt, drag/drop, slider, and thumbnail widgets."""
import re
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt
from aetherloom_core import autocomplete as auto_complete
from aetherloom_core.paths import current_dir

class CompletionTextEdit(QtWidgets.QTextEdit):
    """QTextEdit 扩展：内置基于 `auto_complete` 的弹出补全列表。"""
    def __init__(self, parent=None, current_dir=None, limit=50):
        super().__init__(parent)
        self.setCursorWidth(4)
        try:
            # manager will load autocomplete.txt from current_dir
            self._manager = auto_complete.get_manager(current_dir or globals().get('current_dir'))
        except Exception:
            self._manager = None
        self._limit = limit or 20
        self._popup = auto_complete.AutocompletePopup(self)
        self._popup.setUniformItemSizes(True)
        self._popup_cursor_position = None
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
    def _get_prefix_before_cursor(self):
        cur = self.textCursor()
        if cur.hasSelection():
            return ''
        # QTextCursor positions count UTF-16 units, while Python indexes Unicode
        # code points. Let Qt extract the text to avoid slicing at the wrong emoji.
        cur.movePosition(QtGui.QTextCursor.StartOfBlock, QtGui.QTextCursor.KeepAnchor)
        match = re.search(r'([^,，\s]+)$', cur.selectedText())
        return match.group(1) if match else ''

    def _hide_popup(self, *args):
        self._popup.hide()
        self._popup_cursor_position = None

    def _on_cursor_position_changed(self):
        cur = self.textCursor()
        if cur.hasSelection() or cur.position() != self._popup_cursor_position:
            self._hide_popup()

    def _hide_popup_if_unfocused(self):
        if not self.hasFocus():
            self._hide_popup()

    def _on_text_changed(self):
        # only show when widget has focus
        try:
            if not self.hasFocus() or self.isReadOnly():
                self._hide_popup()
                return
            prefix = self._get_prefix_before_cursor()
            if not prefix or not self._manager:
                self._hide_popup()
                return
            matches = self._manager.get_matches(prefix, limit=self._limit)
            if not matches:
                self._hide_popup()
                return
            self._show_popup(matches)
        except Exception:
            try:
                self._hide_popup()
            except Exception:
                pass

    def _show_popup(self, matches):
        if not matches:
            self._hide_popup()
            return
        self._popup.clear()
        self._popup.addItems(matches)
        for row, match in enumerate(matches):
            self._popup.item(row).setToolTip(match)
        self._popup.setCurrentRow(0)
        self._popup.ensurePolished()

        # Cursor rectangles are relative to the viewport, not the editor frame.
        rect = self.cursorRect()
        below = self.viewport().mapToGlobal(rect.bottomLeft()) + QtCore.QPoint(0, 1)
        above = self.viewport().mapToGlobal(rect.topLeft())
        screen = QtWidgets.QApplication.screenAt(below) or QtWidgets.QApplication.primaryScreen()
        if screen is None:
            self._hide_popup()
            return
        available = screen.availableGeometry()
        frame = self._popup.frameWidth() * 2
        scrollbar = self._popup.style().pixelMetric(QtWidgets.QStyle.PM_ScrollBarExtent)
        width = min(max(self._popup.sizeHintForColumn(0) + frame + scrollbar, 200),
                    800, available.width())
        row_height = max(1, self._popup.sizeHintForRow(0))
        desired_height = min(row_height * min(len(matches), 15) + frame, 600)
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
        self._popup.show()

    def _on_item_clicked(self, item):
        try:
            if item is None:
                return
            self._insert_completion(item.text())
        except Exception:
            pass

    def _insert_completion(self, completion):
        prefix = self._get_prefix_before_cursor()
        if not prefix or self.isReadOnly():
            self._hide_popup()
            return
        cur = self.textCursor()
        pos = cur.position()
        prefix_length = len(prefix.encode('utf-16-le')) // 2
        cur.beginEditBlock()
        cur.setPosition(pos - prefix_length, QtGui.QTextCursor.MoveAnchor)
        cur.setPosition(pos, QtGui.QTextCursor.KeepAnchor)
        following = self.textCursor()
        following.movePosition(QtGui.QTextCursor.End, QtGui.QTextCursor.KeepAnchor)
        cur.insertText(auto_complete.format_completion(completion, following.selectedText()))
        cur.endEditBlock()
        self.setTextCursor(cur)
        self._hide_popup()
        self.setFocus()

    def keyPressEvent(self, event):
        try:
            modifiers = event.modifiers() & (QtCore.Qt.ShiftModifier | QtCore.Qt.ControlModifier |
                                             QtCore.Qt.AltModifier | QtCore.Qt.MetaModifier)
            if self._popup.isVisible() and not modifiers:
                key = event.key()
                if key in (QtCore.Qt.Key_Enter, QtCore.Qt.Key_Return, QtCore.Qt.Key_Tab):
                    it = self._popup.currentItem()
                    if it:
                        self._insert_completion(it.text())
                        return
                elif key == QtCore.Qt.Key_Down:
                    self._popup.setCurrentRow((self._popup.currentRow() + 1) % max(1, self._popup.count()))
                    return
                elif key == QtCore.Qt.Key_Up:
                    self._popup.setCurrentRow((self._popup.currentRow() - 1 + self._popup.count()) % max(1, self._popup.count()))
                    return
                elif key == QtCore.Qt.Key_Escape:
                    self._hide_popup()
                    return
        except Exception:
            pass
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        self._focus_out_timer.start()
        super().focusOutEvent(event)

    def focusInEvent(self, event):
        self._focus_out_timer.stop()
        super().focusInEvent(event)

    def hideEvent(self, event):
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
