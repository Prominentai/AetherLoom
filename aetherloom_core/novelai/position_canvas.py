"""Character positions rendered inside the NovelAI output workspace."""
from copy import deepcopy
from math import hypot

from PyQt5 import QtCore, QtGui, QtWidgets

from .styles import workspace_stylesheet as app_stylesheet, workspace_palette as palette
from .references import guard_wheel


_GRID = (.1, .3, .5, .7, .9)


def _grid_value(value):
    return min(_GRID, key=lambda candidate: (round(abs(candidate - value), 12), candidate))


class CharacterPositionCanvas(QtWidgets.QWidget):
    """Numbered positions in normalized image space, with keyboard access."""

    selectionChanged = QtCore.pyqtSignal(int)
    positionChanged = QtCore.pyqtSignal(int, float, float)

    def __init__(self, characters, size, free_coordinates=True, parent=None):
        super().__init__(parent)
        self._characters = deepcopy(characters)
        self._image_size = QtCore.QSize(max(1, int(size[0])), max(1, int(size[1])))
        self._free = bool(free_coordinates)
        self._selected = 0 if characters else -1
        self._dragging = False
        self._colors = palette('dark')
        self._background = QtGui.QPixmap()
        self.setMinimumSize(200, 180)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName('角色位置画布')
        self._update_accessible()

    def sizeHint(self):
        return QtCore.QSize(480, 380)

    def values(self):
        return deepcopy(self._characters)

    def configure(self, characters, size, free_coordinates=True):
        """Update form state silently, preserving the selected role and drag."""
        self._characters = deepcopy(characters)
        self._image_size = QtCore.QSize(max(1, int(size[0])), max(1, int(size[1])))
        self._free = bool(free_coordinates)
        self._selected = min(max(0, self._selected), len(characters) - 1)
        if self._selected < 0:
            self._dragging = False
        self._update_accessible()
        self.update()

    def set_background(self, pixmap):
        """Share a bounded GUI preview; never open or decode source files here."""
        if pixmap is None or pixmap.isNull():
            self._background = QtGui.QPixmap()
        elif max(pixmap.width(), pixmap.height()) > 2200:
            self._background = pixmap.scaled(2200, 2200, QtCore.Qt.KeepAspectRatio,
                                             QtCore.Qt.SmoothTransformation)
        else:
            self._background = QtGui.QPixmap(pixmap)
        self.update()

    def background_matches(self):
        if self._background.isNull():
            return False
        actual = self._background.width() / self._background.height()
        target = self._image_size.width() / self._image_size.height()
        return abs(actual / target - 1) <= .005

    def image_rect(self):
        # Leave room for the whole marker even when its coordinate is 0 or 1.
        area = QtCore.QRectF(self.rect()).adjusted(19, 19, -19, -19)
        ratio = self._image_size.width() / self._image_size.height()
        width = min(area.width(), area.height() * ratio)
        height = width / ratio
        return QtCore.QRectF(area.center().x() - width / 2,
                             area.center().y() - height / 2, width, height)

    def point_for(self, index):
        value = self._characters[index]
        rect = self.image_rect()
        return QtCore.QPointF(rect.left() + float(value.get('x', .5)) * rect.width(),
                             rect.top() + float(value.get('y', .5)) * rect.height())

    def select(self, index):
        if not 0 <= index < len(self._characters) or index == self._selected:
            return
        self._selected = index
        self._update_accessible()
        self.update()
        self.selectionChanged.emit(index)

    def apply_theme(self, mode):
        self._colors = palette(mode)
        self.update()

    def _update_accessible(self):
        if self._selected < 0:
            self.setAccessibleDescription('请先添加角色。')
            return
        value = self._characters[self._selected]
        self.setAccessibleDescription(
            f"角色 {self._selected + 1}，X {value.get('x', .5):g}，Y {value.get('y', .5):g}。"
            '使用上方角色编号切换角色，方向键调整位置。')

    def _set_position(self, x, y):
        if self._selected < 0:
            return
        x, y = max(0., min(1., x)), max(0., min(1., y))
        x, y = (round(x, 3), round(y, 3)) if self._free else (_grid_value(x), _grid_value(y))
        value = self._characters[self._selected]
        if (value.get('x', .5), value.get('y', .5)) == (x, y):
            return
        value.update(x=x, y=y)
        self._update_accessible()
        self.update()
        self.positionChanged.emit(self._selected, x, y)

    def _place(self, point):
        rect = self.image_rect()
        self._set_position((point.x() - rect.left()) / max(1., rect.width()),
                           (point.y() - rect.top()) / max(1., rect.height()))

    def _hits(self, point):
        hits = []
        for index in range(len(self._characters)):
            location = self.point_for(index)
            distance = hypot(location.x() - point.x(), location.y() - point.y())
            if distance <= 19:
                hits.append((distance, index != self._selected, index))
        return [item[2] for item in sorted(hits)]

    def mousePressEvent(self, event):
        if event.button() != QtCore.Qt.LeftButton or self._selected < 0:
            return super().mousePressEvent(event)
        hits = self._hits(event.localPos())
        if hits:
            self.select(hits[0])
        elif self.image_rect().contains(event.localPos()):
            self._place(event.localPos())
        else:
            return super().mousePressEvent(event)
        self._dragging = True
        self.setFocus(QtCore.Qt.MouseFocusReason)
        self.setCursor(QtCore.Qt.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._place(event.localPos())
            event.accept()
            return
        hits = self._hits(event.localPos())
        self.setCursor(QtCore.Qt.OpenHandCursor if hits else QtCore.Qt.CrossCursor
                       if self.image_rect().contains(event.localPos()) else QtCore.Qt.ArrowCursor)
        self.setToolTip('、'.join(str(self._characters[index].get('name') or
                                  f"角色 {self._characters[index].get('_display_index', index + 1)}")
                                 for index in hits) +
                        ('；重叠时可用上方角色编号切换。' if len(hits) > 1 else '') if hits else '')
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging and event.button() == QtCore.Qt.LeftButton:
            self._dragging = False
            self.setCursor(QtCore.Qt.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def hideEvent(self, event):
        # Leaving the embedded editor can interrupt a drag before mouse release.
        self._dragging = False
        self.unsetCursor()
        if QtWidgets.QWidget.mouseGrabber() is self:
            self.releaseMouse()
        super().hideEvent(event)

    def keyPressEvent(self, event):
        directions = {QtCore.Qt.Key_Left: (-1, 0), QtCore.Qt.Key_Right: (1, 0),
                      QtCore.Qt.Key_Up: (0, -1), QtCore.Qt.Key_Down: (0, 1)}
        if self._selected >= 0 and event.key() in directions:
            value = self._characters[self._selected]
            x, y = float(value.get('x', .5)), float(value.get('y', .5))
            dx, dy = directions[event.key()]
            step = .1 if event.modifiers() & QtCore.Qt.ShiftModifier else .01
            if not self._free:
                x, y, step = _grid_value(x), _grid_value(y), .2
            self._set_position(x + dx * step, y + dy * step)
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        c = self._colors
        p.fillRect(self.rect(), QtGui.QColor(c['canvas']))
        rect = self.image_rect()
        p.setPen(QtGui.QPen(QtGui.QColor(c['border']), 1))
        p.setBrush(QtGui.QColor(c['surface']))
        p.drawRoundedRect(rect, 3, 3)
        if self.background_matches():
            p.save()
            clip = QtGui.QPainterPath()
            clip.addRoundedRect(rect, 3, 3)
            p.setClipPath(clip)
            p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
            size = QtCore.QSizeF(self._background.size())
            size.scale(rect.size(), QtCore.Qt.KeepAspectRatio)
            target = QtCore.QRectF(rect.center().x() - size.width() / 2,
                                  rect.center().y() - size.height() / 2,
                                  size.width(), size.height())
            p.drawPixmap(target, self._background, QtCore.QRectF(self._background.rect()))
            veil = QtGui.QColor(c['canvas'])
            veil.setAlpha(135)
            p.fillRect(rect, veil)
            p.restore()
        if not self._free:
            for n in range(1, 5):
                p.drawLine(QtCore.QPointF(rect.left() + rect.width() * n / 5, rect.top()),
                           QtCore.QPointF(rect.left() + rect.width() * n / 5, rect.bottom()))
                p.drawLine(QtCore.QPointF(rect.left(), rect.top() + rect.height() * n / 5),
                           QtCore.QPointF(rect.right(), rect.top() + rect.height() * n / 5))
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(QtGui.QColor(c['muted']))
            for x in _GRID:
                for y in _GRID:
                    p.drawEllipse(QtCore.QPointF(rect.left() + x * rect.width(),
                                                 rect.top() + y * rect.height()), 2, 2)
        order = [i for i in range(len(self._characters)) if i != self._selected]
        if self._selected >= 0:
            order.append(self._selected)
        for index in order:
            point = self.point_for(index)
            selected = index == self._selected
            invalid = not self._free and any(
                not any(abs(float(self._characters[index].get(axis, .5)) - v) < 1e-6 for v in _GRID)
                for axis in ('x', 'y'))
            edge = c['warning'] if invalid else c['accent'] if selected else c['muted']
            p.setBrush(QtGui.QColor(c['accent'] if selected else c['surface']))
            p.setPen(QtGui.QPen(QtGui.QColor(edge), 2 if selected or invalid else 1.5))
            p.drawEllipse(point, 15 if selected else 13, 15 if selected else 13)
            font = p.font()
            font.setPixelSize(12)
            font.setBold(True)
            p.setFont(font)
            selected_text = c['canvas'] if QtGui.QColor(c['accent']).lightness() > 155 else '#ffffff'
            p.setPen(QtGui.QColor(selected_text if selected else c['text']))
            p.drawText(QtCore.QRectF(point.x() - 16, point.y() - 16, 32, 32),
                       QtCore.Qt.AlignCenter, str(self._characters[index].get('_display_index', index + 1)))
        if self.hasFocus() and self._selected >= 0:
            p.setBrush(QtCore.Qt.NoBrush)
            p.setPen(QtGui.QPen(QtGui.QColor(c['accent']), 1))
            p.drawEllipse(self.point_for(self._selected), 18, 18)


class CharacterPositionEditor(QtWidgets.QWidget):
    """Embedded editor; the owning page decides how finish/cancel apply."""

    positionEdited = QtCore.pyqtSignal(int, float, float)
    finishRequested = QtCore.pyqtSignal()
    cancelRequested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('novelaiPositionEditor')
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setMinimumSize(220, 220)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self._mode = 'dark'
        self._labels = []
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(8)
        self.character_selector = guard_wheel(QtWidgets.QComboBox())
        self.character_selector.setAccessibleName('要定位的角色')
        self.character_selector.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.character_selector.setMinimumContentsLength(2)
        self.character_selector.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        # Keep the selector as a silent compatibility adapter; the visible
        # surface is a scrolling row of numbered roles, like the web editor.
        self.character_selector.setParent(self)
        self.character_selector.hide()
        self.character_selector.setFocusPolicy(QtCore.Qt.NoFocus)
        self.role_tabs = QtWidgets.QTabBar(self)
        self.role_tabs.setObjectName('positionRoleTabs')
        self.role_tabs.setAccessibleName('角色编号')
        self.role_tabs.setDrawBase(False)
        self.role_tabs.setExpanding(False)
        self.role_tabs.setUsesScrollButtons(True)
        self.role_tabs.setElideMode(QtCore.Qt.ElideNone)
        self.role_tabs.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.role_tabs.setMinimumWidth(0)
        self.role_tabs.setFixedHeight(34)
        self.role_tabs.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        top.addWidget(self.role_tabs, 1)
        self.readout = QtWidgets.QLabel()
        self.readout.setObjectName('positionReadout')
        self.readout.setMinimumWidth(0)
        self.readout.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)
        self.readout.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        top.addWidget(self.readout)
        root.addLayout(top)
        self.canvas = CharacterPositionCanvas([], (832, 1216), True, self)
        self.canvas.setMinimumSize(120, 120)
        root.addWidget(self.canvas, 1)
        footer = QtWidgets.QHBoxLayout()
        footer.setSpacing(6)
        self.hint = QtWidgets.QLabel()
        self.hint.setObjectName('positionHint')
        self.hint.setMinimumWidth(0)
        self.hint.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        footer.addWidget(self.hint, 1)
        self.cancel_button = QtWidgets.QPushButton('取消')
        self.cancel_button.setObjectName('positionCancel')
        self.cancel_button.setMinimumHeight(32)
        self.cancel_button.setToolTip('放弃本次位置调整并返回输出预览（Esc）')
        self.cancel_button.clicked.connect(self.cancelRequested)
        footer.addWidget(self.cancel_button)
        self.finish_button = QtWidgets.QPushButton('完成定位')
        self.finish_button.setObjectName('positionFinish')
        self.finish_button.setMinimumHeight(32)
        self.finish_button.setToolTip('保留位置调整并返回输出预览')
        self.finish_button.clicked.connect(self.finishRequested)
        footer.addWidget(self.finish_button)
        root.addLayout(footer)
        self.character_selector.currentIndexChanged.connect(self.canvas.select)
        self.role_tabs.currentChanged.connect(self.canvas.select)
        self.canvas.selectionChanged.connect(self.role_tabs.setCurrentIndex)
        self.canvas.selectionChanged.connect(self.character_selector.setCurrentIndex)
        self.character_selector.currentIndexChanged.connect(self._update_readout)
        self.canvas.positionChanged.connect(self._position_edited)
        self._escape = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Escape), self)
        self._escape.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        self._escape.activated.connect(self.cancelRequested)
        self.configure([], (832, 1216), True)
        self.apply_theme('dark')

    def configure(self, characters, size, free_coordinates=True):
        self.canvas.configure(characters, size, free_coordinates)
        labels = [(str(value.get('_display_index', index + 1)),
                   str(value.get('name', '')).strip(),
                   ' '.join(str(value.get('prompt', '')).split()))
                  for index, value in enumerate(characters)]
        with QtCore.QSignalBlocker(self.character_selector), QtCore.QSignalBlocker(self.role_tabs):
            if labels != self._labels:
                self._labels = labels
                self.character_selector.clear()
                while self.role_tabs.count():
                    self.role_tabs.removeTab(self.role_tabs.count() - 1)
                for index, (number, name, prompt) in enumerate(labels):
                    short = prompt[:54] + ('…' if len(prompt) > 54 else '')
                    title = name or f'角色 {number}'
                    self.character_selector.addItem(title + (' · ' + short if short else ''), index)
                    self.character_selector.setItemData(index, prompt, QtCore.Qt.ToolTipRole)
                    self.role_tabs.addTab(number)
                    self.role_tabs.setTabToolTip(index, title + (' · ' + prompt if prompt else ''))
                    self.role_tabs.setTabData(index, index)
            self.character_selector.setCurrentIndex(self.canvas._selected)
            self.role_tabs.setCurrentIndex(self.canvas._selected)
        self.finish_button.setEnabled(bool(characters))
        self._update_readout()

    def set_background(self, pixmap):
        self.canvas.set_background(pixmap)
        self._update_readout()

    def select_character(self, index):
        self.canvas.select(index)

    def selected_index(self):
        return self.canvas._selected

    def focus_canvas(self):
        self.canvas.setFocus(QtCore.Qt.OtherFocusReason)

    def values(self):
        return self.canvas.values()

    def _position_edited(self, index, x, y):
        self._update_readout()
        self.positionEdited.emit(index, x, y)

    def _update_readout(self, *unused):
        values = self.canvas.values()
        index = self.canvas._selected
        valid = 0 <= index < len(values)
        value = values[index] if valid else {}
        text = f"X {value.get('x', .5):.3f}   Y {value.get('y', .5):.3f}" if valid else '请先添加角色'
        self.readout.setText(text)
        self.readout.setToolTip(text + f' · {self.canvas._image_size.width()} × {self.canvas._image_size.height()}')
        invalid = not self.canvas._free and any(
            not any(abs(float(v.get(axis, .5)) - p) < 1e-6 for p in _GRID)
            for v in values for axis in ('x', 'y'))
        mismatch = not self.canvas._background.isNull() and not self.canvas.background_matches()
        text = ('橙色角色需要对齐网格' if invalid else
                '比例不同，按目标画幅定位' if mismatch else
                '拖动编号 · 方向键微调' if self.canvas._free else '点击网格定位角色')
        details = ('选择角色后点击画面或拖动编号。'
                   + ('方向键微调，Shift 加快移动。' if self.canvas._free else '位置吸附到 5 × 5 网格中心。'))
        if invalid:
            details += '橙色标记保留了原非网格坐标，点击或拖动该角色可重新定位。'
        if mismatch:
            details += '当前结果与生成尺寸的宽高比例不同，因此使用空白目标画幅，避免错误映射。'
        self.hint.setText(text)
        self.hint.setToolTip(details)
        self.setAccessibleDescription(details)
        tone = 'warning' if invalid or mismatch else 'muted'
        if self.hint.property('tone') != tone:
            self.hint.setProperty('tone', tone)
            self.hint.style().unpolish(self.hint)
            self.hint.style().polish(self.hint)
        self.setToolTip(details)

    def apply_theme(self, mode):
        self._mode = mode
        c = palette(mode)
        self.setStyleSheet(app_stylesheet(mode).replace('#rhAppPage', '#novelaiPositionEditor') + f'''
            QWidget#novelaiPositionEditor {{background:{c['canvas']};color:{c['text']};border:none;}}
            QWidget#novelaiPositionEditor QTabBar#positionRoleTabs {{background:transparent;border:none;}}
            QWidget#novelaiPositionEditor QTabBar#positionRoleTabs::tab {{background:transparent;
                color:{c['muted']};border:1px solid {c['border']};border-radius:4px;
                min-width:23px;min-height:22px;padding:3px 5px;margin-right:5px;font-size:12px;}}
            QWidget#novelaiPositionEditor QTabBar#positionRoleTabs::tear {{width:0;image:none;}}
            QWidget#novelaiPositionEditor QTabBar#positionRoleTabs::tab:hover {{background:{c['hover']};color:{c['text']};}}
            QWidget#novelaiPositionEditor QTabBar#positionRoleTabs::tab:selected {{background:{c['accent_soft']};
                color:{c['accent']};border-color:{c['accent']};font-weight:600;}}
            QWidget#novelaiPositionEditor QTabBar#positionRoleTabs QToolButton {{background:{c['canvas']};
                color:{c['text']};border:none;border-radius:0;min-width:20px;max-width:20px;
                padding:0;min-height:24px;}}
            QWidget#novelaiPositionEditor QTabBar#positionRoleTabs QToolButton:hover {{background:{c['hover']};}}
            QWidget#novelaiPositionEditor QLabel#positionHint,
            QWidget#novelaiPositionEditor QLabel#positionReadout {{color:{c['muted']};font-size:11px;}}
            QWidget#novelaiPositionEditor QLabel#positionHint[tone="warning"] {{color:{c['warning']};}}
            QWidget#novelaiPositionEditor QPushButton#positionFinish {{background:{c['accent_soft']};
                color:{c['accent']};border:1px solid {c['accent']};font-weight:600;}}
            QWidget#novelaiPositionEditor QPushButton#positionCancel {{background:transparent;color:{c['muted']};}}
        ''')
        self.canvas.apply_theme(mode)
