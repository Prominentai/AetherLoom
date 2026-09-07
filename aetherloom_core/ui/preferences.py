"""Shared API/settings presentation without changing configuration storage."""
from pathlib import Path

from PyQt5 import QtCore, QtWidgets

from aetherloom_core.rh_ui import palette
from aetherloom_core.autocomplete import completion_options


def _completion_card(window):
    card = QtWidgets.QFrame()
    card.setObjectName('settingsCard')
    layout = QtWidgets.QVBoxLayout(card)
    title = QtWidgets.QLabel('提示词补全')
    title.setObjectName('settingsCardTitle')
    description = QtWidgets.QLabel('控制补全后的 tag 格式与候选窗口大小；修改后立即生效。')
    description.setObjectName('settingsHint')
    description.setWordWrap(True)
    layout.addWidget(title)
    layout.addWidget(description)
    options = completion_options(window.settings)
    window.autocomplete_escape_cb = QtWidgets.QCheckBox('自动在圆括号前添加反斜杠（\\）')
    window.autocomplete_escape_cb.setChecked(options['escape_parentheses'])
    window.autocomplete_escape_cb.setToolTip(r'例如 character_(series) → character_\(series\)，避免括号被识别为权重语法。')
    window.autocomplete_spaces_cb = QtWidgets.QCheckBox('用下划线（_）替换空格')
    window.autocomplete_spaces_cb.setChecked(options['replace_spaces'])
    window.autocomplete_spaces_cb.setToolTip('开启：long_hair；关闭：long hair。仅影响新插入的补全内容。')
    layout.addWidget(window.autocomplete_escape_cb)
    layout.addWidget(window.autocomplete_spaces_cb)
    form = QtWidgets.QFormLayout()
    form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
    window.autocomplete_rows_spin = QtWidgets.QSpinBox()
    window.autocomplete_rows_spin.setRange(1, 50)
    window.autocomplete_rows_spin.setValue(options['visible_tags'])
    window.autocomplete_rows_spin.setSuffix(' 个')
    window.autocomplete_rows_spin.setMinimumHeight(38)
    window.autocomplete_rows_spin.setMaximumWidth(160)
    window.autocomplete_rows_spin.setKeyboardTracking(False)
    window.autocomplete_rows_spin.setAccessibleName('补全窗口可见 tag 数量')
    window._install_combo_wheel_blocker(window.autocomplete_rows_spin)
    form.addRow('窗口可见 tag 数量', window.autocomplete_rows_spin)
    layout.addLayout(form)
    hint = QtWidgets.QLabel('范围 1–50，默认 15；屏幕空间不足时自动收缩，更多候选可滚动查看。')
    hint.setObjectName('settingsHint')
    hint.setWordWrap(True)
    layout.addWidget(hint)

    def save_options(*_):
        window.settings['autocomplete'] = {
            'escape_parentheses': window.autocomplete_escape_cb.isChecked(),
            'replace_spaces': window.autocomplete_spaces_cb.isChecked(),
            'visible_tags': window.autocomplete_rows_spin.value()}
        window._save_settings()

    window.autocomplete_escape_cb.toggled.connect(save_options)
    window.autocomplete_spaces_cb.toggled.connect(save_options)
    window.autocomplete_rows_spin.valueChanged.connect(save_options)
    return card


def _header(frame, title, description, eyebrow):
    frame.setObjectName('preferenceHero')
    layout = frame.layout()
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            item.widget().deleteLater()
    layout.setContentsMargins(22, 20, 22, 20)
    layout.setSpacing(7)
    for name, text in (('preferenceEyebrow', eyebrow), ('preferenceTitle', title),
                       ('preferenceDescription', description)):
        label = QtWidgets.QLabel(text)
        label.setObjectName(name)
        label.setWordWrap(True)
        layout.addWidget(label)


class ResponsivePreferences(QtCore.QObject):
    def __init__(self, scroll, layout, rows=()):
        super().__init__(scroll)
        self.scroll, self.layout, self.rows = scroll, layout, rows
        scroll.viewport().installEventFilter(self)
        self.update()

    def update(self):
        width = self.scroll.viewport().width()
        gutter = 16 if width < 720 else 28
        gutter = max(gutter, (width - 1120) // 2)
        self.layout.setContentsMargins(gutter, 24, gutter, 24)
        for row in self.rows:
            direction = QtWidgets.QBoxLayout.TopToBottom if width < 720 else QtWidgets.QBoxLayout.LeftToRight
            if row.direction() != direction:
                row.setDirection(direction)

    def eventFilter(self, obj, event):
        if event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show):
            self.update()
        return False


