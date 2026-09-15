"""A single import/preview surface, with optional multi-file management."""
from PyQt5 import QtCore, QtWidgets
from aetherloom_core.image_input_preview import ImageInputPreview
from aetherloom_core.image_import import ImageDropFilter
from aetherloom_core.mask_assets import matches


def build(panel, node):
    from .editors import FileList
    files = FileList(kind='image');files.setObjectName('canvasInputFiles')
    files.setMinimumHeight(70);files.setMaximumHeight(110)
    files.set_paths(node.get('params', {}).get('files', []))
    preview = panel.input_preview = ImageInputPreview(panel)
    preview.setObjectName('canvasImageInputPreview')
    preview._image_drop = ImageDropFilter(preview, files.import_paths)
    preview.browse_requested.connect(lambda: panel._add_files(files, 'image'))
    preview.edit_requested.connect(lambda: panel._edit_input_mask(files))
    panel.form.addWidget(preview, 1)
    navigation = QtWidgets.QHBoxLayout();navigation.setSpacing(4)
    caption = QtWidgets.QLabel();caption.setMinimumWidth(0)
    caption.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
    previous = QtWidgets.QToolButton();previous.setText('‹');previous.setToolTip('上一张')
    following = QtWidgets.QToolButton();following.setText('›');following.setToolTip('下一张')
    for button in (previous, following):button.setFixedSize(28, 24)
    previous.clicked.connect(lambda: files.setCurrentRow(max(0, files.currentRow() - 1)))
    following.clicked.connect(lambda: files.setCurrentRow(min(files.count() - 1, files.currentRow() + 1)))
    navigation.addWidget(caption, 1);navigation.addWidget(previous);navigation.addWidget(following)
    panel.form.addLayout(navigation)
    toolbar = QtWidgets.QHBoxLayout();toolbar.setSpacing(4)
    for title, callback in [('导入', lambda: panel._add_files(files, 'image')),
                            ('文件夹', lambda: panel._add_folder(files)),
                            ('遮罩 / 绘画', lambda: panel._edit_input_mask(files))]:
        button = QtWidgets.QPushButton(title);button.clicked.connect(callback)
        if title == '遮罩 / 绘画':button.setObjectName('canvasMaskButton')
        toolbar.addWidget(button)
    manage = QtWidgets.QToolButton();manage.setText('管理');manage.setCheckable(True)
    manage.setMinimumWidth(44)
    toolbar.addWidget(manage);panel.form.addLayout(toolbar)
    details = QtWidgets.QWidget(panel);layout = QtWidgets.QVBoxLayout(details);layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(files)
    row = QtWidgets.QHBoxLayout();path = QtWidgets.QLineEdit();path.setObjectName('canvasInputPath');path.setPlaceholderText('文件或文件夹路径')
    add = QtWidgets.QPushButton('添加')
    def import_path():
        if files.import_paths([path.text()]):path.clear()
        else:panel.message.emit('路径不存在、格式不匹配或已在列表中。')
    add.clicked.connect(import_path);path.returnPressed.connect(import_path);row.addWidget(path, 1);row.addWidget(add);layout.addLayout(row)
    row = QtWidgets.QHBoxLayout()
    for title, callback in [('重新定位', lambda: panel._relocate(files, 'image')), ('移除选中', lambda: panel._remove_files(files))]:
        button = QtWidgets.QPushButton(title);button.clicked.connect(callback);row.addWidget(button)
    layout.addLayout(row);panel.form.addWidget(details);details.hide();manage.toggled.connect(details.setVisible)
    def refresh():
        paths = files.paths();index = max(0, files.currentRow())
        selected = paths[index] if index < len(paths) else ''
        masks = panel.node.get('params', {}).get('masks') or []
        mask = next((value for value in masks if selected and matches(value, selected)), None)
        if not paths and masks:mask = masks[0]
        preview.set_input(selected, mask)
        name = files.item(index).text() if paths else '遮罩预览' if mask else '未导入图像'
        caption.setText((f'{index + 1} / {len(paths)} · ' if paths else '') + name)
        caption.setToolTip(selected or name)
        previous.setEnabled(index > 0);following.setEnabled(index + 1 < len(paths))
    panel.refresh_input_preview = refresh
    def changed(values):
        panel.changed.emit('params.files', values)
        if files.count() and files.currentRow() < 0:files.setCurrentRow(0)
        refresh()
    files.files_changed.connect(changed);files.currentRowChanged.connect(lambda _: refresh())
    if files.count():files.setCurrentRow(0)
    refresh()
