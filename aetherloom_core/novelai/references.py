"""Local reference-image editing; importing never calls a generation service."""
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
import hashlib
import math

from PIL import Image, ImageOps, UnidentifiedImageError

from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core.image_import import ImageDropFilter, import_mime
from .styles import workspace_stylesheet as app_stylesheet, workspace_palette as palette
from aetherloom_core.ui.design import CONTROL
from aetherloom_core.ui.widgets import _ComboWheelBlocker
from .catalog import reference_defaults
from .client import MAX_INPUT, MAX_PIXELS


def editor_stylesheet(mode, scope):
    """Shared restrained editor details, scoped to each embedded panel."""
    p = palette(mode)
    q = 'QWidget#' + scope
    return f'''
        {q} QLabel {{color:{p['muted']};}}
        {q} QLineEdit, {q} QTextEdit, {q} QPlainTextEdit,
        {q} QComboBox, {q} QAbstractSpinBox {{border-radius:8px;}}
        {q} QLineEdit:hover, {q} QComboBox:hover,
        {q} QAbstractSpinBox:hover {{border-color:{p['muted']};}}
        {q} QLineEdit:disabled, {q} QComboBox:disabled,
        {q} QAbstractSpinBox:disabled {{color:{p['muted']};background:{p['canvas']};}}
        {q} QTextEdit {{padding:8px 9px;}}
        {q} QAbstractSpinBox::up-button, {q} QAbstractSpinBox::down-button {{
            width:20px;border-left:0;background:transparent;}}
        {q} QAbstractSpinBox::up-button:hover, {q} QAbstractSpinBox::down-button:hover {{
            background:{p['accent_soft']};}}
        {q} QComboBox::drop-down {{width:26px;border-left:none;}}
        {q} QToolButton#novelaiIconButton {{padding:0;min-height:0;min-width:0;
            border:1px solid transparent;background:transparent;color:{p['muted']};border-radius:6px;}}
        {q} QToolButton#novelaiIconButton:hover {{background:{p['hover']};color:{p['text']};
            border-color:{p['border']};}}
        {q} QToolButton#novelaiIconButton[destructive="true"]:hover {{color:{p['danger']};}}
        {q} QPushButton#novelaiAddButton {{border:1px solid {p['border']};background:{p['accent_soft']};
            color:{p['accent']};font-weight:600;}}
        {q} QPushButton#novelaiAddButton:hover {{border-color:{p['accent']};}}
        {q} QPushButton#novelaiAddButton:disabled {{color:{p['muted']};background:{p['input']};
            border-color:{p['border']};}}
        {q} QPushButton#novelaiSecondaryButton {{color:{p['muted']};background:transparent;}}
        {q} QPushButton#novelaiSecondaryButton:hover {{color:{p['text']};background:{p['hover']};}}
        {q} QLabel#novelaiCardTitle {{color:{p['text']};font-weight:600;}}
        {q} QLabel#novelaiMuted, {q} QLabel#novelaiCapability {{color:{p['muted']};font-size:12px;}}
        {q} QLabel[tone="warning"] {{color:{p['warning']};}}
    '''


def set_tone(label, tone):
    if label.property('tone') != tone:
        label.setProperty('tone', tone)
        label.style().unpolish(label)
        label.style().polish(label)
        label.update()


def guard_wheel(widget):
    """Keep mouse-wheel scrolling from changing an unfocused parameter."""
    widget._novelai_wheel_guard = _ComboWheelBlocker(widget)
    widget.installEventFilter(widget._novelai_wheel_guard)
    if isinstance(widget, QtWidgets.QAbstractSpinBox):
        widget.lineEdit().installEventFilter(widget._novelai_wheel_guard)
        widget.setKeyboardTracking(False)
        widget.setAlignment(QtCore.Qt.AlignRight)
    widget.setMinimumHeight(CONTROL)
    widget.setMinimumWidth(0)
    widget.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
    return widget


def decimal(minimum=0., maximum=1., value=0., step=.05, decimals=2):
    widget = guard_wheel(QtWidgets.QDoubleSpinBox())
    widget.setLocale(QtCore.QLocale.c())
    widget.setDecimals(decimals)
    widget.setRange(minimum, maximum)
    widget.setSingleStep(step)
    widget.setValue(value)
    return widget