class SettingsSections(QtCore.QObject):
    def __init__(self, window, tabs, cards):
        super().__init__(window.settings_page)
        self.window, self.tabs, self.cards = window, tabs, cards
        tabs.currentChanged.connect(self.select)
        self.select(0)

    def select(self, index):
        for position, card in enumerate(self.cards):
            group = 0 if position < 3 else 1 if position < 6 else 2
            card.setVisible(group == index)
        self.window.settings_page.verticalScrollBar().setValue(0)


def configure_settings(window, layout, column, hero):
    scroll = window.settings_page
    scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
    layout.setSpacing(18)
    column.setSpacing(14)
    _header(hero, '设置中心', '管理文件位置、运行偏好与提示词模板。', '工作空间偏好')
    tabs = QtWidgets.QTabBar()
    tabs.setObjectName('preferenceTabs')
    tabs.setExpanding(False)
    tabs.setDrawBase(False)
    tabs.setUsesScrollButtons(True)
    tabs.setAccessibleName('设置分类')
    for title in ('文件目录', '性能与任务', '提示词'):
        tabs.addTab(title)
    layout.insertWidget(1, tabs)
    window._settings_tabs = tabs
    # Prompt cards start at index six; preserve the existing category mapping.
    column.insertWidget(6, _completion_card(window))
    cards = [column.itemAt(i).widget() for i in range(column.count()) if column.itemAt(i).widget()]
    window._settings_cards = cards
    folder_rows = []
    for card, field, browse, open_button in zip(cards[:3],
            (window.input_label, window.output_label, window.local_decode_label),
            (window.input_btn, window.output_btn, window.local_decode_btn),
            (window.input_open_btn, window.output_open_btn, window.local_decode_open_btn)):
        row = card.layout().itemAt(2).layout()
        while row.count():
            row.takeAt(0)
        field.setMinimumWidth(0)
        field.setMinimumHeight(40)
        field.setToolTip(field.text())
        field.textChanged.connect(field.setToolTip)
        title = card.findChild(QtWidgets.QLabel, 'settingsCardTitle').text()
        field.setAccessibleName(title)
        browse.setText('选择目录')
        open_button.setText('打开')
        actions = QtWidgets.QWidget(card)
        actions_layout = QtWidgets.QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(8)
        actions_layout.addStretch(1)
        for button in (browse, open_button):
            button.setMinimumWidth(0)
            button.setMinimumHeight(40)
            button.setAccessibleName(button.text() + '：' + title)
            button.setCursor(QtCore.Qt.PointingHandCursor)
            actions_layout.addWidget(button)
        row.addWidget(field, 1)
        row.addWidget(actions)
        folder_rows.append(row)
    for card in cards:
        card.layout().setContentsMargins(20, 18, 20, 18)
        card.layout().setSpacing(10)
    cards[3].findChild(QtWidgets.QLabel, 'settingsHint').setText('限制磁盘缩略图缓存占用；达到上限后自动清理较旧的缓存。')
    cards[4].findChild(QtWidgets.QLabel, 'settingsHint').setText('保留最近访问的 RH 应用页面，减少再次打开时的加载。')
    # Keep control instances and their original signal connections intact.
    for spin, suffix in ((window.thumb_cache_spin, ' MB'), (window.app_cache_spin, ' 个'),
                         (window.rh_retry_max_spin, ' 次'), (window.rh_retry_delay_spin, ' 秒')):
        spin.setMinimumWidth(116)
        spin.setMaximumWidth(160)
        spin.setMinimumHeight(38)
        spin.setSuffix(suffix)
        window._install_combo_wheel_blocker(spin)
    for card, text in ((cards[3], '缓存上限'), (cards[4], '保留页面')):
        row = card.layout().itemAt(2).layout()
        row.itemAt(0).widget().setText(text)
    rh_layout = cards[5].layout()
    old_row = rh_layout.takeAt(2).layout()
    while old_row.count():
        item = old_row.takeAt(0)
        if isinstance(item.widget(), QtWidgets.QLabel):
            item.widget().deleteLater()
    old_row.deleteLater()
    form = QtWidgets.QFormLayout()
    form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
    form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldsStayAtSizeHint)
    form.setHorizontalSpacing(20)
    form.setVerticalSpacing(12)
    form.addRow('最多重试', window.rh_retry_max_spin)
    form.addRow('重试间隔', window.rh_retry_delay_spin)
    rh_layout.addLayout(form)
    for editor in (window.expand_system_prompt_edit, window.image_reverse_prompt_edit):
        editor.setFixedHeight(210)
        editor.setAcceptRichText(False)
    window.clear_cache_btn.setMinimumHeight(38)
    window._settings_sections = SettingsSections(window, tabs, cards)
    window._settings_responsive = ResponsivePreferences(scroll, layout, folder_rows)


