"""Image comparison window and synchronized zoom/pan controls."""
import math
import os
import weakref
import cv2
from PIL import Image
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt
try:
    import sip
except Exception:
    sip = None
from aetherloom_core.paths import current_dir
from aetherloom_core.resources import VIDEO_EXTS
from aetherloom_core.platform_utils import _set_native_titlebar_dark
from aetherloom_core.ui.widgets import DropLabel

class CompareSyncController(QtCore.QObject):
    """Synchronize zoom/pan state across multiple ComparePreviewLabel instances."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._views = []
        self._state = None
        self._updating = False

    def _cleanup(self):
        self._views = [ref for ref in self._views if ref() is not None]

    def register_view(self, view):
        self._cleanup()
        self._views.append(weakref.ref(view))
        if self._state:
            view.apply_shared_state(self._state, from_sync=True)

    def unregister_view(self, view):
        self._views = [ref for ref in self._views if ref() not in (None, view)]

    def update_state(self, source, state):
        if not state:
            return
        self._state = state
        if self._updating:
            return
        self._updating = True
        try:
            for ref in list(self._views):
                target = ref()
                if target is None:
                    try:
                        self._views.remove(ref)
                    except ValueError:
                        pass
                    continue
                if target is source:
                    continue
                target.apply_shared_state(state, from_sync=True)
        finally:
            self._updating = False

    def reset(self):
        self._state = None
        self._cleanup()


class ComparePreviewLabel(DropLabel):
    """Preview widget used inside the compare window with synced interactions."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.compare_controller = None
        self._selection_origin = None
        self._rubber_band = None
        self._suspend_sync = False
        self._compare_tile = None

    def set_controller(self, controller):
        self.compare_controller = controller
        if controller:
            controller.register_view(self)
            self.destroyed.connect(lambda *_: controller.unregister_view(self))

    def set_container(self, tile):
        self._compare_tile = tile

    def _ensure_rubber_band(self):
        if self._rubber_band is None:
            self._rubber_band = QtWidgets.QRubberBand(QtWidgets.QRubberBand.Rectangle, self)

    def set_base_pixmap(self, pixmap: QtGui.QPixmap):
        super().set_base_pixmap(pixmap)
        if self.compare_controller and getattr(self.compare_controller, '_state', None):
            self.apply_shared_state(self.compare_controller._state, from_sync=True)
        else:
            self._notify_sync()

    def wheelEvent(self, event):
        super().wheelEvent(event)
        self._notify_sync()

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton and self._compare_tile is not None:
            drag = QtGui.QDrag(self)
            mime = QtCore.QMimeData()
            mime.setData('application/x-compare-index', str(self._compare_tile.index).encode('utf-8'))
            drag.setMimeData(mime)
            try:
                drag.setPixmap(self._compare_tile.grab())
            except Exception:
                pass
            drag.exec_(Qt.MoveAction)
            event.accept()
            return
        if event.button() == Qt.RightButton:
            self._selection_origin = event.pos()
            self._ensure_rubber_band()
            try:
                self._rubber_band.setGeometry(QtCore.QRect(self._selection_origin, QtCore.QSize()))
                self._rubber_band.show()
            except Exception:
                pass
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.RightButton and self._selection_origin is not None:
            self._ensure_rubber_band()
            rect = QtCore.QRect(self._selection_origin, event.pos()).normalized()
            try:
                self._rubber_band.setGeometry(rect)
            except Exception:
                pass
            event.accept()
            return
        super().mouseMoveEvent(event)
        if event.buttons() & Qt.LeftButton:
            self._notify_sync()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton and self._selection_origin is not None:
            rect = QtCore.QRect(self._selection_origin, event.pos()).normalized()
            self._selection_origin = None
            if self._rubber_band:
                self._rubber_band.hide()
            if rect.width() > 12 and rect.height() > 12:
                self._zoom_to_rect(rect)
            event.accept()
            return
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton:
            self._notify_sync()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.compare_controller and getattr(self.compare_controller, '_state', None):
            self.apply_shared_state(self.compare_controller._state, from_sync=True)

    def _ensure_fit_scale_value(self):
        if getattr(self, '_fit_scale', None) is not None:
            return
        if not hasattr(self, '_base_pixmap') or self._base_pixmap is None:
            self._fit_scale = 1.0
            return
        bw = max(1, self._base_pixmap.width())
        bh = max(1, self._base_pixmap.height())
        self._fit_scale = min(max(1, self.width()) / bw, max(1, self.height()) / bh)
        if not math.isfinite(self._fit_scale) or self._fit_scale <= 0:
            self._fit_scale = 1.0

    def _calc_normalized_center(self):
        if not hasattr(self, '_base_pixmap') or self._base_pixmap is None:
            return None
        self._ensure_fit_scale_value()
        bw = max(1, self._base_pixmap.width())
        bh = max(1, self._base_pixmap.height())
        total_scale = max(0.0001, self._fit_scale * getattr(self, '_zoom', 1.0))
        ox = getattr(self, '_origin_x', 0)
        oy = getattr(self, '_origin_y', 0)
        cx = ((self.width() / 2) - ox) / (bw * total_scale)
        cy = ((self.height() / 2) - oy) / (bh * total_scale)
        cx = max(0.0, min(1.0, cx))
        cy = max(0.0, min(1.0, cy))
        return (cx, cy)

    def _apply_center(self, center):
        if not hasattr(self, '_base_pixmap') or self._base_pixmap is None:
            return
        self._ensure_fit_scale_value()
        bw = max(1, self._base_pixmap.width())
        bh = max(1, self._base_pixmap.height())
        total_scale = max(0.0001, self._fit_scale * getattr(self, '_zoom', 1.0))
        cx = max(0.0, min(1.0, center[0]))
        cy = max(0.0, min(1.0, center[1]))
        target_x = cx * bw * total_scale
        target_y = cy * bh * total_scale
        self._origin_x = int(self.width() / 2 - target_x)
        self._origin_y = int(self.height() / 2 - target_y)

    def _calc_visible_state(self):
        if not hasattr(self, '_base_pixmap') or self._base_pixmap is None:
            return None
        self._ensure_fit_scale_value()
        bw = max(1, self._base_pixmap.width())
        bh = max(1, self._base_pixmap.height())
        total_scale = max(0.0001, self._fit_scale * getattr(self, '_zoom', 1.0))
        ox = getattr(self, '_origin_x', 0)
        oy = getattr(self, '_origin_y', 0)

        def norm(ptx, pty):
            ix = (ptx - ox) / (bw * total_scale)
            iy = (pty - oy) / (bh * total_scale)
            return ix, iy

        x1, y1 = norm(0, 0)
        x2, y2 = norm(self.width(), self.height())
        span_x = max(0.0, min(1.0, x2 - x1))
        span_y = max(0.0, min(1.0, y2 - y1))
        cx = max(0.0, min(1.0, x1 + span_x / 2.0))
        cy = max(0.0, min(1.0, y1 + span_y / 2.0))
        return {'center': (cx, cy), 'span': (span_x, span_y)}

    def _build_state(self):
        metrics = self._calc_visible_state()
        if metrics is None:
            return None
        metrics['zoom'] = float(getattr(self, '_zoom', 1.0))
        return metrics

    def _notify_sync(self):
        if self.compare_controller is None or self._suspend_sync:
            return
        state = self._build_state()
        if state:
            self.compare_controller.update_state(self, state)

    def apply_shared_state(self, state, from_sync=False):
        if not state or not hasattr(self, '_base_pixmap') or self._base_pixmap is None:
            return
        center = state.get('center', (0.5, 0.5))
        span = state.get('span')
        self._suspend_sync = True
        try:
            self._ensure_fit_scale_value()
            bw = max(1, self._base_pixmap.width())
            bh = max(1, self._base_pixmap.height())
            if span and len(span) == 2 and span[0] > 0 and span[1] > 0:
                total_scale_x = self.width() / max(1e-6, span[0] * bw)
                total_scale_y = self.height() / max(1e-6, span[1] * bh)
                total_scale = min(total_scale_x, total_scale_y)
                if not math.isfinite(total_scale) or total_scale <= 0:
                    total_scale = self._fit_scale or 1.0
                self._zoom = max(0.2, min(6.0, total_scale / max(0.0001, self._fit_scale)))
            elif 'zoom' in state:
                self._zoom = max(0.2, min(6.0, float(state.get('zoom', 1.0))))
            self._apply_center(center)
            self._update_display()
        finally:
            self._suspend_sync = False
        if not from_sync:
            self._notify_sync()

    def _zoom_to_rect(self, rect):
        if not hasattr(self, '_base_pixmap') or self._base_pixmap is None:
            return
        self._ensure_fit_scale_value()
        bw = max(1, self._base_pixmap.width())
        bh = max(1, self._base_pixmap.height())
        total_scale = max(0.0001, self._fit_scale * getattr(self, '_zoom', 1.0))
        ox = getattr(self, '_origin_x', 0)
        oy = getattr(self, '_origin_y', 0)
        ix1 = (rect.left() - ox) / total_scale
        iy1 = (rect.top() - oy) / total_scale
        ix2 = (rect.right() - ox) / total_scale
        iy2 = (rect.bottom() - oy) / total_scale
        ix1 = max(0, min(bw, ix1))
        iy1 = max(0, min(bh, iy1))
        ix2 = max(0, min(bw, ix2))
        iy2 = max(0, min(bh, iy2))
        sel_w = max(1.0, abs(ix2 - ix1))
        sel_h = max(1.0, abs(iy2 - iy1))
        target_scale = min(self.width() / sel_w, self.height() / sel_h)
        if target_scale <= 0:
            return
        new_zoom = target_scale / max(0.0001, self._fit_scale)
        new_zoom = max(0.2, min(6.0, new_zoom))
        center_x = ((ix1 + ix2) / 2.0) / bw
        center_y = ((iy1 + iy2) / 2.0) / bh
        self._zoom = new_zoom
        self._apply_center((center_x, center_y))
        self._update_display()
        self._notify_sync()


class ComparePixmapSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(str, bytes)


class ComparePixmapJob(QtCore.QRunnable):
    def __init__(self, path, max_edge=2048):
        super().__init__()
        self.path = path
        self.max_edge = max_edge
        self.signals = ComparePixmapSignals()

    def run(self):
        data = b''
        try:
            if self.path.lower().endswith(VIDEO_EXTS):
                if self.path.lower().endswith('.gif'):
                    frame = Image.open(self.path).copy().convert('RGB')
                else:
                    cap = cv2.VideoCapture(self.path)
                    ret, fr = cap.read()
                    cap.release()
                    if not ret:
                        frame = Image.new('RGB', (512, 512), (0, 0, 0))
                    else:
                        frame = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
            else:
                frame = Image.open(self.path).convert('RGB')
            w, h = frame.size
            max_dim = max(w, h)
            if max_dim > self.max_edge:
                scale = self.max_edge / max_dim
                frame = frame.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            from io import BytesIO
            buf = BytesIO()
            frame.save(buf, format='PNG')
            data = buf.getvalue()
        except Exception:
            data = b''
        try:
            self.signals.finished.emit(self.path, data)
        except Exception:
            pass


class CompareTile(QtWidgets.QFrame):
    """Single cell inside the compare window."""
    def __init__(self, path, controller, owner, index, tile_size, x_label='', y_label=''):
        super().__init__()
        self.path = path
        self.index = index
        self.owner = owner
        self._tile_size = tile_size
        self.setAcceptDrops(True)
        self.setObjectName('compareTile')
        self.setStyleSheet('QFrame#compareTile { background: #0f1720; border: 1px solid #1f2a37; border-radius: 10px; }')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        self.tag_label = QtWidgets.QLabel('')
        self.tag_label.setAlignment(Qt.AlignCenter)
        self.tag_label.setStyleSheet('color: #d1d5db; font-size: 10pt; font-weight: 600;')
        layout.addWidget(self.tag_label)
        self.preview = ComparePreviewLabel(alignment=Qt.AlignCenter)
        self.preview.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        min_w = max(200, int(tile_size.width() * 0.6))
        min_h = max(150, int(tile_size.height() * 0.6))
        self.preview.setMinimumSize(min_w, min_h)
        self.preview.setStyleSheet('background:#000000; border-radius:8px;')
        self.preview.set_controller(controller)
        self.preview.set_container(self)
        layout.addWidget(self.preview, 1)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.set_labels(x_label, y_label)
        self._placeholder_active = False
        self._pix_requested = False

    def sizeHint(self):
        return QtCore.QSize(self._tile_size.width() + 24, self._tile_size.height() + 48)

    def minimumSizeHint(self):
        return QtCore.QSize(max(220, int(self._tile_size.width() * 0.6)), max(180, int(self._tile_size.height() * 0.6)))

    def refresh_pixmap(self):
        if self.owner is not None:
            self.preview.setText('加载中...')
            self.owner.request_pixmap(self.path, self)

    def ensure_pixmap(self, force=False):
        if self.owner is None:
            return
        if not force and self._pix_requested:
            return
        self._pix_requested = True
        self.refresh_pixmap()

    def set_placeholder(self, pix):
        if pix is None:
            return
        self._placeholder_active = True
        self.preview.setText('')
        self.preview.set_base_pixmap(pix)

    def set_pixmap(self, pix):
        if pix is not None:
            self._placeholder_active = False
            self.preview.setText('')
            self.preview.set_base_pixmap(pix)
        else:
            if self._placeholder_active:
                return
            self.preview.setText('加载失败')

    def set_labels(self, x_label, y_label):
        parts = [lbl for lbl in (y_label, x_label) if lbl]
        txt = ' / '.join(parts)
        self.tag_label.setVisible(bool(txt))
        self.tag_label.setText(txt)

    def set_index(self, idx):
        self.index = idx

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat('application/x-compare-index'):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat('application/x-compare-index'):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasFormat('application/x-compare-index'):
            try:
                payload = bytes(event.mimeData().data('application/x-compare-index')).decode('utf-8')
                src = int(payload)
            except Exception:
                src = None
            if src is not None:
                self.owner.swap_items(src, self.index)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class CompareWindow(QtWidgets.QMainWindow):
    """Floating window that shows side-by-side previews for quick comparison."""
    def __init__(self, host=None):
        super().__init__(host)
        self.setWindowTitle('比较预览')
        self.resize(1200, 800)
        try:
            self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        except Exception:
            pass
        self._host = host
        self.paths = []
        self.rows = 1
        self.cols = 2
        self.x_labels = []
        self.y_labels = []
        self.tiles = []
        self.sync_controller = CompareSyncController(self)
        self.base_tile_edge = 420
        self.tile_size = QtCore.QSize(self.base_tile_edge, int(self.base_tile_edge * 0.75))
        self._dim_cache = {}
        from collections import OrderedDict
        self._pixmap_cache = OrderedDict()
        self._pixmap_cache_max = 48
        self._pending_tiles = {}
        self._lowres_cache = OrderedDict()
        self._lowres_cache_max = 160
        self._lowres_edge = 512
        self._thumb_cache_dir = getattr(self._host, '_thumb_cache_dir', None)
        self._thumb_key_func = getattr(self._host, '_get_thumb_key', None)
        self._compare_pool = QtCore.QThreadPool(self)
        try:
            cpu = max(2, (os.cpu_count() or 2))
            self._compare_pool.setMaxThreadCount(max(2, min(4, cpu // 2)))
        except Exception:
            self._compare_pool.setMaxThreadCount(3)
        self._setup_ui()
        self.sync_theme()

    def _setup_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        controls_frame = QtWidgets.QFrame()
        controls_frame.setObjectName('compareControls')
        controls_frame.setStyleSheet(
            'QFrame#compareControls { background: #111823; border: 1px solid #1f2a37; border-radius: 10px; }'
            'QFrame#compareControls QLabel { color: #e5e7eb; font-weight: 600; }'
            'QFrame#compareControls QSpinBox, QFrame#compareControls QLineEdit {'
            ' background: #0b111a; color: #f8fafc; border: 1px solid #1f2a37; border-radius: 6px; padding: 4px 6px; }'
            'QFrame#compareControls QPushButton { background: #2563eb; color: #ffffff; border: none; border-radius: 6px; padding: 8px 14px; }'
            'QFrame#compareControls QPushButton:hover { background: #1d4ed8; }'
        )
        controls = QtWidgets.QGridLayout(controls_frame)
        controls.setContentsMargins(16, 12, 16, 12)
        controls.setHorizontalSpacing(12)
        controls.setVerticalSpacing(8)
        layout.addWidget(controls_frame)

        self.save_btn = QtWidgets.QPushButton('保存当前比较')
        self.save_btn.clicked.connect(self._save_snapshot)
        controls.addWidget(self.save_btn, 0, 0)

        self.help_btn = QtWidgets.QPushButton('使用说明')
        self.help_btn.clicked.connect(self._show_compare_help)
        controls.addWidget(self.help_btn, 0, 6)

        controls.addWidget(QtWidgets.QLabel('排版 (行×列):'), 0, 1)
        self.rows_spin = QtWidgets.QSpinBox()
        self.rows_spin.setRange(1, 12)
        self.rows_spin.setValue(self.rows)
        controls.addWidget(self.rows_spin, 0, 2)
        times_lbl = QtWidgets.QLabel('×')
        times_lbl.setAlignment(Qt.AlignCenter)
        controls.addWidget(times_lbl, 0, 3)
        self.cols_spin = QtWidgets.QSpinBox()
        self.cols_spin.setRange(1, 12)
        self.cols_spin.setValue(self.cols)
        controls.addWidget(self.cols_spin, 0, 4)
        self.layout_apply_btn = QtWidgets.QPushButton('应用排版')
        self.layout_apply_btn.clicked.connect(self._apply_layout_from_controls)
        controls.addWidget(self.layout_apply_btn, 0, 5)

        controls.addWidget(QtWidgets.QLabel('X 标签 (逗号分隔):'), 1, 0)
        self.x_labels_edit = QtWidgets.QLineEdit()
        self.x_labels_edit.setPlaceholderText('例如: Prompt1,Prompt2')
        self.x_labels_edit.setMinimumWidth(220)
        self.x_labels_edit.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        controls.addWidget(self.x_labels_edit, 1, 1, 1, 2)
        controls.addWidget(QtWidgets.QLabel('Y 标签 (逗号分隔):'), 1, 3)
        self.y_labels_edit = QtWidgets.QLineEdit()
        self.y_labels_edit.setPlaceholderText('例如: CFG7,CFG11')
        self.y_labels_edit.setMinimumWidth(220)
        self.y_labels_edit.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        controls.addWidget(self.y_labels_edit, 1, 4, 1, 2)
        self.labels_apply_btn = QtWidgets.QPushButton('应用标注')
        self.labels_apply_btn.clicked.connect(self._apply_labels_from_controls)
        controls.addWidget(self.labels_apply_btn, 1, 6)
        controls.setColumnStretch(1, 1)
        controls.setColumnStretch(2, 1)
        controls.setColumnStretch(4, 1)
        controls.setColumnStretch(5, 1)
        controls.setColumnStretch(6, 0)

        self.scroll_area = QtWidgets.QScrollArea()
        self.scroll_area.setObjectName('compareScroll')
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._tile_enqueue_timer = QtCore.QTimer(self)
        self._tile_enqueue_timer.setSingleShot(True)
        self._tile_enqueue_timer.setInterval(80)
        try:
            self._tile_enqueue_timer.timeout.connect(self._enqueue_visible_tiles)
        except Exception:
            pass
        try:
            vbar = self.scroll_area.verticalScrollBar()
            hbar = self.scroll_area.horizontalScrollBar()
            vbar.valueChanged.connect(lambda *_: self._schedule_tile_refresh())
            hbar.valueChanged.connect(lambda *_: self._schedule_tile_refresh())
            vbar.rangeChanged.connect(lambda *_: self._schedule_tile_refresh())
            hbar.rangeChanged.connect(lambda *_: self._schedule_tile_refresh())
        except Exception:
            pass
        layout.addWidget(self.scroll_area, 1)
        self.grid_widget = QtWidgets.QWidget()
        self.grid_widget.setObjectName('compareGrid')
        self.scroll_area.setWidget(self.grid_widget)
        try:
            self.scroll_area.viewport().installEventFilter(self)
        except Exception:
            pass
        self.grid_layout = QtWidgets.QGridLayout(self.grid_widget)
        self.grid_layout.setContentsMargins(6, 6, 6, 6)
        self.grid_layout.setHorizontalSpacing(6)
        self.grid_layout.setVerticalSpacing(6)

    def sync_theme(self, mode=None):
        try:
            if mode is None and self._host is not None:
                mode = getattr(self._host, '_theme_mode', 'dark')
            if mode is None:
                mode = 'dark'
            _set_native_titlebar_dark(self, mode == 'dark')
        except Exception:
            pass

    def _show_compare_help(self):
        text = (
            '比较窗口使用说明:\n'
            '- 鼠标中键拖动一个预览到另一个上方即可交换两者位置。\n'
            '- 鼠标滚轮缩放，拖动预览即可平移视图。\n'
            '- 按住右键拖出矩形后松开，可对该区域放大查看。\n'
            '- 顶部可设置排版行列、轴向标签，并保存当前拼图。'
        )
        QtWidgets.QMessageBox.information(self, '使用说明', text, QtWidgets.QMessageBox.Ok)

    def _probe_dimensions(self, path):
        cached = self._dim_cache.get(path)
        if cached:
            return cached
        w = h = None
        try:
            lower = path.lower()
            if lower.endswith(VIDEO_EXTS):
                cap = cv2.VideoCapture(path)
                if cap.isOpened():
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                cap.release()
            else:
                with Image.open(path) as img:
                    w, h = img.size
        except Exception:
            w = h = None
        if w and h:
            self._dim_cache[path] = (w, h)
        else:
            self._dim_cache[path] = (None, None)
        return self._dim_cache[path]

    def _detect_common_ratio(self):
        ratios = []
        for p in self.paths:
            w, h = self._probe_dimensions(p)
            if not w or not h:
                continue
            ratios.append(w / h)
        if not ratios:
            return None
        ref = ratios[0]
        eps = 0.01
        for r in ratios[1:]:
            if abs(r - ref) > eps:
                return None
        return ref

    def _compute_tile_size(self):
        ratio = self._detect_common_ratio()
        base = self.base_tile_edge
        if ratio and math.isfinite(ratio) and ratio > 0:
            if ratio >= 1.0:
                width = base
                height = max(220, int(base / ratio))
            else:
                height = base
                width = max(220, int(base * ratio))
            return QtCore.QSize(width, height)
        return QtCore.QSize(base, int(base * 0.75))

    def add_paths(self, paths):
        added = False
        for p in paths:
            if not p or not os.path.exists(p):
                continue
            self.paths.append(p)
            added = True
        if not added:
            if self._host:
                try:
                    self._host.log('没有可加入比较的文件')
                except Exception:
                    pass
            return
        self._auto_expand_layout()
        self._refresh_grid()

    def _auto_expand_layout(self):
        while self.rows * self.cols < max(1, len(self.paths)):
            if self.cols <= self.rows:
                self.cols += 1
            else:
                self.rows += 1
        self._sync_spins()

    def _sync_spins(self):
        for spin, value in ((self.rows_spin, self.rows), (self.cols_spin, self.cols)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    def _apply_layout_from_controls(self):
        self.rows = max(1, self.rows_spin.value())
        self.cols = max(1, self.cols_spin.value())
        self._auto_expand_layout()
        self._refresh_grid()

    def _apply_labels_from_controls(self):
        self.x_labels = [lbl.strip() for lbl in self.x_labels_edit.text().split(',') if lbl.strip()]
        self.y_labels = [lbl.strip() for lbl in self.y_labels_edit.text().split(',') if lbl.strip()]
        self._refresh_grid()

    def _has_x_labels(self):
        return any(self.x_labels)

    def _has_y_labels(self):
        return any(self.y_labels)

    def _make_axis_label(self, text, axis):
        lbl = QtWidgets.QLabel(text)
        lbl.setStyleSheet('color:#4b5563; font-weight:600;')
        if axis == 'x':
            lbl.setAlignment(Qt.AlignCenter)
        else:
            lbl.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
        return lbl

    def _refresh_grid(self):
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.tiles = []
        self.tile_size = self._compute_tile_size()
        show_x = self._has_x_labels()
        show_y = self._has_y_labels()
        row_offset = 1 if show_x else 0
        col_offset = 1 if show_y else 0
        total_rows = row_offset + self.rows
        total_cols = col_offset + self.cols
        for r in range(max(1, total_rows)):
            self.grid_layout.setRowStretch(r, 0)
        for c in range(max(1, total_cols)):
            self.grid_layout.setColumnStretch(c, 0)
        if show_x and show_y:
            spacer = QtWidgets.QLabel('')
            self.grid_layout.addWidget(spacer, 0, 0)
        if show_x:
            for c in range(self.cols):
                text = self.x_labels[c] if c < len(self.x_labels) else ''
                lbl = self._make_axis_label(text, axis='x')
                self.grid_layout.addWidget(lbl, 0, c + col_offset)
        if show_y:
            for r in range(self.rows):
                text = self.y_labels[r] if r < len(self.y_labels) else ''
                lbl = self._make_axis_label(text, axis='y')
                self.grid_layout.addWidget(lbl, r + row_offset, 0)
        for idx, path in enumerate(self.paths):
            row = idx // self.cols
            col = idx % self.cols
            x_label = self.x_labels[col] if col < len(self.x_labels) else ''
            y_label = self.y_labels[row] if row < len(self.y_labels) else ''
            tile = CompareTile(path, self.sync_controller, self, idx, self.tile_size, x_label, y_label)
            self.grid_layout.addWidget(tile, row + row_offset, col + col_offset)
            self.tiles.append(tile)
        for r in range(self.rows):
            self.grid_layout.setRowStretch(r + row_offset, 1)
        for c in range(self.cols):
            self.grid_layout.setColumnStretch(c + col_offset, 1)
        self.grid_widget.adjustSize()
        hint = self.grid_widget.sizeHint()
        if hint.isValid():
            self.grid_widget.setMinimumSize(hint)
        self._schedule_tile_refresh()

    def swap_items(self, src, dest):
        if src == dest:
            return
        if src < 0 or src >= len(self.paths) or dest < 0 or dest >= len(self.paths):
            return
        self.paths[src], self.paths[dest] = self.paths[dest], self.paths[src]
        self._refresh_grid()

    def _schedule_tile_refresh(self):
        timer = getattr(self, '_tile_enqueue_timer', None)
        if timer is not None:
            try:
                timer.start()
            except Exception:
                pass

    def _enqueue_visible_tiles(self):
        if not self.tiles:
            return
        viewport = getattr(self.scroll_area, 'viewport', lambda: None)()
        if viewport is None:
            return
        try:
            visible_rect = QtCore.QRect(QtCore.QPoint(0, 0), viewport.size())
        except Exception:
            return
        expanded = visible_rect.adjusted(-120, -120, 120, 120)
        for tile in list(self.tiles):
            try:
                origin = tile.mapTo(viewport, QtCore.QPoint(0, 0))
                tile_rect = QtCore.QRect(origin, tile.size())
                if tile_rect.intersects(expanded):
                    tile.ensure_pixmap()
            except Exception:
                try:
                    tile.ensure_pixmap()
                except Exception:
                    pass

    def eventFilter(self, obj, event):
        try:
            viewport = self.scroll_area.viewport() if hasattr(self, 'scroll_area') else None
            if obj is viewport and event.type() in (
                QtCore.QEvent.Resize,
                QtCore.QEvent.Show,
                QtCore.QEvent.UpdateRequest,
                QtCore.QEvent.Paint
            ):
                self._schedule_tile_refresh()
        except Exception:
            pass
        try:
            return super().eventFilter(obj, event)
        except Exception:
            return False

    def resizeEvent(self, event):
        try:
            super().resizeEvent(event)
        finally:
            self._schedule_tile_refresh()

    def _trim_lowres_cache(self):
        try:
            while len(self._lowres_cache) > self._lowres_cache_max:
                self._lowres_cache.popitem(last=False)
        except Exception:
            pass

    def _load_placeholder_pixmap(self, path):
        pix = self._lowres_cache.get(path)
        if pix is not None:
            try:
                self._lowres_cache.move_to_end(path)
            except Exception:
                pass
            return pix
        cache_dir = self._thumb_cache_dir
        key_func = self._thumb_key_func
        if cache_dir and key_func:
            try:
                key = key_func(path, self._lowres_edge)
            except Exception:
                key = None
            if key:
                cache_path = os.path.join(cache_dir, key + '.png')
                if os.path.exists(cache_path):
                    pix = QtGui.QPixmap(cache_path)
                    if not pix.isNull():
                        self._lowres_cache[path] = pix
                        try:
                            self._lowres_cache.move_to_end(path)
                        except Exception:
                            pass
                        self._trim_lowres_cache()
                        return pix
        return None

    def _apply_placeholder(self, path, tile):
        pix = self._load_placeholder_pixmap(path)
        if pix is None:
            return False
        preview = getattr(tile, 'preview', None)
        target = preview.size() if preview is not None else QtCore.QSize()
        if not target.isValid() or target.width() < 32 or target.height() < 32:
            target = self.tile_size if isinstance(self.tile_size, QtCore.QSize) else QtCore.QSize(320, 240)
        try:
            scaled = pix.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        except Exception:
            scaled = pix
        tile.set_placeholder(scaled)
        return True

    def _load_compare_image(self, path):
        try:
            lower = path.lower()
            if lower.endswith(VIDEO_EXTS):
                if lower.endswith('.gif'):
                    img = Image.open(path)
                    return img.copy().convert('RGB')
                cap = cv2.VideoCapture(path)
                ret, frame = cap.read()
                cap.release()
                if not ret or frame is None:
                    return Image.new('RGB', (512, 512), (0, 0, 0))
                return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            return Image.open(path).convert('RGB')
        except Exception:
            return Image.new('RGB', (512, 512), (12, 12, 12))

    def _render_highres_snapshot(self, save_path):
        if not self.paths:
            return False
        images = []
        for path in self.paths:
            img = self._load_compare_image(path)
            images.append(img)
        col_widths = [0] * self.cols
        row_heights = [0] * self.rows
        for idx, img in enumerate(images):
            row = idx // self.cols
            col = idx % self.cols
            if row >= self.rows:
                break
            col_widths[col] = max(col_widths[col], img.width)
            row_heights[row] = max(row_heights[row], img.height)
        col_widths = [w if w > 0 else 256 for w in col_widths]
        row_heights = [h if h > 0 else 256 for h in row_heights]
        spacing = 40
        header_h = 0
        sidebar_w = 0
        label_font = None
        draw_module = None
        font_module = None
        try:
            from PIL import ImageDraw as _ImageDraw, ImageFont as _ImageFont
            draw_module = _ImageDraw
            font_module = _ImageFont
        except Exception:
            draw_module = None
            font_module = None

        def _try_load_font(font_mod, size=36):
            if font_mod is None:
                return None
            candidates = [
                'msyh.ttc', 'msyh.ttf', 'msyh.ttf', 'msyh.ttf',
                'simhei.ttf', 'simsun.ttc', 'simsun.ttf',
                'Arial Unicode.ttf', 'arialuni.ttf', 'NotoSansCJK-Regular.ttc',
                'DejaVuSans.ttf', 'arial.ttf'
            ]
            for name in candidates:
                try:
                    f = font_mod.truetype(name, size)
                    return f
                except Exception:
                    continue
            try:
                return font_mod.load_default()
            except Exception:
                return None

        if font_module:
            label_font = _try_load_font(font_module, 36)
        if self._has_x_labels():
            header_h = 90
        if self._has_y_labels():
            sidebar_w = 200
        total_w = max(1, sidebar_w + spacing + sum(col_widths) + spacing * (self.cols - 1))
        total_h = max(1, header_h + spacing + sum(row_heights) + spacing * (self.rows - 1))
        canvas = Image.new('RGB', (total_w, total_h), (8, 12, 20))
        draw = draw_module.Draw(canvas) if draw_module else None
        def _measure_text(draw_obj, text, font):
            try:
                if draw_obj is not None:
                    if hasattr(draw_obj, 'textbbox'):
                        bbox = draw_obj.textbbox((0, 0), text, font=font)
                        return (bbox[2] - bbox[0], bbox[3] - bbox[1])
                    if hasattr(draw_obj, 'textsize'):
                        return draw_obj.textsize(text, font=font)
                if font is not None:
                    if hasattr(font, 'getsize'):
                        return font.getsize(text)
                    if hasattr(font, 'getbbox'):
                        bbox = font.getbbox(text)
                        return (bbox[2] - bbox[0], bbox[3] - bbox[1])
            except Exception:
                pass
            return (len(text) * 8, 16)

        if self._has_x_labels() and draw:
            for c in range(self.cols):
                label = self.x_labels[c] if c < len(self.x_labels) else ''
                if not label:
                    continue
                x_offset = sidebar_w + spacing + sum(col_widths[:c]) + spacing * c
                col_w = col_widths[c]
                text_x = x_offset + col_w // 2
                text_y = header_h - 60
                tw, th = _measure_text(draw, label, label_font)
                draw.text((text_x - tw / 2, max(10, text_y)), label, fill=(238, 242, 255), font=label_font)
        if self._has_y_labels() and draw:
            for r in range(self.rows):
                label = self.y_labels[r] if r < len(self.y_labels) else ''
                if not label:
                    continue
                y_offset = header_h + spacing + sum(row_heights[:r]) + spacing * r
                row_h = row_heights[r]
                text_y = y_offset + row_h // 2
                tw, th = _measure_text(draw, label, label_font)
                draw.text((max(10, sidebar_w - tw - 20), text_y - th / 2), label, fill=(238, 242, 255), font=label_font)
        for idx, img in enumerate(images):
            row = idx // self.cols
            col = idx % self.cols
            if row >= self.rows or col >= self.cols:
                continue
            x = sidebar_w + spacing + sum(col_widths[:col]) + spacing * col
            y = header_h + spacing + sum(row_heights[:row]) + spacing * row
            box_w = col_widths[col]
            box_h = row_heights[row]
            paste_x = x + max(0, (box_w - img.width) // 2)
            paste_y = y + max(0, (box_h - img.height) // 2)
            canvas.paste(img, (paste_x, paste_y))
        try:
            canvas.save(save_path)
            return True
        except Exception:
            return False

    def request_pixmap(self, path, tile):
        if path in self._pixmap_cache:
            pix = self._pixmap_cache[path]
            tile.set_pixmap(pix)
            try:
                self._pixmap_cache.move_to_end(path)
            except Exception:
                pass
            return
        try:
            self._apply_placeholder(path, tile)
        except Exception:
            pass
        watchers = self._pending_tiles.setdefault(path, [])
        if tile not in watchers:
            watchers.append(tile)
        if len(watchers) > 1:
            return
        job = ComparePixmapJob(path, max_edge=4096)
        try:
            job.signals.finished.connect(self._on_pixmap_ready, QtCore.Qt.QueuedConnection)
        except Exception:
            job.signals.finished.connect(self._on_pixmap_ready)
        try:
            self._compare_pool.start(job)
        except Exception:
            job.run()

    def _on_pixmap_ready(self, path, data):
        watchers = self._pending_tiles.pop(path, [])
        pix = None
        if data:
            pixmap = QtGui.QPixmap()
            if pixmap.loadFromData(data, 'PNG'):
                pix = pixmap
                self._pixmap_cache[path] = pix
                try:
                    self._pixmap_cache.move_to_end(path)
                    while len(self._pixmap_cache) > self._pixmap_cache_max:
                        self._pixmap_cache.popitem(last=False)
                except Exception:
                    pass
        for tile in watchers:
            target = tile
            if target is None:
                continue
            try:
                if sip is not None and sip.isdeleted(target):
                    continue
            except Exception:
                pass
            try:
                target.set_pixmap(pix)
            except RuntimeError:
                continue

    def _content_rect(self):
        rect = None
        try:
            count = self.grid_layout.count()
        except Exception:
            count = 0
        for i in range(count):
            try:
                item = self.grid_layout.itemAt(i)
                if item is None:
                    continue
                w = item.widget()
                if w is None or not w.isVisible():
                    continue
                if isinstance(w, QtWidgets.QLabel) and not (w.text() or '').strip():
                    continue
                geo = w.geometry()
                rect = geo if rect is None else rect.united(geo)
            except Exception:
                continue
        return rect

    def _save_snapshot(self):
        try:
            rect = self._content_rect()
            if rect is not None and rect.isValid():
                pad = 8
                capture = rect.adjusted(-pad, -pad, pad, pad)
                capture = capture.intersected(self.grid_widget.rect())
                pix = self.grid_widget.grab(capture)
            else:
                pix = self.grid_widget.grab()
        except Exception:
            pix = None
        if pix is None:
            if self._host:
                try:
                    self._host.log('截取比较窗口失败')
                except Exception:
                    pass
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, '保存比较快照', os.path.join(current_dir, 'compare.png'), 'PNG (*.png)')
        if not path:
            return
        if not path.lower().endswith('.png'):
            path += '.png'
        use_highres = False
        try:
            if self.paths:
                box = QtWidgets.QMessageBox(self)
                box.setWindowTitle('选择保存方式')
                box.setText('保存比较图像时选择输出方式：')
                box.setIcon(QtWidgets.QMessageBox.Question)
                high_btn = box.addButton('原分辨率大图', QtWidgets.QMessageBox.AcceptRole)
                quick_btn = box.addButton('当前视图截图', QtWidgets.QMessageBox.ActionRole)
                cancel_btn = box.addButton(QtWidgets.QMessageBox.Cancel)
                box.exec_()
                clicked = box.clickedButton()
                if clicked is cancel_btn:
                    return
                use_highres = clicked is high_btn
        except Exception:
            use_highres = False
        if use_highres:
            if self._render_highres_snapshot(path):
                if self._host:
                    self._host.log(f'已保存原分辨率比较大图: {path}')
                return
            else:
                if self._host:
                    self._host.log('原分辨率渲染失败，改用视图截图')
        try:
            pix.save(path, 'PNG')
            if self._host:
                self._host.log(f'已保存比较快照: {path}')
        except Exception as e:
            if self._host:
                self._host.log(f'保存比较快照失败: {e}')