_THUMBNAILS = OrderedDict()


def thumbnail(path):
    """Cache only small decoded images, with file revision in the key."""
    try:
        source = Path(path)
        stat = source.stat()
        key = (str(source.resolve()), stat.st_mtime_ns, stat.st_size)
        if key in _THUMBNAILS:
            _THUMBNAILS.move_to_end(key)
            return _THUMBNAILS[key]
        reader = QtGui.QImageReader(str(source))
        reader.setAutoTransform(True)
        size = reader.size()
        if size.width() * size.height() > 32_000_000:
            return QtGui.QPixmap()
        if size.isValid():
            reader.setScaledSize(size.scaled(112, 112, QtCore.Qt.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            return QtGui.QPixmap()
        pixmap = QtGui.QPixmap.fromImage(image).scaled(
            112, 112, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        _THUMBNAILS[key] = pixmap
        while len(_THUMBNAILS) > 96:
            _THUMBNAILS.popitem(last=False)
        return pixmap
    except (OSError, ValueError):
        return QtGui.QPixmap()


def prepare_reference_image(path, directory):
    """Validate early and normalize supported static imports to an API-ready PNG."""
    source = Path(path)
    if not source.is_file() or source.stat().st_size > MAX_INPUT:
        raise ValueError('文件不存在或超过 32 MiB')
    try:
        with Image.open(source) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ValueError('图像超过 16777216 像素')
            if getattr(image, 'n_frames', 1) != 1:
                raise ValueError('请先导出需要使用的静态图像帧')
            if image.format in ('PNG', 'JPEG', 'WEBP', 'BMP'):
                image.verify()
                return source.resolve()
            if image.format not in ('GIF', 'TIFF'):
                raise ValueError('请选择 PNG、JPEG、WebP、BMP、静态 GIF 或 TIFF 图像')
            normalized = ImageOps.exif_transpose(image).convert('RGBA' if 'transparency' in image.info or 'A' in image.getbands() else 'RGB')
        from aetherloom_core.mask_assets import atomic_png
        digest = hashlib.sha256(normalized.tobytes())
        digest.update(str(normalized.size).encode('ascii'))
        digest.update(normalized.mode.encode('ascii'))
        destination = Path(directory) / ('reference-' + digest.hexdigest()[:20] + '.png')
        created = not destination.is_file()
        try:
            if created:
                destination.parent.mkdir(parents=True, exist_ok=True)
                atomic_png(normalized, destination)
        finally:
            normalized.close()
        if destination.stat().st_size > MAX_INPUT:
            if created:
                destination.unlink(missing_ok=True)
            raise ValueError('转换后的图像超过 32 MiB')
        return destination.resolve()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise ValueError('无法读取有效图像') from None


def _reference_slider(spin):
    """Slider stays in the usual 0–1 range; numeric overrides are retained."""
    slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
    slider.setRange(0, 100)
    slider.setSingleStep(1)
    slider.setPageStep(5)
    slider.setFixedHeight(20)
    slider.setMinimumWidth(0)
    slider._wheel_guard = _ComboWheelBlocker(slider)
    slider.installEventFilter(slider._wheel_guard)
    spin.setFixedWidth(94)

    def synchronize(value):
        with QtCore.QSignalBlocker(slider):
            slider.setValue(round(max(0., min(1., value)) * 100))

    spin.valueChanged.connect(synchronize)
    slider.valueChanged.connect(lambda value: spin.setValue(value / 100.))
    synchronize(spin.value())
    return slider


class _ReferenceCard(QtWidgets.QFrame):
    changed = QtCore.pyqtSignal()
    remove_requested = QtCore.pyqtSignal(object)

    def __init__(self, value, parent=None):
        super().__init__(parent)
        self.setObjectName('novelaiReferenceCard')
        self._raw = deepcopy(value)
        self._kind = str(value.get('kind', 'vibe'))
        self._precise_kind = (self._kind if self._kind != 'vibe' else
                              value.get('ui_precise_kind', 'character&style'))
        if self._precise_kind not in ('character', 'style', 'character&style') and self._kind == 'vibe':
            self._precise_kind = 'character&style'
        self._family_information = {'vibe': 1., 'precise': 1.}
        saved_information = value.get('ui_family_information', {})
        if isinstance(saved_information, dict):
            for family in self._family_information:
                saved = saved_information.get(family)
                if (isinstance(saved, (int, float)) and not isinstance(saved, bool)
                        and math.isfinite(saved) and 0 <= saved <= 1):
                    self._family_information[family] = float(saved)
        self._family_information['vibe' if self._kind == 'vibe' else 'precise'] = float(value.get('information_extracted', 1.))
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(11, 11, 11, 11)
        layout.setSpacing(9)
        head = QtWidgets.QHBoxLayout()
        preview = QtWidgets.QLabel()
        preview.setFixedSize(64, 64)
        preview.setAlignment(QtCore.Qt.AlignCenter)
        pixmap = thumbnail(value.get('path', ''))
        if pixmap.isNull():
            preview.setText('无法预览')
        else:
            preview.setPixmap(pixmap.scaled(64, 64, QtCore.Qt.KeepAspectRatio,
                                           QtCore.Qt.SmoothTransformation))
        preview.setObjectName('novelaiReferencePreview')
        head.addWidget(preview)
        title = QtWidgets.QLabel(Path(str(value.get('path', ''))).name or '参考图')
        title.setObjectName('novelaiCardTitle')
        title.setWordWrap(True)
        title.setMinimumWidth(0)
        title.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        title.setToolTip(str(value.get('path', '')))
        head.addWidget(title, 1)
        self.enabled = QtWidgets.QCheckBox('启用')
        self.enabled.setChecked(value.get('enabled', True) is not False)
        self.enabled.setToolTip('关闭后保留参考图设置，但不参与生成或计费。')
        head.addWidget(self.enabled, 0, QtCore.Qt.AlignTop)
        remove = QtWidgets.QToolButton()
        remove.setText('×')
        remove.setObjectName('novelaiIconButton')
        remove.setProperty('destructive', True)
        remove.setToolTip('移除此参考图')
        remove.setFixedSize(30, 30)
        remove.clicked.connect(lambda: self.remove_requested.emit(self))
        head.addWidget(remove, 0, QtCore.Qt.AlignTop)
        layout.addLayout(head)
        self.options = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(self.options)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.kind = guard_wheel(QtWidgets.QComboBox())
        for text, key in [('角色', 'character'), ('画风', 'style'), ('角色与画风', 'character&style')]:
            self.kind.addItem(text, key)
        if self.kind.findData(self._precise_kind) < 0:
            self.kind.addItem(str(self._precise_kind), self._precise_kind)
            self.kind.model().item(self.kind.count() - 1).setEnabled(False)
        self.kind.setCurrentIndex(self.kind.findData(self._precise_kind))
        self.kind_label = QtWidgets.QLabel('参考内容')
        form.addRow(self.kind_label, self.kind)
        self.strength = decimal(-1_000_000, 1_000_000, float(value.get('strength', .6)), .01)
        self.strength.setToolTip('常用范围 0–1；可直接输入负值或大于 1 的数值。强度过高可能压制提示词。')
        form.addRow('强度', self.strength)
        self.strength_slider = _reference_slider(self.strength)
        form.addRow(self.strength_slider)
        self.fidelity = decimal(-1_000_000, 1_000_000, float(value.get('fidelity', 1)))
        self.fidelity_label = QtWidgets.QLabel('保真度')
        self.fidelity.setToolTip('常用范围 0–1；越高越严格遵循参考图，越低越容易通过提示词调整。可手动输入负值。')
        form.addRow(self.fidelity_label, self.fidelity)
        self.fidelity_slider = _reference_slider(self.fidelity)
        form.addRow(self.fidelity_slider)
        self.information = decimal(0, 1, float(value.get('information_extracted', 1)), .01)
        self.information_label = QtWidgets.QLabel('提取信息量')
        self.information.setToolTip('降低时优先丢失纹理，保留更多构图信息。更改此值可能需要重新收费编码；相同图像与参数的编码会缓存。')
        form.addRow(self.information_label, self.information)
        self.information_slider = _reference_slider(self.information)
        form.addRow(self.information_slider)
        for widget in (self.strength, self.fidelity, self.information):
            form.setAlignment(widget, QtCore.Qt.AlignRight)
        layout.addWidget(self.options)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        for widget in (self.strength, self.fidelity, self.information):
            widget.valueChanged.connect(self.changed)
        self.enabled.toggled.connect(self._enabled_changed)
        self.set_family('vibe' if self._kind == 'vibe' else 'precise', emit=False)
        self._enabled_changed(self.enabled.isChecked(), emit=False)

    def _enabled_changed(self, enabled, emit=True):
        self.options.setEnabled(enabled)
        self.setProperty('referenceEnabled', enabled)
        self.style().unpolish(self)
        self.style().polish(self)
        if emit:
            self.changed.emit()

    def _kind_changed(self):
        self._precise_kind = self.kind.currentData()
        if self._kind != 'vibe':
            self._kind = self._precise_kind
        self.changed.emit()

    def set_family(self, family, emit=True):
        previous = 'vibe' if self._kind == 'vibe' else 'precise'
        self._family_information[previous] = self.information.value()
        with QtCore.QSignalBlocker(self.information):
            self.information.setValue(self._family_information[family])
        with QtCore.QSignalBlocker(self.information_slider):
            self.information_slider.setValue(round(self.information.value() * 100))
        vibe = family == 'vibe'
        self._kind = 'vibe' if vibe else self._precise_kind
        self.kind.setVisible(not vibe)
        self.kind_label.setVisible(not vibe)
        self.fidelity.setVisible(not vibe)
        self.fidelity_label.setVisible(not vibe)
        self.fidelity_slider.setVisible(not vibe)
        self.information.setVisible(vibe)
        self.information_label.setVisible(vibe)
        self.information_slider.setVisible(vibe)
        self.strength.setSingleStep(.01 if vibe else .05)
        # Keep the other family's numeric settings intact when changing modes.
        if emit:
            self.changed.emit()

    def value(self):
        value = deepcopy(self._raw)
        family_information = dict(self._family_information)
        family_information['vibe' if self._kind == 'vibe' else 'precise'] = self.information.value()
        value.update(kind=self._kind, strength=self.strength.value(),
                     fidelity=self.fidelity.value(),
                     information_extracted=self.information.value(), enabled=self.enabled.isChecked(),
                     ui_precise_kind=self._precise_kind,
                     ui_family_information=family_information)
        return value


class ReferencesEditor(QtWidgets.QWidget):
    """An exclusive Vibe/Precise reference group with independent image settings."""
    changed = QtCore.pyqtSignal()

    def __init__(self, owner=None):
        super().__init__(owner if isinstance(owner, QtWidgets.QWidget) else None)
        self.owner = owner
        self.setObjectName('novelaiReferences')
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self._cards = []
        self._loading = False
        self._caps = {}
        self._model = ""
        self._mode = 'dark'
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        self.family = guard_wheel(QtWidgets.QComboBox())
        self.family.addItem('Vibe Transfer · 氛围参考', 'vibe')
        self.family.addItem('Precise Reference · 精确参考', 'precise')
        self.family.currentIndexChanged.connect(self._family_changed)
        root.addWidget(self.family)
        self.hint = QtWidgets.QLabel()
        self.hint.setObjectName('novelaiMuted')
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)
        self.normalize_strength = QtWidgets.QCheckBox('归一化多张 Vibe 的总强度')
        self.normalize_strength.setChecked(True)
        self.normalize_strength.setToolTip('多张 Vibe 强度总和超过 1 时，按比例归一化到 1；各图原始设置保持不变。')
        self.normalize_strength.toggled.connect(self.changed)
        root.addWidget(self.normalize_strength)
        toolbar = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QPushButton('添加参考图')
        self.add_button.setObjectName('novelaiAddButton')
        self.add_button.clicked.connect(self._choose)
        self.paste_button = QtWidgets.QPushButton('粘贴图像')
        self.paste_button.setObjectName('novelaiSecondaryButton')
        self.paste_button.clicked.connect(self._paste)
        toolbar.addWidget(self.add_button, 1)
        toolbar.addWidget(self.paste_button, 1)
        root.addLayout(toolbar)
        self.empty = QtWidgets.QLabel('拖入图像到这里\n每张图可单独调整参考强度')
        self.empty.setObjectName('novelaiReferenceEmpty')
        self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.empty.setMinimumHeight(116)
        self.empty.setWordWrap(True)
        root.addWidget(self.empty)
        self.cards_layout = QtWidgets.QVBoxLayout()
        self.cards_layout.setSpacing(10)
        root.addLayout(self.cards_layout)
        root.addStretch(1)
        self._drop_filter = ImageDropFilter(self, self.add_paths)
        self._empty_drop_filter = ImageDropFilter(self.empty, self.add_paths)
        self._update_hint()
        self.apply_theme('dark')

    def _choose(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, '选择参考图', '', '图像 (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff);;所有文件 (*)')
        if paths:
            self.add_paths(paths)

    def _paste(self):
        if not import_mime(QtWidgets.QApplication.clipboard().mimeData(), self, self.add_paths):
            QtWidgets.QMessageBox.information(self, '粘贴图像', '剪贴板中没有可导入的图像。')

    def add_paths(self, paths):
        if not self.isEnabled():
            return
        rejected, added = [], 0
        family = self.family.currentData()
        if not self._caps.get(family, True):
            QtWidgets.QMessageBox.information(self, '参考图', '当前模型或模式不支持此参考方式，请先切换。')
            return
        for path in paths:
            if len(self._cards) >= 16:
                QtWidgets.QMessageBox.information(self, '参考图数量', '每次最多使用 16 张参考图。')
                break
            try:
                from aetherloom_core.paths import current_dir
                directory = Path(getattr(self.owner, 'input_dir', Path(current_dir) / 'input')) / 'novelai'
                source = prepare_reference_image(path, directory)
            except (OSError, ValueError) as error:
                rejected.append(f'{Path(path).name}：{error}')
                continue
            self._add_card(dict(reference_defaults(self._model, "vibe" if family == "vibe" else "character&style"), path=str(source)))
            added += 1
        self._update_hint()
        if added:
            self.changed.emit()
        if rejected:
            QtWidgets.QMessageBox.warning(self, '参考图未导入',
                '\n'.join(rejected[:8]))

    def _add_card(self, value):
        card = _ReferenceCard(value, self)
        card.changed.connect(self._card_changed)
        card.remove_requested.connect(self._remove)
        self._cards.append(card)
        self.cards_layout.addWidget(card)

    def _card_changed(self):
        self._synchronize_family()
        self._update_hint()
        if not self._loading:
            self.changed.emit()

    def _synchronize_family(self):
        families = {'vibe' if card._kind == 'vibe' else 'precise'
                    for card in self._cards if card.enabled.isChecked()}
        family = (next(iter(families)) if len(families) == 1 else
                  'mixed' if families else self.family.currentData())
        if family == 'mixed' and not families:
            family = 'vibe'
        with QtCore.QSignalBlocker(self.family):
            if self.family.findData(family) < 0:
                self.family.addItem('混合参考 · 需要调整', family)
            self.family.setCurrentIndex(self.family.findData(family))
            mixed = self.family.findData('mixed')
            if family != 'mixed' and mixed >= 0:
                self.family.removeItem(mixed)

    def _remove(self, card):
        self._cards.remove(card)
        self.cards_layout.removeWidget(card)
        card.hide()
        card.deleteLater()
        self._synchronize_family()
        self._update_hint()
        self.changed.emit()

    def _family_changed(self):
        if self._loading:
            return
        family = self.family.currentData()
        if family in ('vibe', 'precise'):
            for card in self._cards:
                card.set_family(family, emit=False)
            mixed = self.family.findData('mixed')
            if mixed >= 0:
                self.family.removeItem(mixed)
        self._update_hint()
        self.changed.emit()

    def _update_hint(self):
        family = self.family.currentData()
        available = self._caps.get(family, True)
        active_count = sum(card.enabled.isChecked() for card in self._cards)
        text = ('首次编码或修改提取信息量可能消耗 Anlas；相同设置的编码会缓存。'
                if family == 'vibe' else
                '每张参考图每次生成额外消耗 5 Anlas；多张角色参考会融合角色特征。')
        if family == 'vibe' and active_count > 4:
            text += ' 超过 4 张的部分另有生成费用。'
        text += ' Vibe 与精确参考不能同时启用。'
        if family == 'mixed':
            text = '当前设置混用了 Vibe 和精确参考，请选择一种参考模式后再生成。'
        elif not available:
            text = '当前模型或模式不支持此参考方式。已保留参考图；请切换模型或移除参考图。'
        self.hint.setText(text)
        self.family.setToolTip("切换模式会应用到全部参考图；单张停用可保留其设置。")
        set_tone(self.hint, 'warning' if family == 'mixed' or not available else 'muted')
        self.normalize_strength.setVisible(family == 'vibe')
        self.normalize_strength.setEnabled(available)
        self.empty.setVisible(not self._cards)
        can_add = available and family != 'mixed' and len(self._cards) < 16
        self.add_button.setEnabled(can_add)
        self.paste_button.setEnabled(can_add)
        self.add_button.setText(f"添加参考图 · {len(self._cards)}/16" if self._cards else "添加参考图")

    def set_capabilities(self, capabilities, model=None):
        self._caps = dict(capabilities or {})
        if model is not None:
            self._model = str(model)
        for index in range(self.family.count()):
            item = self.family.model().item(index)
            family = self.family.itemData(index)
            if item is not None:
                item.setEnabled(self._caps.get(family, True))
        self._update_hint()

    def value(self):
        return [card.value() for card in self._cards]

    def set_value(self, values, family=None):
        self._loading = True
        try:
            for card in self._cards:
                self.cards_layout.removeWidget(card)
                card.hide()
                card.deleteLater()
            self._cards = []
            values = values if isinstance(values, list) else []
            families = {'vibe' if value.get('kind', 'vibe') == 'vibe' else 'precise'
                        for value in values if isinstance(value, dict) and value.get('enabled', True) is not False}
            if families:
                family = next(iter(families)) if len(families) == 1 else 'mixed'
            elif family not in ('vibe', 'precise'):
                stored_families = {'vibe' if value.get('kind', 'vibe') == 'vibe' else 'precise'
                                   for value in values if isinstance(value, dict)}
                family = next(iter(stored_families)) if len(stored_families) == 1 else 'vibe'
            if self.family.findData(family) < 0:
                self.family.addItem('混合参考 · 需要调整', family)
            self.family.setCurrentIndex(self.family.findData(family))
            for value in values:
                if isinstance(value, dict):
                    self._add_card(value)
            self._synchronize_family()
            self._update_hint()
        finally:
            self._loading = False

    def apply_theme(self, mode):
        self._mode = mode
        colors = palette(mode)
        self.setStyleSheet(app_stylesheet(mode).replace('#rhAppPage', '#novelaiReferences') +
            editor_stylesheet(mode, 'novelaiReferences') + f'''
            QWidget#novelaiReferences {{background:transparent;}}
            QWidget#novelaiReferences QFrame#novelaiReferenceCard {{background:{colors['surface']};
                border:1px solid {colors['border']};border-radius:12px;}}
            QWidget#novelaiReferences QFrame#novelaiReferenceCard:hover {{border-color:{colors['muted']};}}
            QWidget#novelaiReferences QFrame#novelaiReferenceCard[referenceEnabled="false"] {{
                background:{colors['input']};border-style:dashed;}}
            QWidget#novelaiReferences QSlider::groove:horizontal {{height:4px;background:{colors['border']};border-radius:2px;}}
            QWidget#novelaiReferences QSlider::sub-page:horizontal {{background:{colors['accent']};border-radius:2px;}}
            QWidget#novelaiReferences QSlider::handle:horizontal {{width:12px;margin:-4px 0;background:{colors['accent']};border-radius:6px;}}
            QWidget#novelaiReferences QSlider::sub-page:horizontal:disabled,
            QWidget#novelaiReferences QSlider::handle:horizontal:disabled {{background:{colors['muted']};}}
            QWidget#novelaiReferences QLabel#novelaiReferenceEmpty {{color:{colors['muted']};
                background:{colors['input']};border:1px dashed {colors['border']};border-radius:12px;padding:12px;}}
            QWidget#novelaiReferences QLabel#novelaiReferencePreview {{background:{colors['input']};
                border:1px solid {colors['border']};border-radius:8px;}}
        ''')
