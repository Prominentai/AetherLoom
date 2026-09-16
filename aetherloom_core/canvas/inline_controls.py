"""Lazy in-node forms using the same parameter editors as the full inspector."""
import copy
from PyQt5 import QtCore, QtGui, QtWidgets
from .editors import Inspector


class InlineControls(QtWidgets.QScrollArea):
    def __init__(self, item):
        super().__init__()
        self.item = item
        self.page = item.canvas_scene.parent()
        self._canvas_owner = self.page.owner
        self.settings = self._canvas_owner.settings
        self._changing = self._building = False
        self.inspector = None
        self._state = None
        self.setObjectName('canvasInlineControls')
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setMinimumSize(60, 50)
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._build)
        self._panel_timer = QtCore.QTimer(self)
        self._panel_timer.setSingleShot(True)
        self._panel_timer.timeout.connect(self._sync_panel)
        self._geometry_timer = QtCore.QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.timeout.connect(self._sync_geometry)
        self.verticalScrollBar().valueChanged.connect(lambda *_: self._geometry_timer.start(0))
        self._build()
        self.refresh()

    @property
    def input_dir(self):
        return self._canvas_owner.input_dir

    def state(self):
        node = self.item.node
        state = {key: copy.deepcopy(node.get(key)) for key in
                 ('title', 'params', 'decode_settings', 'model_config', 'app')}
        state['connected_inputs'] = tuple(sorted(edge['input'] for edge in self.page.document['edges']
                                                if edge['target'] == node['id']))
        state['input_keys'] = tuple(node.get('input_keys') or [])
        return state

    def _build(self):
        if self._building or self.page._closed:return
        self._building = True
        try:
            old = self.inspector
            position = self.verticalScrollBar().value()
            if old is not None:
                old.changed.disconnect()
                old.hide()
                self.takeWidget()
                old.deleteLater()
            panel = Inspector(self.item.node, self.page.document['id'], self.page.document['edges'],
                              self.page.histories, parent=self.viewport(), model_owner=self._canvas_owner,
                              embedded=True)
            self.inspector = panel
            if hasattr(panel, 'compare_view'):
                panel.compare_view.surface.setToolTip('拖动分隔线对比；滚轮缩放画布，双击恢复对比视图。')
            panel.layout().setContentsMargins(2, 2, 2, 2)
            panel.layout().setSpacing(5)
            from .model import MODEL_KINDS
            if self.item.node['kind'] in MODEL_KINDS:
                from .node_form import FieldLabel
                config = self.item.node.get('model_config', {})
                name = str(config.get('model') or '').strip()
                provider = str(config.get('provider') or '').strip()
                summary = name if name and provider else '未选择连接' if name else '未配置'
                row = QtWidgets.QHBoxLayout();row.setSpacing(6)
                label = FieldLabel('模型：' + summary, panel)
                label.setObjectName('canvasModelSummary')
                label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
                label.setToolTip('当前模型：' + (name or '未指定') + '\n连接：' + (provider or '未选择'))
                button = QtWidgets.QToolButton(panel);button.setText('模型设置')
                button.setObjectName('canvasModelSettingsButton')
                button.setCursor(QtCore.Qt.PointingHandCursor)
                button.setToolTip('打开此节点的模型分页，设置连接和模型；仅对当前节点生效。')
                button.clicked.connect(lambda: self.page._model_settings(self.item.node['id']))
                row.addWidget(label, 1);row.addWidget(button)
                panel.layout().insertLayout(0, row)
            # Long help remains available on hover and in the complete inspector.
            for label in panel.findChildren(QtWidgets.QLabel, 'canvasMuted'):
                panel.setToolTip((panel.toolTip() + '\n' + label.text()).strip())
                label.hide()
            self._prepare_ports(panel)
            for widget in panel.findChildren(QtWidgets.QWidget):
                widget.installEventFilter(self)
                if isinstance(widget, QtWidgets.QLineEdit):widget.setMinimumWidth(0)
            panel.changed.connect(self._changed)
            panel.message.connect(self.page._message)
            panel.password_requested.connect(self.page._provide_password)
            panel.install_requested.connect(self.page._install_missing_apps)
            panel.rebind_requested.connect(self.page._rebind_app)
            self.setWidget(panel)
            self.verticalScrollBar().setValue(position)
            self._state = self.state()
            self._geometry_timer.start(0)
        finally:
            self._building = False

    def _changed(self, path, value):
        if self._building:return
        self._changing = True
        try:
            self.page._node_changed(self.item.node['id'], path, value)
            self.inspector.node = self.item.node
            self._state = self.state()
            # Text documents already synchronize both views without rebuilding
            # the editor (and therefore without disturbing its cursor/undo).
            text_keys = {editor.property('canvasTextKey') for editor in self.inspector.findChildren(QtWidgets.QTextEdit)}
            if path not in {'params.' + key for key in text_keys if isinstance(key, str)}:
                self._panel_timer.start(0)
        finally:
            self._changing = False

    def validate(self):
        if not self.inspector.validate():return False
        for editor in self.inspector.findChildren(QtWidgets.QLineEdit):
            if editor.isEnabled() and editor.isModified():
                editor.editingFinished.emit()
                editor.setModified(False)
        return True

    def _sync_panel(self):
        panel = self.page._inspector
        if isinstance(panel, Inspector) and panel.node['id'] == self.item.node['id']:
            focus = QtWidgets.QApplication.focusWidget()
            if focus is not None and panel.isAncestorOf(focus):return
            self.page._selection_changed(force=True)

    def eventFilter(self, widget, event):
        if event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.LayoutRequest, QtCore.QEvent.Show, QtCore.QEvent.Hide):
            if not self._building:self._geometry_timer.start(0)
        if event.type() == QtCore.QEvent.FocusIn and not self._building:
            if not self.item.isSelected():
                self.item.scene().clearSelection()
                self.item.setSelected(True)
        return super().eventFilter(widget, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_geometry_timer'):self._geometry_timer.start(0)

    def _prepare_ports(self, panel):
        from .model import input_ports
        from .node_form import FoldSection, FieldLabel
        self._port_sections = {}
        insertion = 0
        for port in input_ports(self.item.node):
            key = port['key']
            widget = panel.port_widgets.get(key)
            if widget is None:
                widget = FieldLabel(port['label'], panel)
                widget.setObjectName('canvasFieldLabel');widget.setMinimumHeight(24)
                widget.setToolTip('通过连线传入此项内容。')
                panel.layout().insertWidget(insertion, widget);insertion += 1
                panel.port_widgets[key] = widget
            parent = widget.parentWidget()
            while parent is not None and parent is not panel:
                if isinstance(parent, FoldSection):
                    parent.add_port(key, port['label'])
                    self._port_sections[key] = parent
                    break
                parent = parent.parentWidget()
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

    def port_positions(self):
        positions = {}
        for key, widget in self.inspector.port_widgets.items():
            section = self._port_sections.get(key)
            if section is not None and not section.toggle.isChecked():
                widget = section.port_summaries[key]
            positions[key] = widget.mapTo(self, QtCore.QPoint(0, widget.height() // 2)).y()
        return positions

    def _sync_geometry(self):
        if self._building or self.page._closed or self.item.inline_proxy is None:return
        self.inspector.layout().activate()
        if self.inspector.layout() is not None:
            height = max(self.inspector.sizeHint().height(),
                         self.inspector.heightForWidth(self.viewport().width())) + 4
            self.item.form_content_height = height
            reserve = 308 if self.item.node['kind'] in ('preview', 'mask_preview') else 228
            minimum = self.item.body_rect().top() + height + 29 + (reserve if self.item.result_count() else 0)
            if abs(getattr(self.item, 'form_minimum_height', 0) - minimum) > 1:
                self.item.form_minimum_height = minimum
                size = [self.item.width, self.item.height] if self.item._resize_start is not None else self.item.node.get('size')
                self.item.set_size(size)
        self.item.layout_inline()

    def refresh(self):
        self.inspector.node = self.item.node
        if self.item.node['kind'] == 'text_file':
            self.inspector.connected_inputs = {edge['input'] for edge in self.page.document['edges']
                                               if edge['target'] == self.item.node['id']}
        if hasattr(self.inspector, 'refresh_input_preview'):self.inspector.refresh_input_preview()
        if hasattr(self.inspector, 'compare_view'):
            self.inspector.compare_view.set_results(self.item.node.get('results', []))
        self._geometry_timer.start(0)
        if not self._changing and self.state() != self._state:
            self._refresh_timer.start(0)
        colors = self.item.canvas_scene.colors
        if hasattr(self.inspector, 'input_preview'):self.inspector.input_preview.set_colors(colors)
        self._theme_mode = getattr(self._canvas_owner, '_theme_mode', 'dark')
        if getattr(self, '_colors', None) == colors:return
        self._colors = dict(colors)
        palette = QtGui.QPalette(self._canvas_owner.palette())
        for role, key in ((QtGui.QPalette.Window, 'surface'), (QtGui.QPalette.Base, 'input'),
                          (QtGui.QPalette.Button, 'surface'), (QtGui.QPalette.Text, 'text'),
                          (QtGui.QPalette.WindowText, 'text'), (QtGui.QPalette.ButtonText, 'text')):
            palette.setColor(role, QtGui.QColor(colors[key]))
        self.setPalette(palette)
        from pathlib import Path
        from aetherloom_core.paths import current_dir
        arrow = (Path(current_dir) / 'icons' / ('ui-chevron-down-' + self._theme_mode + '.svg')).as_posix()
        self.setStyleSheet(
            'QWidget{color:'+colors['text']+';font-family:"Microsoft YaHei UI";font-size:12px;}'
            'QScrollArea,QScrollArea>QWidget>QWidget{background:'+colors['surface']+';border:none;}'
            'QLineEdit,QTextEdit,QPlainTextEdit,QAbstractSpinBox,QComboBox,QListWidget{background:'+colors['input']+';'
            'border:1px solid '+colors['border']+';border-radius:4px;padding:3px;selection-background-color:'+colors['accent']+';}'
            'QTextEdit,QPlainTextEdit{padding:7px;font-size:13px;border-radius:6px;}'
            'QTextEdit:focus,QLineEdit:focus{border-color:'+colors['accent']+';}'
            'QTextEdit:disabled,QLineEdit:disabled,QAbstractSpinBox:disabled,QComboBox:disabled{color:'+colors['muted']+';}'
            'QPushButton,QToolButton{background:'+colors['input']+';border:1px solid '+colors['border']+';'
            'border-radius:4px;padding:4px;font-size:11px;}'
            'QPushButton:hover,QToolButton:hover{border-color:'+colors['accent']+';}'
            'QToolButton#canvasTextHistoryButton{border:none;background:transparent;padding:0;font-size:16px;}'
            'QLabel#canvasFieldLabel{color:'+colors['text']+';font-size:12px;}'
            'QAbstractSpinBox,QComboBox{border-radius:10px;min-height:20px;}'
            'QComboBox{padding-right:20px;}QComboBox::drop-down{width:20px;border:none;background:transparent;}'
            'QComboBox::down-arrow{image:url("'+arrow+'");width:10px;height:10px;}'
            'QFrame#canvasScalarRow{background:'+colors['input']+';border:1px solid '+colors['border']+';border-radius:12px;}'
            'QFrame#canvasScalarRow QAbstractSpinBox,QFrame#canvasScalarRow QComboBox{border:none;background:transparent;}'
            'QLabel#canvasModelSummary{color:'+colors['muted']+';font-size:11px;}'
            'QToolButton#canvasSectionToggle{background:transparent;border:none;border-top:1px solid '+colors['border']+';'
            'border-radius:0;text-align:left;padding:4px 2px;color:'+colors['muted']+';}'
            'QGroupBox{border:1px solid '+colors['border']+';border-radius:5px;margin-top:9px;padding-top:9px;}'
            'QGroupBox::title{subcontrol-origin:margin;left:8px;}'
            'QScrollBar:vertical{width:7px;background:transparent;}'
            'QScrollBar::handle:vertical{background:'+colors['border']+';min-height:20px;border-radius:3px;}'
            'QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}'
            'QWidget:disabled{color:'+colors['muted']+';}')
