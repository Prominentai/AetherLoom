"""Small, presentation-only tools for arranging and inspecting a canvas."""
import os
import re
import weakref

from PyQt5 import QtCore, QtGui, QtWidgets, sip

from aetherloom_core.rh_ui import palette
from aetherloom_core.ui.popups import DialogBoundary, themed_palette
from . import model
from .graphics import KIND_NAMES, RUNNING_STATES, STATUS_NAMES, WAITING_STATES, NodeItem
from .input_requirements import display_issue, display_missing_ports


def _live(page):
    return page is not None and not sip.isdeleted(page) and not getattr(page, '_closed', False)


def _selected(page, movable=False):
    if not _live(page):
        return []
    return sorted((item for item in page.scene.selectedItems()
                   if isinstance(item, NodeItem) and item.isVisible()
                   and (not movable or not item.node.get('layout_locked'))),
                  key=lambda item: item.node['id'])


def align_selected(page, mode):
    """Arrange visible, unlocked nodes, recording the whole move as one undo."""
    modes = {'left', 'right', 'top', 'bottom', 'hcenter', 'vcenter', 'distribute_h', 'distribute_v'}
    if mode not in modes:
        return False
    items = _selected(page, movable=True)
    if len(items) < (3 if mode.startswith('distribute_') else 2):
        return False
    rectangles = {item.node['id']: QtCore.QRectF(item.scenePos(), QtCore.QSizeF(item.width, item.height))
                  for item in items}
    bounds = QtCore.QRectF()
    for rect in rectangles.values():
        bounds = bounds.united(rect)
    positions = {}
    if mode.startswith('distribute_'):
        horizontal = mode == 'distribute_h'
        ordered = sorted(items, key=lambda item: ((rectangles[item.node['id']].center().x() if horizontal
                                                 else rectangles[item.node['id']].center().y()), item.node['id']))
        first, last = rectangles[ordered[0].node['id']], rectangles[ordered[-1].node['id']]
        start, end = (first.center().x(), last.center().x()) if horizontal else (first.center().y(), last.center().y())
        step = (end - start) / (len(ordered) - 1)
        for index, item in enumerate(ordered[1:-1], 1):
            rect = rectangles[item.node['id']]
            positions[item.node['id']] = ((start + index * step - rect.width() / 2, rect.top()) if horizontal
                                          else (rect.left(), start + index * step - rect.height() / 2))
    else:
        for item in items:
            rect = rectangles[item.node['id']]
            x, y = rect.left(), rect.top()
            if mode == 'left':x = bounds.left()
            elif mode == 'right':x = bounds.right() - rect.width()
            elif mode == 'top':y = bounds.top()
            elif mode == 'bottom':y = bounds.bottom() - rect.height()
            elif mode == 'hcenter':x = bounds.center().x() - rect.width() / 2
            elif mode == 'vcenter':y = bounds.center().y() - rect.height() / 2
            positions[item.node['id']] = (x, y)
    positions = {key: point for key, point in positions.items()
                 if abs(point[0] - rectangles[key].left()) > .01 or abs(point[1] - rectangles[key].top()) > .01}
    if not positions:
        return False
    # The normal move handler owns subgraph translation. Explicitly pin locked
    # members so a group cannot indirectly move a node with a fixed position.
    moved_groups = [node for node in page.document['nodes'] if node['id'] in positions and node['kind'] == 'subgraph']
    members = {identity for group in moved_groups for identity in group.get('members', [])}
    for node in page.document['nodes']:
        if node['id'] in members and node.get('layout_locked'):
            positions[node['id']] = (node.get('x', 0), node.get('y', 0))
    with QtCore.QSignalBlocker(page.scene):
        for identity, point in positions.items():
            item = page.scene.nodes.get(identity)
            if item is not None:
                item.setPos(*point)
    page._move_nodes(positions)
    return True


def set_layout_locked(page, locked):
    """Persist an editing preference without changing execution inputs."""
    identities = {item.node['id'] for item in _selected(page)}
    changed = [node for node in page.document['nodes']
               if node['id'] in identities and bool(node.get('layout_locked')) != bool(locked)] if _live(page) else []
    if not changed:
        return False
    page._checkpoint()
    for node in changed:
        node['layout_locked'] = bool(locked)
    page._edited()
    return True