def configure_api(window, layout, hero):
    _header(hero, 'API 管理', '统一管理服务密钥与模型连接，配置后可测试响应。', '模型与服务')
    window._api_responsive = ResponsivePreferences(window._api_scroll, layout)
    panel = window.api_page.findChild(QtWidgets.QFrame, 'apikeyPanel')
    if panel is None:
        return
    description = QtWidgets.QLabel('按供应商保存密钥，在不同模型配置间复用。')
    description.setObjectName('apiMuted')
    description.setWordWrap(True)
    panel.layout().insertWidget(1, description)
    rows = []
    for fields in window.apikey_rows.values():
        row = fields['key_edit'].parentWidget()
        row.setObjectName('apiKeyRow')
        row.layout().setContentsMargins(14, 12, 14, 12)
        label = row.findChild(QtWidgets.QLabel)
        rows.append((row, label.text().casefold() if label else ''))
    if not rows:
        return
    search = QtWidgets.QLineEdit()
    search.setObjectName('apiKeySearch')
    search.setPlaceholderText('搜索供应商名称…')
    search.setAccessibleName('搜索密钥供应商')
    search.setClearButtonEnabled(True)
    empty = QtWidgets.QLabel('没有匹配的供应商')
    empty.setObjectName('apiMuted')
    empty.hide()
    holder = rows[0][0].parentWidget()
    expanded = holder.parentWidget()
    expanded.layout().insertWidget(0, search)
    expanded.layout().insertWidget(1, empty)

    def filter_rows(text):
        query = text.strip().casefold()
        matches = 0
        for row, name in rows:
            visible = not query or query in name
            row.setVisible(visible)
            matches += visible
        empty.setVisible(matches == 0)

    search.textChanged.connect(filter_rows)
    window._api_key_search = search


