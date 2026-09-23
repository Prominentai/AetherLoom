"""Local NovelAI prompt suggestion preferences, separate from drawing options."""
from PyQt5 import QtCore, QtWidgets

from .styles import workspace_palette, workspace_stylesheet


def normalize_suggestion_preferences(value):
    """Only JSON booleans override the defaults; strings and numbers never do."""
    value = value if isinstance(value, dict) else {}
    return {key: value[key] if isinstance(value.get(key), bool) else True
            for key in ('online', 'local')}


class SuggestionPreferencesDialog(QtWidgets.QDialog):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.setObjectName('novelaiPreferences')
        self.setWindowTitle('NovelAI 设置')
        self.setMinimumWidth(280)
        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 800, 600)
        self.resize(min(430, max(280, available.width() - 40)), 330)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)
        heading = QtWidgets.QLabel('提示词候选')
        heading.setObjectName('suggestionPreferencesHeading')
        root.addWidget(heading)
        settings = normalize_suggestion_preferences(page.prompt_suggestion_preferences)
        for key, title, description in (
                ('online', '在线标签候选', '使用当前模型及 Anime / Furry 数据集，需要先在连接设置中添加 API Key。'),
                ('local', '本地标签候选', '使用客户端本地词库，无需连接；可与在线候选同时启用。')):
            box = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(box)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(5)
            checkbox = QtWidgets.QCheckBox(title)
            checkbox.setObjectName('suggestionPreference_' + key)
            checkbox.setChecked(settings[key])
            setattr(self, key, checkbox)
            layout.addWidget(checkbox)
            note = QtWidgets.QLabel(description)
            note.setWordWrap(True)
            note.setObjectName('suggestionPreferencesNote')
            layout.addWidget(note)
            root.addWidget(box)
        note = QtWidgets.QLabel('NovelAI 本地候选使用空格，括号不转义；与设置中心的补全参数独立。')
        note.setObjectName('suggestionPreferencesNote')
        note.setWordWrap(True)
        root.addWidget(note)
        self.error = QtWidgets.QLabel()
        self.error.setObjectName('suggestionPreferencesError')
        self.error.setTextFormat(QtCore.Qt.PlainText)
        self.error.setWordWrap(True)
        self.error.hide()
        root.addWidget(self.error)
        root.addStretch(1)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        self.save_button = buttons.button(QtWidgets.QDialogButtonBox.Save)
        self.save_button.setText('保存')
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText('取消')
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        mode = getattr(page.owner, '_theme_mode', 'dark')
        p = workspace_palette(mode)
        self.setStyleSheet(workspace_stylesheet(mode, '#novelaiPreferences') + f'''
            QDialog#novelaiPreferences {{background:{p['surface']};color:{p['text']};}}
            #novelaiPreferences QLabel {{color:{p['text']};background:transparent;}}
            #novelaiPreferences QLabel#suggestionPreferencesHeading {{font-size:15px;font-weight:600;}}
            #novelaiPreferences QLabel#suggestionPreferencesNote {{color:{p['muted']};font-size:12px;}}
            #novelaiPreferences QLabel#suggestionPreferencesError {{color:{p['danger']};font-size:12px;}}
            #novelaiPreferences QCheckBox {{color:{p['text']};background:transparent;}}
        ''')

    def save(self):
        values = dict(online=self.online.isChecked(), local=self.local.isChecked())
        try:
            self.page.save_prompt_suggestion_preferences(values)
        except (OSError, ValueError) as error:
            self.error.setText('设置未能保存，原设置保持不变。\n' + str(error))
            self.error.show()
            return
        self.accept()