def _safe_text(value, limit=1200):
    """Keep diagnostic messages readable without echoing credential fields."""
    text = str(value or '')[:limit * 3]
    text = re.sub(r'(?i)\b(?:Bearer|Basic)\s+[^\s,;]+', '[已隐藏认证信息]', text)
    text = re.sub(r'(?i)([\"\']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|'
                  r'authorization|password|passwd|secret|cookie)[\"\']?\s*[:=]\s*)'
                  r'(?:"[^"]*"|\'[^\']*\'|[^\s,;}&]+)', r'\1[已隐藏]', text)
    text = re.sub(r'(?i)\bsk-[A-Za-z0-9_-]{8,}', '[已隐藏密钥]', text)
    text = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[已隐藏]@', text)
    # Signed download links are not useful in a local-path inspection panel.
    text = re.sub(r'(https?://[^\s?#]+)\?[^\s]+', r'\1?[已隐藏查询参数]', text)
    return text[:limit] + ('…' if len(text) > limit else '')


def _state_labels(node):
    status = str(node.get('status') or 'IDLE')
    labels = [STATUS_NAMES.get(status, status)]
    if node.get('cached') and status != 'REUSED':labels.append('缓存结果')
    if node.get('bypass'):labels.append('已忽略')
    elif node.get('bypassed'):labels.append('上次已旁路')
    if node.get('stale') or node.get('_ui_stale'):labels.append('结果待更新')
    if node.get('layout_locked'):labels.append('位置已锁定')
    return ' · '.join(labels)


class _WorkspaceDialog(QtWidgets.QDialog):
    def __init__(self, page, title):
        super().__init__(page)
        self._page_ref = weakref.ref(page)
        self.setWindowTitle(title)
        self.setObjectName('canvasWorkspaceDialog')
        self.setModal(False)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.setMinimumSize(380, 320)
        self.resize(650, 510)
        font = QtGui.QFont(page.font());font.setPixelSize(13);self.setFont(font)
        self._popup_boundary = DialogBoundary(self)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(700)
        self._timer.timeout.connect(self.refresh)
        self._theme_source = self._stylesheet
        self._theme_mode = None
        self.refresh_theme()

    def page(self):
        page = self._page_ref()
        return page if _live(page) else None

    def _stylesheet(self):
        p = palette(self._theme_mode)
        return f'''
            QDialog#canvasWorkspaceDialog {{ background: {p['canvas']}; color: {p['text']}; }}
            QDialog#canvasWorkspaceDialog QWidget {{ font-size: 13px; }}
            QDialog#canvasWorkspaceDialog QLabel {{ color: {p['text']}; background: transparent; }}
            QDialog#canvasWorkspaceDialog QLabel#toolTitle {{ font-size: 20px; font-weight: 600; }}
            QDialog#canvasWorkspaceDialog QLabel#toolMuted {{ color: {p['muted']}; }}
            QDialog#canvasWorkspaceDialog QLineEdit, QDialog#canvasWorkspaceDialog QComboBox,
            QDialog#canvasWorkspaceDialog QPlainTextEdit, QDialog#canvasWorkspaceDialog QTreeWidget {{
                background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']};
                border-radius: 8px; padding: 7px; selection-background-color: {p['accent_soft']};
                selection-color: {p['text']}; }}
            QDialog#canvasWorkspaceDialog QLineEdit:focus {{ border-color: {p['accent']}; }}
            QDialog#canvasWorkspaceDialog QTreeWidget::item {{ padding: 8px 4px; border: none; }}
            QDialog#canvasWorkspaceDialog QTreeWidget::item:selected {{ background: {p['accent_soft']}; }}
            QDialog#canvasWorkspaceDialog QHeaderView::section {{ background: {p['surface']}; color: {p['muted']};
                padding: 8px 5px; border: none; border-bottom: 1px solid {p['border']}; }}
            QDialog#canvasWorkspaceDialog QPushButton {{ background: {p['surface']}; color: {p['text']};
                border: 1px solid {p['border']}; border-radius: 7px; padding: 8px 14px; }}
            QDialog#canvasWorkspaceDialog QPushButton:hover {{ background: {p['hover']}; border-color: {p['accent']}; }}
            QDialog#canvasWorkspaceDialog QPushButton:disabled {{ color: {p['muted']}; }}
            QDialog#canvasWorkspaceDialog QPushButton#toolPrimary {{ background: {p['accent']}; color: white; }}
            QDialog#canvasWorkspaceDialog QComboBox QAbstractItemView {{ background: {p['surface']};
                color: {p['text']}; selection-background-color: {p['accent_soft']}; }}
        '''

    def refresh_theme(self):
        page = self.page()
        mode = getattr(getattr(page, 'owner', None), '_theme_mode', 'dark')
        if mode != self._theme_mode:
            self._theme_mode = mode
            self.setPalette(themed_palette(mode, self.palette()))
            self.setStyleSheet(self._stylesheet())
            for editor in self.findChildren(QtWidgets.QLineEdit):
                editor.setPalette(themed_palette(mode, editor.palette()))

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._timer.start()
        self._popup_boundary.schedule()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._popup_boundary.schedule()