def stylesheet(root, mode):
    p = palette(mode)
    icons = Path(__file__).resolve().parents[2] / 'icons'
    theme = 'light' if mode == 'light' else 'dark'
    down = (icons / f'ui-chevron-down-{theme}.svg').as_posix()
    up = (icons / f'ui-chevron-up-{theme}.svg').as_posix()
    check = (icons / 'ui-check.svg').as_posix()
    s = '#' + root
    return f'''
        {s} {{ background: {p['canvas']}; color: {p['text']}; border: none; }}
        {s} QWidget {{ background: transparent; color: {p['text']}; }}
        {s} QScrollArea {{ border: none; background: transparent; }}
        {s} QLabel {{ border: none; background: transparent; font-size: 13px; }}
        {s} QFrame#preferenceHero {{ background: {p['surface']}; border: 1px solid {p['border']};
            border-left: 3px solid {p['accent']}; border-radius: 12px; }}
        {s} QLabel#preferenceEyebrow {{ color: {p['accent']}; font-size: 12px; font-weight: 600; }}
        {s} QLabel#preferenceTitle {{ font-size: 26px; font-weight: 700; }}
        {s} QLabel#preferenceDescription, {s} QLabel#settingsHint, {s} QLabel#apiMuted {{ color: {p['muted']}; }}
        {s} QFrame#settingsCard, {s} QFrame#apiModelCard, {s} QFrame#apikeyPanel {{
            background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 12px; }}
        {s} QFrame#apiModelCard[expanded="true"] {{ border-color: {p['accent']}; }}
        {s} QLabel#settingsCardTitle {{ font-size: 16px; font-weight: 600; }}
        {s} QTabBar#preferenceTabs {{ background: {p['surface']}; border-radius: 9px; }}
        {s} QTabBar#preferenceTabs::tab {{ color: {p['muted']}; background: transparent;
            border: none; border-radius: 6px; margin: 4px; padding: 10px 18px; font-size: 13px; }}
        {s} QTabBar#preferenceTabs::tab:selected {{ background: {p['accent_soft']}; color: {p['accent']}; font-weight: 600; }}
        {s} QTabBar#preferenceTabs::tab:hover {{ color: {p['text']}; }}
        {s} QLineEdit, {s} QComboBox, {s} QSpinBox, {s} QTextEdit {{
            background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']};
            border-radius: 8px; padding: 8px 10px; min-height: 20px; font-size: 13px;
            selection-background-color: {p['accent']}; selection-color: white; }}
        {s} QLineEdit:focus, {s} QComboBox:focus, {s} QSpinBox:focus, {s} QTextEdit:focus {{ border-color: {p['accent']}; }}
        {s} QCheckBox {{ spacing: 9px; padding: 4px 0; font-size: 13px; }}
        {s} QCheckBox::indicator {{ width: 17px; height: 17px; border: 1px solid {p['muted']};
            border-radius: 4px; background: {p['input']}; }}
        {s} QCheckBox::indicator:checked {{ image: url("{check}"); background: {p['accent']}; border-color: {p['accent']}; }}
        {s} QCheckBox::indicator:hover {{ border-color: {p['accent']}; }}
        {s} QLineEdit:read-only {{ color: {p['muted']}; }}
        {s} QComboBox QAbstractItemView {{ background: {p['surface']}; color: {p['text']};
            selection-background-color: {p['accent_soft']}; selection-color: {p['text']}; border: 1px solid {p['border']}; }}
        {s} QComboBox::drop-down {{ border: none; width: 30px; }}
        {s} QComboBox::down-arrow {{ image: url("{down}"); width: 16px; height: 16px; }}
        {s} QComboBox QLineEdit {{ border: none; background: transparent; padding: 0; }}
        {s} QSpinBox {{ padding-right: 26px; }}
        {s} QSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 24px; border: none; }}
        {s} QSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 24px; border: none; }}
        {s} QSpinBox::up-arrow {{ image: url("{up}"); width: 12px; height: 12px; }}
        {s} QSpinBox::down-arrow {{ image: url("{down}"); width: 12px; height: 12px; }}
        {s} QPushButton {{ background: {p['input']}; color: {p['text']}; border: 1px solid {p['border']};
            border-radius: 8px; padding: 8px 14px; font-size: 13px; font-weight: 500; }}
        {s} QPushButton:hover {{ background: {p['hover']}; border-color: {p['accent']}; }}
        {s} QPushButton:focus {{ border-color: {p['accent']}; }}
        {s} QPushButton:pressed {{ background: {p['accent_soft']}; }}
        {s} QPushButton:disabled {{ color: {p['muted']}; }}
        {s} QPushButton#apiPrimaryButton {{ background: {p['accent']}; color: white; border-color: {p['accent']}; font-weight: 600; }}
        {s} QToolButton#apiCardToggle {{ border: none; background: transparent; text-align: left;
            font-size: 16px; font-weight: 600; padding: 4px 0; color: {p['text']}; }}
        {s} QToolButton#apiCardToggle:hover {{ color: {p['accent']}; }}
        {s} QToolButton#apiCardToggle:focus {{ color: {p['accent']}; }}
        {s} QWidget#apiKeyRow {{ background: {p['input']}; border: 1px solid {p['border']}; border-radius: 8px; }}
        {s} QLabel#apiStateBadge {{ background: {p['input']}; color: {p['muted']};
            border-radius: 6px; padding: 5px 9px; font-size: 11px; }}
        {s} QLabel#apiProbeStatus {{ color: {p['muted']}; font-size: 12px; padding: 4px 0; }}
        {s} QLabel#apiStateBadge[state="success"], {s} QLabel#apiProbeStatus[state="success"] {{ color: {p['success']}; }}
        {s} QLabel#apiStateBadge[state="error"], {s} QLabel#apiProbeStatus[state="error"] {{ color: {p['danger']}; }}
        {s} QLabel#apiStateBadge[state="busy"], {s} QLabel#apiProbeStatus[state="busy"] {{ color: {p['accent']}; }}
        {s} QScrollBar:vertical {{ width: 8px; background: transparent; margin: 2px; }}
        {s} QScrollBar::handle:vertical {{ background: {p['border']}; border-radius: 3px; min-height: 30px; }}
        {s} QScrollBar::add-line:vertical, {s} QScrollBar::sub-line:vertical {{ height: 0; }}
        {s} QScrollBar::add-page:vertical, {s} QScrollBar::sub-page:vertical {{ background: transparent; }}
    '''


def apply_settings_theme(window, mode):
    page = getattr(window, 'settings_page', None)
    if page is not None:
        page.setStyleSheet(stylesheet('settings_page_root', mode))
