"""Enhance setup using the public web magnitude presets and size choices."""
from PIL import Image
from PyQt5 import QtCore, QtWidgets
from . import catalog
from .references import decimal, guard_wheel
from .styles import workspace_stylesheet, workspace_palette

MAGNITUDES = ((.2, 0.), (.4, 0.), (.5, 0.), (.6, 0.), (.7, .1))
MAX_AREA = 3_145_728
MAX_ENHANCE_INPUT_AREA = MAX_AREA * .8


def available_scales(size, model):
    width, height = size
    if width <= 0 or height <= 0:
        return []
    if (width, height) in ((832, 1216), (1216, 832)):
        result = [1.5, 1.]
    else:
        result = [scale for scale in (2., 1.5, 1.)
                  if width * height * scale ** 2 <= MAX_AREA
                  and width * scale % 64 == 0 and height * scale % 64 == 0
                  and 64 <= width * scale <= 4096 and 64 <= height * scale <= 4096]
    if (catalog.capabilities(model).get('max_enhance') and width * height < MAX_ENHANCE_INPUT_AREA
            and 64 <= width <= 4096 and 64 <= height <= 4096):
        result.insert(0, 'max')
    return result


class EnhancementDialog(QtWidgets.QDialog):
    def __init__(self, path, model, parent=None, *, values=None, mode='dark'):
        super().__init__(parent)
        self.setObjectName('novelaiEnhancement')
        self.setWindowTitle('增强图像')
        self.resize(460, 330)
        self.setMinimumWidth(340)
        with Image.open(path) as image:
            self.source_size = image.size
            if image.getexif().get(274) in (5, 6, 7, 8):
                self.source_size = self.source_size[::-1]
        choices = available_scales(self.source_size, model)
        if not choices:
            raise ValueError('此图像尺寸不符合当前模型的增强条件，请先裁剪或调整尺寸。')
        values = values if isinstance(values, dict) else {}
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(12)
        title = QtWidgets.QLabel('增强当前图像')
        title.setStyleSheet('font-size:16px;font-weight:600;')
        box.addWidget(title)
        detail = QtWidgets.QLabel(f'{self.source_size[0]} × {self.source_size[1]} · 使用当前提示词和参考图')
        detail.setWordWrap(True)
        box.addWidget(detail)
        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self.scale = guard_wheel(QtWidgets.QComboBox())
        for choice in choices:
            self.scale.addItem('Max · 模型最大增强' if choice == 'max' else f'{choice:g}×', choice)
        remembered = self.scale.findData(values.get('scale'))
        if remembered >= 0:
            self.scale.setCurrentIndex(remembered)
        form.addRow('放大倍数', self.scale)
        self.resolution = QtWidgets.QLabel()
        self.resolution.setWordWrap(True)
        form.addRow('', self.resolution)
        box.addLayout(form)
        magnitude_row = QtWidgets.QHBoxLayout()
        magnitude_row.addWidget(QtWidgets.QLabel('增强幅度'))
        self.magnitude = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.magnitude.setRange(1, 5)
        self.magnitude.setTickPosition(QtWidgets.QSlider.TicksBelow)
        self.magnitude.setTickInterval(1)
        self.magnitude.setValue(max(1, min(5, int(values.get('magnitude', 1)))))
        self.magnitude.setAccessibleName('增强幅度，一到五')
        self.level = QtWidgets.QLabel()
        magnitude_row.addWidget(self.magnitude, 1)
        magnitude_row.addWidget(self.level)
        self.magnitude_row = QtWidgets.QWidget()
        self.magnitude_row.setLayout(magnitude_row)
        box.addWidget(self.magnitude_row)
        self.advanced = QtWidgets.QCheckBox('单独设置强度与噪声')
        self.advanced.setChecked(bool(values.get('advanced', False)))
        box.addWidget(self.advanced)
        self.individual = QtWidgets.QWidget()
        settings = QtWidgets.QFormLayout(self.individual)
        settings.setContentsMargins(0, 0, 0, 0)
        self.strength = decimal(.01, .99, float(values.get('strength', .2)), .01)
        self.noise = decimal(0., .99, float(values.get('noise', 0.)), .01)
        settings.addRow('强度', self.strength)
        settings.addRow('噪声', self.noise)
        box.addWidget(self.individual)
        note = QtWidgets.QLabel('增强会重新生成一张图像。应用后可检查参数，点击主页生成按钮才提交任务。')
        note.setWordWrap(True)
        box.addWidget(note)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(buttons.Ok).setText('应用增强设置')
        buttons.button(buttons.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        self.scale.currentIndexChanged.connect(self._refresh_size)
        self.magnitude.valueChanged.connect(self._magnitude_changed)
        self.advanced.toggled.connect(self._toggle_advanced)
        self._refresh_size()
        self.level.setText(str(self.magnitude.value()))
        self._toggle_advanced(self.advanced.isChecked())
        if not self.advanced.isChecked():
            self._magnitude_changed(self.magnitude.value())
        colors = workspace_palette(mode)
        self.setStyleSheet(workspace_stylesheet(mode, '#novelaiEnhancement') +
            f'QDialog#novelaiEnhancement {{background:{colors["surface"]};color:{colors["text"]};}}')

    def _refresh_size(self, *_):
        scale = self.scale.currentData()
        text = ('Max 由模型确定最终尺寸；与独立 2× 超分不同。' if scale == 'max'
                else f'{int(self.source_size[0] * scale)} × {int(self.source_size[1] * scale)}')
        self.resolution.setText(text)

    def _magnitude_changed(self, value):
        self.level.setText(str(value))
        strength, noise = MAGNITUDES[value - 1]
        self.strength.setValue(strength)
        self.noise.setValue(noise)

    def _toggle_advanced(self, enabled):
        self.individual.setVisible(enabled)
        self.magnitude_row.setVisible(not enabled)
        if not enabled:
            self._magnitude_changed(self.magnitude.value())

    def preferences(self):
        return dict(scale=self.scale.currentData(), magnitude=self.magnitude.value(),
                    advanced=self.advanced.isChecked(), strength=self.strength.value(), noise=self.noise.value())

    def options(self):
        scale = self.scale.currentData()
        width, height = self.source_size
        if scale != 'max':
            width, height = int(width * scale), int(height * scale)
        return dict(width=width, height=height, strength=self.strength.value(), noise=self.noise.value(),
                    upscaled_enhance=scale == 'max')