class NodeFinderDialog(_WorkspaceDialog):
    MAX_ROWS = 1000
    FILTERS = (('全部节点', 'all'), ('运行中', 'running'), ('等待中', 'waiting'), ('失败 / 阻塞', 'failed'),
               ('缺少输入', 'missing'), ('已完成 / 缓存', 'complete'), ('已忽略', 'bypassed'), ('结果待更新', 'stale'))

    def __init__(self, page):
        super().__init__(page, '查找画布节点')
        self._snapshot = None
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        title = QtWidgets.QLabel('查找节点');title.setObjectName('toolTitle')
        layout.addWidget(title)
        hint = QtWidgets.QLabel('搜索名称、类型或状态，双击定位。折叠子图内的节点会定位到所属子图。')
        hint.setObjectName('toolMuted');hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('搜索当前画布…');self.search.setClearButtonEnabled(True)
        self.search.setPalette(themed_palette(self._theme_mode, self.search.palette()))
        row.addWidget(self.search, 1)
        self.filter = QtWidgets.QComboBox()
        for label, key in self.FILTERS:self.filter.addItem(label, key)
        row.addWidget(self.filter)
        layout.addLayout(row)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(['节点名称', '类型', '状态'])
        self.tree.setRootIsDecorated(False);self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tree.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tree.setTextElideMode(QtCore.Qt.ElideRight)
        self.tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(2, QtWidgets.QHeaderView.Interactive)
        self.tree.setColumnWidth(1, 120);self.tree.setColumnWidth(2, 160)
        layout.addWidget(self.tree, 1)
        footer = QtWidgets.QHBoxLayout()
        self.summary = QtWidgets.QLabel();self.summary.setObjectName('toolMuted');self.summary.setWordWrap(True)
        footer.addWidget(self.summary, 1)
        self.locate = QtWidgets.QPushButton('定位节点');self.locate.setObjectName('toolPrimary')
        footer.addWidget(self.locate)
        close = QtWidgets.QPushButton('关闭');close.clicked.connect(self.close);footer.addWidget(close)
        layout.addLayout(footer)
        self.search.textChanged.connect(self.refresh)
        self.filter.currentIndexChanged.connect(self.refresh)
        self.tree.itemDoubleClicked.connect(self.locate_current)
        self.tree.itemSelectionChanged.connect(lambda: self.locate.setEnabled(self.tree.currentItem() is not None))
        self.search.returnPressed.connect(self.locate_current)
        self.locate.clicked.connect(self.locate_current)

    @staticmethod
    def _matches(node, key):
        status = str(node.get('status') or 'IDLE')
        return (key == 'all' or key == 'running' and status in RUNNING_STATES
                or key == 'waiting' and status in WAITING_STATES
                or key == 'failed' and status in {'FAILED', 'BLOCKED', 'DOWNLOAD_FAILED', 'UNKNOWN', 'INTERRUPTED'}
                or key == 'missing' and bool(display_issue(node) or node.get('_input_issue'))
                or key == 'complete' and (status in {'SUCCESS', 'REUSED'} or node.get('cached'))
                or key == 'bypassed' and bool(node.get('bypass') or node.get('bypassed'))
                or key == 'stale' and bool(node.get('stale') or node.get('_ui_stale')))

    def refresh(self, *unused):
        page = self.page()
        if page is None:
            self.close();return
        self.refresh_theme()
        terms = self.search.text().casefold().split()
        key, rows, total = self.filter.currentData(), [], 0
        for node in page.document.get('nodes', []):
            title = model.node_title(node)
            kind = KIND_NAMES.get(node['kind'], model.TITLES.get(node['kind'], node['kind']))
            status = _state_labels(node)
            haystack = ' '.join((title, kind, status, node['kind'], str(node.get('status', 'IDLE')))).casefold()
            if self._matches(node, key) and all(term in haystack for term in terms):
                total += 1
                if len(rows) < self.MAX_ROWS:rows.append((node['id'], title, kind, status))
        snapshot = (page.document['id'], tuple(rows), total)
        if snapshot == self._snapshot:
            return
        same_document = self._snapshot is not None and self._snapshot[0] == page.document['id']
        selected = self.tree.currentItem()
        identity = selected.data(0, QtCore.Qt.UserRole) if same_document and selected else None
        scroll = self.tree.verticalScrollBar().value() if same_document else 0
        self._snapshot = snapshot
        with QtCore.QSignalBlocker(self.tree):
            self.tree.clear()
            for node_id, title, kind, status in rows:
                item = QtWidgets.QTreeWidgetItem([title, kind, status])
                item.setData(0, QtCore.Qt.UserRole, node_id)
                for column, text in enumerate((title, kind, status)):item.setToolTip(column, text)
                self.tree.addTopLevelItem(item)
                if node_id == identity:self.tree.setCurrentItem(item)
            if self.tree.currentItem() is None and rows:self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self.tree.verticalScrollBar().setValue(scroll)
        self.locate.setEnabled(bool(rows))
        self.summary.setText((f'找到 {total} 个节点' if total else '没有匹配的节点')
                             + (f' · 显示前 {self.MAX_ROWS} 个，请缩小搜索范围' if total > self.MAX_ROWS else ''))

    def locate_current(self, *unused):
        page, row = self.page(), self.tree.currentItem()
        if page is None or row is None or self._snapshot is None:
            return
        if page.document['id'] != self._snapshot[0]:
            self.refresh();return
        identity = row.data(0, QtCore.Qt.UserRole)
        item = page.scene.nodes.get(identity)
        if item is None:
            self.refresh();return
        visited = set()
        while not item.isVisible() and identity not in visited:
            visited.add(identity)
            identity = getattr(page.scene, 'group_owners', {}).get(identity)
            item = page.scene.nodes.get(identity)
            if item is None:return
        page.scene.clearSelection()
        item.setSelected(True)
        page.view.centerOn(item)
        page.view.viewport().update()


