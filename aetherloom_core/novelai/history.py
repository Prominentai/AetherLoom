"""Paged history: at most 32 thumbnail pixmaps regardless of library size."""
import copy
import os
import math
import time
import zipfile
from pathlib import Path
from PyQt5 import QtCore, QtGui, QtWidgets

from .jobs import Job
from .storage import MAX_IMAGES, bounded_history


class _HistoryList(QtWidgets.QListWidget):
    reuseClicked = QtCore.pyqtSignal(object, int)

    def mouseReleaseEvent(self, event):
        modifiers = event.modifiers() & (QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier)
        if event.button() == QtCore.Qt.LeftButton and modifiers:
            item = self.itemAt(event.pos())
            if item is not None:
                self.reuseClicked.emit(item, int(modifiers))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and event.modifiers() & (QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        modifiers = event.modifiers() & (QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier)
        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter, QtCore.Qt.Key_Space) and modifiers:
            item = self.currentItem()
            if item is not None:
                self.reuseClicked.emit(item, int(modifiers))
            event.accept()
            return
        super().keyPressEvent(event)


class HistoryPanel(QtWidgets.QWidget):
    selected = QtCore.pyqtSignal(dict)
    reuseRequested = QtCore.pyqtSignal(dict)
    reuseSettingsRequested = QtCore.pyqtSignal(dict)
    reuseSeedRequested = QtCore.pyqtSignal(dict)
    reuseAllRequested = QtCore.pyqtSignal(dict)
    useImageRequested = QtCore.pyqtSignal(dict)
    itemsChanged = QtCore.pyqtSignal(list)
    PAGE_SIZE = 32

    def __init__(self, owner=None, parent=None):
        super().__init__(parent)
        self.owner, self._items, self._page = owner, [], 0
        self._export_job = None
        self.setMinimumWidth(180)
        self.setObjectName('novelaiHistory')
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(10)
        top = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel('历史记录')
        self.title.setObjectName('novelaiHistoryTitle')
        top.addWidget(self.title, 1)
        self.count = QtWidgets.QLabel('0')
        self.count.setObjectName('novelaiHistoryCount')
        self.count.setAlignment(QtCore.Qt.AlignCenter)
        self.count.setFixedHeight(22)
        top.addWidget(self.count)
        self.export_btn = QtWidgets.QToolButton(text='导出')
        self.export_btn.setEnabled(False)
        self.export_btn.setToolTip('将已保存结果打包为 ZIP；最多保留最近 500 条历史索引')
        self.export_btn.clicked.connect(self.export_zip)
        top.addWidget(self.export_btn)
        layout.addLayout(top)
        self.hint = QtWidgets.QLabel('单击预览 · Ctrl＋点击复用参数 · Shift＋点击回填种子')
        self.hint.setObjectName('novelaiHistoryHint')
        self.hint.setWordWrap(True)
        self.hint.hide()
        layout.addWidget(self.hint)
        self.stack = QtWidgets.QStackedWidget()
        self.list = _HistoryList()
        self.list.setObjectName('novelaiHistoryList')
        self.list.setAccessibleName('NovelAI 历史图片')
        self.list.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.list.setIconSize(QtCore.QSize(64, 64))
        self.list.setSpacing(6)
        self.list.setUniformItemSizes(True)
        self.list.setTextElideMode(QtCore.Qt.ElideRight)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.menu)
        self.list.itemClicked.connect(lambda item: self.selected.emit(copy.deepcopy(item.data(QtCore.Qt.UserRole))))
        self.list.reuseClicked.connect(lambda item, modifiers: self._reuse_click(item.data(QtCore.Qt.UserRole), modifiers))
        self.list.itemDoubleClicked.connect(lambda item: self.open(item.data(QtCore.Qt.UserRole)))
        self.stack.addWidget(self.list)
        empty = QtWidgets.QWidget()
        empty_layout = QtWidgets.QVBoxLayout(empty)
        empty_layout.setContentsMargins(8, 20, 8, 20)
        empty_layout.setSpacing(10)
        empty_layout.addStretch(1)
        empty_title = QtWidgets.QLabel('还没有作品')
        empty_title.setObjectName('novelaiEmptyTitle')
        empty_text = QtWidgets.QLabel('生成的图片会保存在这里\n随时预览、复用参数或导出')
        empty_text.setObjectName('novelaiEmptyText')
        for label in (empty_title, empty_text):
            label.setAlignment(QtCore.Qt.AlignCenter)
            label.setWordWrap(True)
            empty_layout.addWidget(label)
        empty_layout.addStretch(1)
        self.stack.addWidget(empty)
        self.stack.setCurrentIndex(1)
        layout.addWidget(self.stack, 1)
        foot = QtWidgets.QHBoxLayout()
        self.previous = QtWidgets.QToolButton(text='‹')
        self.next = QtWidgets.QToolButton(text='›')
        self.previous.setToolTip('上一页')
        self.next.setToolTip('下一页')
        self.previous.setAccessibleName('历史记录上一页')
        self.next.setAccessibleName('历史记录下一页')
        self.previous.setEnabled(False)
        self.next.setEnabled(False)
        self.pages = QtWidgets.QLabel('0 / 0')
        self.pages.setObjectName('novelaiHistoryPages')
        self.pages.setAlignment(QtCore.Qt.AlignCenter)
        self.previous.clicked.connect(lambda: self.turn(-1))
        self.next.clicked.connect(lambda: self.turn(1))
        foot.addWidget(self.previous)
        foot.addWidget(self.pages, 1)
        foot.addWidget(self.next)
        layout.addLayout(foot)

    def items(self):
        return copy.deepcopy(self._items)

    def set_items(self, items):
        self._items = bounded_history([v for v in items[:MAX_IMAGES] if isinstance(v, dict)])
        self._page = 0
        self.render()

    def add_items(self, items):
        self._items = bounded_history(list(items) + self._items)
        self._page = 0
        self.render()
        self.itemsChanged.emit(self.items())

    def turn(self, direction):
        self._page = max(0, min(self._page + direction, max(0, (len(self._items) - 1) // self.PAGE_SIZE)))
        self.render()

    def render(self):
        self.list.clear()
        start = self._page * self.PAGE_SIZE
        for record in self._items[start:start + self.PAGE_SIZE]:
            path = str(record.get('path', ''))
            created = record.get('created')
            try:
                if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created):
                    raise ValueError('Unknown creation time')
                stamp = time.strftime('%m/%d %H:%M', time.localtime(created))
            except (ValueError, OverflowError, OSError):
                stamp = '时间未知'
            item = QtWidgets.QListWidgetItem(f'{stamp}\n{record.get("width", "?")} × {record.get("height", "?")}')
            item.setSizeHint(QtCore.QSize(160, 80))
            item.setTextAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
            item.setData(QtCore.Qt.UserRole, record)
            seed = record.get('seed')
            seed_label = '服务未返回' if seed is None else str(seed)
            item.setToolTip(f'{Path(path).name}\n种子：{seed_label}\n单击预览 · 双击打开 · 右键更多\nCtrl＋点击：复用参数（保留当前种子和输入素材）\nShift＋点击：回填种子\nCtrl＋Shift＋点击：复用参数与种子')
            reader = QtGui.QImageReader(path)
            size = reader.size()
            if size.isValid() and size.width() * size.height() <= 40_000_000:
                reader.setScaledSize(size.scaled(152, 152, QtCore.Qt.KeepAspectRatio))
                image = reader.read()
                if not image.isNull():
                    item.setIcon(QtGui.QIcon(QtGui.QPixmap.fromImage(image)))
            if not os.path.isfile(path):
                item.setText(stamp + '\n文件已移除')
            self.list.addItem(item)
        count = (len(self._items) + self.PAGE_SIZE - 1) // self.PAGE_SIZE
        self.pages.setText(f'{self._page + 1 if count else 0} / {count}')
        self.previous.setEnabled(self._page > 0)
        self.next.setEnabled(self._page + 1 < count)
        self.count.setText(str(len(self._items)))
        self.count.setToolTip(f'共 {len(self._items)} 张图片；每页最多显示 {self.PAGE_SIZE} 张')
        self.pages.setToolTip(f'第 {self._page + 1 if count else 0} 页，共 {count} 页')
        self.stack.setCurrentIndex(0 if self._items else 1)
        self.hint.setVisible(bool(self._items))
        self.export_btn.setEnabled(bool(self._items) and self._export_job is None)

    def open(self, record):
        path = record.get('path', '')
        if os.path.isfile(path):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def menu(self, point):
        item = self.list.itemAt(point)
        if item is None:
            return
        menu = self._create_menu(item.data(QtCore.Qt.UserRole))
        menu.exec_(self.list.viewport().mapToGlobal(point))
        menu.deleteLater()

    def _reuse_click(self, record, modifiers):
        if not isinstance(record, dict):
            return
        ctrl = bool(modifiers & QtCore.Qt.ControlModifier)
        shift = bool(modifiers & QtCore.Qt.ShiftModifier)
        if ctrl and shift and (record.get('settings') or record.get('seed') is not None):
            self.reuseAllRequested.emit(copy.deepcopy(record))
        elif ctrl and isinstance(record.get('settings'), dict) and record['settings']:
            self.reuseSettingsRequested.emit(copy.deepcopy(record))
        elif shift and not ctrl and record.get('seed') is not None:
            self.reuseSeedRequested.emit(copy.deepcopy(record))

    def _create_menu(self, record):
        menu = QtWidgets.QMenu(self)
        menu.addAction('打开图片', lambda: self.open(record))
        menu.addAction('打开文件位置', lambda: self.reveal(record))
        menu.addSeparator()
        settings = record.get('settings') if isinstance(record.get('settings'), dict) else {}
        params = menu.addAction('复用参数（保留种子和素材）', lambda: self._reuse_click(record, QtCore.Qt.ControlModifier))
        params.setEnabled(bool(settings))
        seed = menu.addAction('回填种子', lambda: self._reuse_click(record, QtCore.Qt.ShiftModifier))
        seed.setEnabled(record.get('seed') is not None)
        both = menu.addAction('复用参数与种子（保留素材）', lambda: self._reuse_click(record, QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier))
        both.setEnabled(bool(settings) or record.get('seed') is not None)
        menu.addSeparator()
        menu.addAction('作为底图', lambda: self.useImageRequested.emit(copy.deepcopy(record)))
        copy_seed = menu.addAction('复制种子', lambda: QtWidgets.QApplication.clipboard().setText(str(record.get('seed', ''))))
        copy_seed.setEnabled(record.get('seed') is not None)
        menu.addAction('复制提示词', lambda: QtWidgets.QApplication.clipboard().setText(str(settings.get('prompt', ''))))
        menu.addSeparator()
        menu.addAction('从历史移除（保留文件）', lambda: self.remove_record(record))
        return menu

    def reveal(self, record):
        from aetherloom_core.platform_utils import reveal_in_file_manager_async
        reveal_in_file_manager_async([record.get('path', '')], self,
                                    lambda message: QtWidgets.QMessageBox.warning(self, '无法定位文件', message))

    def remove_record(self, record):
        self._items = [v for v in self._items if v.get('id') != record.get('id')]
        self.turn(0)
        self.itemsChanged.emit(self.items())

    def export_zip(self):
        if self._export_job is not None or not self._items:
            return
        target, _ = QtWidgets.QFileDialog.getSaveFileName(self, '导出历史图片', 'NovelAI-images.zip', 'ZIP (*.zip)')
        if not target:
            return
        paths = [v.get('path', '') for v in self._items]
        def operation(job):
            destination = Path(target)
            staging = destination.with_name(destination.name + '.part')
            try:
                with zipfile.ZipFile(staging, 'w', compression=zipfile.ZIP_STORED) as archive:
                    for i, path in enumerate(paths):
                        if job.stop.is_set():
                            raise InterruptedError('已停止导出')
                        if os.path.isfile(path):
                            archive.write(path, f'{i+1:04d}_{Path(path).name}')
                os.replace(staging, destination)
            finally:
                staging.unlink(missing_ok=True)
            return target
        job = self._export_job = Job(operation, self)
        self.export_btn.setEnabled(False)
        job.succeeded.connect(lambda path: QtWidgets.QMessageBox.information(self, '导出完成', '图片已保存至所选 ZIP 文件。'))
        job.failed.connect(lambda error: QtWidgets.QMessageBox.warning(self, '导出失败', str(error)))
        job.finished.connect(self._export_finished)
        job.start()

    def _export_finished(self):
        job, self._export_job = self._export_job, None
        self.export_btn.setEnabled(bool(self._items))
        if job is not None:
            job.deleteLater()

    def apply_theme(self, mode):
        pass  # All controls inherit the workspace's scoped theme.

    def shutdown(self):
        if self._export_job is not None:
            self._export_job.cancel()
