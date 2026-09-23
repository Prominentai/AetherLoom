"""Simple non-destructive crop / canvas expansion before image generation."""
from pathlib import Path
from PIL import Image, ImageOps
from PyQt5 import QtCore, QtGui, QtWidgets
from aetherloom_core.mask_assets import MAX_PIXELS, store_asset
from aetherloom_core.rh_parameters import RhNumberSpinBox, RhEnumComboBox


class GeometryDialog(QtWidgets.QDialog):
    def __init__(self, path, input_dir, parent=None):
        super().__init__(parent)
        self.setWindowTitle('裁剪与扩图')
        self.path, self.input_dir, self.result_path = path, input_dir, ''
        with Image.open(path) as source:
            if source.width * source.height > MAX_PIXELS:
                raise ValueError('图片尺寸过大')
            self.image = ImageOps.exif_transpose(source).convert('RGBA')
        preview_image = self.image.copy()
        preview_image.thumbnail((440, 210), Image.Resampling.LANCZOS)
        self._preview_scale = preview_image.width / self.image.width
        preview_qimage = QtGui.QImage(preview_image.tobytes(), preview_image.width, preview_image.height,
                                     preview_image.width * 4, QtGui.QImage.Format_RGBA8888).copy()
        self._preview_pixmap = QtGui.QPixmap.fromImage(preview_qimage)
        self.resize(580, 490)
        self.setMinimumSize(360, 340)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(18, 18, 18, 18)
        self.mode = RhEnumComboBox()
        self.mode.addItem('裁剪指定矩形', 'crop')
        self.mode.addItem('扩展画布（新增区域作为遮罩）', 'expand')
        box.addWidget(self.mode)
        self.preview = QtWidgets.QLabel()
        self.preview.setMinimumHeight(140)
        self.preview.setAlignment(QtCore.Qt.AlignCenter)
        box.addWidget(self.preview, 1)
        form = QtWidgets.QFormLayout()
        self.fields = {}
        for key, label, value, maximum in [('x', '左侧 / X', 0, 8192), ('y', '顶部 / Y', 0, 8192),
                                            ('width', '宽度', self.image.width, 8192), ('height', '高度', self.image.height, 8192)]:
            widget = RhNumberSpinBox(integer=True)
            widget.configure({'min': 1 if key in ('width', 'height') else 0, 'max': maximum})
            widget.setValue(value)
            self.fields[key] = widget
            form.addRow(label, widget)
            widget.valueChanged.connect(self.refresh)
        box.addLayout(form)
        self.note = QtWidgets.QLabel('创建新的输入文件，保留原图。扩图模式的 X/Y 表示原图在新画布中的位置。')
        self.note.setWordWrap(True)
        box.addWidget(self.note)
        footer = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        footer.button(footer.Ok).setText('应用到输入')
        footer.accepted.connect(self.apply)
        footer.rejected.connect(self.reject)
        box.addWidget(footer)
        self.mode.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def transformed(self):
        x, y, width, height = (int(self.fields[key].value()) for key in ('x', 'y', 'width', 'height'))
        if width * height > MAX_PIXELS:
            raise ValueError('输出不能超过 3200 万像素')
        if self.mode.currentData() == 'crop':
            if x + width > self.image.width or y + height > self.image.height:
                raise ValueError('裁剪矩形超出原图，请调整宽高或起点。')
            return self.image.crop((x, y, x + width, y + height))
        if x + self.image.width > width or y + self.image.height > height:
            raise ValueError('新画布必须能完整容纳原图。')
        target = Image.new('RGBA', (width, height), (255, 255, 255, 0))
        target.paste(self.image, (x, y))
        return target

    def refresh(self, *_):
        if len(self.fields) < 4:
            return
        # Preview geometry on a small source, never allocate a large canvas per keystroke.
        pixmap = self._preview_pixmap.copy()
        if self.mode.currentData() == 'crop':
            painter = QtGui.QPainter(pixmap)
            painter.setPen(QtGui.QPen(QtGui.QColor('#4c8dff'), 2))
            scale = self._preview_scale
            rect = QtCore.QRectF(*(self.fields[key].value() * scale for key in ('x', 'y', 'width', 'height')))
            painter.drawRect(rect)
            painter.end()
        self.preview.setPixmap(pixmap)

    def apply(self):
        try:
            self.result_path = store_asset(self.transformed(), Path(self.input_dir) / 'novelai')
        except (OSError, ValueError) as error:
            QtWidgets.QMessageBox.warning(self, '无法应用', str(error))
            return
        self.accept()