def _result_lines(results, limit=40):
    """Inspect at most 40 leaf results and 200 containers; never read content."""
    stack = [(iter(results), 0)]
    lines, inspected, visited = [], 0, 0
    while stack and inspected < limit and visited < 200:
        iterator, depth = stack[-1]
        try:value = next(iterator)
        except StopIteration:
            stack.pop();continue
        visited += 1
        if isinstance(value, dict) and value.get('type') == 'batch' and depth < 8:
            members = value.get('items') or []
            if isinstance(members, list):stack.append((iter(members), depth + 1));continue
        inspected += 1
        path = value if isinstance(value, str) else (value.get('path') or value.get('file_path')) if isinstance(value, dict) else None
        if path:
            path = str(path)
            if path.startswith(('\\\\', '//')) or '://' in path:
                available = '远程路径 · 未检查'
            else:
                try:available = '文件可用' if os.path.exists(path) else '文件已缺失'
                except (OSError, ValueError):available = '路径不可访问'
            lines.append(f'{inspected}. {available}\n   {_safe_text(path, 350)}')
        else:
            kind = str(value.get('type') or '内存结果') if isinstance(value, dict) else '内存结果'
            lines.append(f'{inspected}. {_safe_text(kind, 60)} · 无本地文件路径')
    if stack:lines.append(f'仅检查前 {inspected} 项；其余结果未展开。')
    return lines


class NodeDiagnosticsDialog(_WorkspaceDialog):
    def __init__(self, page, node_id):
        super().__init__(page, '节点诊断')
        self.node_id, self.document_id = node_id, page.document['id']
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16);layout.setSpacing(12)
        self.title = QtWidgets.QLabel('节点诊断');self.title.setObjectName('toolTitle');self.title.setWordWrap(True)
        self.title.setTextFormat(QtCore.Qt.PlainText)
        layout.addWidget(self.title)
        hint = QtWidgets.QLabel('查看当前状态、输入提示和结果文件。内容会自动刷新。')
        hint.setObjectName('toolMuted');hint.setWordWrap(True);layout.addWidget(hint)
        self.details = QtWidgets.QPlainTextEdit();self.details.setReadOnly(True)
        self.details.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
        layout.addWidget(self.details, 1)
        footer = QtWidgets.QHBoxLayout()
        footer.addStretch(1)
        refresh = QtWidgets.QPushButton('刷新');refresh.clicked.connect(self.refresh);footer.addWidget(refresh)
        close = QtWidgets.QPushButton('关闭');close.clicked.connect(self.close);footer.addWidget(close)
        layout.addLayout(footer)

    def refresh(self, *unused):
        page = self.page()
        if page is None:
            self.close();return
        self.refresh_theme()
        node = next((node for node in page.document.get('nodes', []) if node['id'] == self.node_id), None)
        if self.document_id != page.document['id'] or node is None:
            self.title.setText('节点已不可用')
            self.details.setPlainText('节点已被删除或已切换画布。关闭此窗口后重新选择节点。')
            return
        self.title.setText(model.node_title(node)[:100])
        lines = ['状态', _state_labels(node), '', '输入与错误']
        issue = display_issue(node)
        if issue:lines.append('当前输入问题：' + _safe_text(issue))
        hint = node.get('_input_issue')
        if hint and hint != issue:lines.append('输入提示：' + _safe_text(hint))
        ports = display_missing_ports(node)
        if ports:lines.append('缺少端口：' + _safe_text('、'.join(map(str, ports))))
        stale = node.get('stale') or node.get('_ui_stale')
        error = node.get('error')
        if error and not stale:lines.append('运行错误：' + _safe_text(error))
        elif error:lines.append('参数已变更，上次运行错误不代表当前输入状态。')
        if not issue and not hint and not error:lines.append('当前没有输入问题或运行错误。')
        if node.get('message') and not stale:lines.append('运行信息：' + _safe_text(node['message']))
        results = node.get('results') or []
        lines.extend(['', f'结果 · {len(results)} 个输出记录'])
        if node.get('bypass'):lines.append('此节点已忽略，将按旁路规则传递输入。')
        if stale:lines.append('已有结果来自旧参数；再次运行后更新。')
        lines.extend(_result_lines(results) if results else ['尚无结果。'])
        text = '\n'.join(lines)
        if self.details.toPlainText() != text:
            scroll = self.details.verticalScrollBar().value()
            self.details.setPlainText(text)
            self.details.verticalScrollBar().setValue(scroll)


def show_node_finder(page):
    if not _live(page):return None
    dialog = getattr(page, '_node_finder_dialog', None)
    if dialog is None or sip.isdeleted(dialog):
        dialog = page._node_finder_dialog = NodeFinderDialog(page)
    dialog.show();dialog.raise_();dialog.activateWindow();dialog.search.setFocus()
    return dialog


def show_node_diagnostics(page):
    items = _selected(page)
    if not items:
        if _live(page):page._message('请先选择一个节点以查看诊断。')
        return None
    node_id = items[0].node['id']
    dialog = getattr(page, '_node_diagnostics_dialog', None)
    if dialog is None or sip.isdeleted(dialog):
        dialog = page._node_diagnostics_dialog = NodeDiagnosticsDialog(page, node_id)
    else:
        dialog.node_id, dialog.document_id = node_id, page.document['id']
    dialog.refresh();dialog.show();dialog.raise_();dialog.activateWindow()
    return dialog
