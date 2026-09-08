"""Compact home page and bounded, asynchronous repository README updates."""
import base64
import html
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urljoin, urlparse

import requests
from PyQt5 import QtCore, QtGui, QtWidgets

from aetherloom_core import __version__

REPOSITORY = 'https://github.com/Prominentai/AetherLoom'
README_URL = REPOSITORY + '/blob/main/README.md'
RAW_URL = 'https://raw.githubusercontent.com/Prominentai/AetherLoom/main/README.md'
API_URL = 'https://api.github.com/repos/Prominentai/AetherLoom/readme'
MAX_BYTES = 1024 * 1024
REFRESH_AGE = 3600


def _atomic_write(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.isfile(temporary):
            os.unlink(temporary)


def _read_bounded(path):
    with open(path, 'rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('README 超过大小限制')
    return data.decode('utf-8-sig')


def _valid_markdown(text):
    if not text.strip() or '\x00' in text:
        raise ValueError('README 内容为空或无效')
    if re.match(r'\s*(?:<!doctype\s+html|<html\b)', text, flags=re.I):
        raise ValueError('服务器返回了网页而非 README')
    return text


def display_markdown(text):
    """Keep screenshots as links: no remote/full-size media load on the GUI thread."""
    text = re.sub(r'<svg\b[\s\S]*?</svg\s*>', '', text, flags=re.I)
    text = re.sub(r'<(?:script|style)\b[\s\S]*?</(?:script|style)\s*>', '', text, flags=re.I)

    def image_link(url, label='查看图片'):
        url = html.unescape(url).strip()
        if urlparse(url).scheme not in ('', 'http', 'https'):
            return ''
        url = urljoin(RAW_URL, url)
        return '[%s](<%s>)' % (label.replace('[', '').replace(']', ''), url.replace('>', '%3E'))

    def html_image(match):
        src = re.search(r'\bsrc\s*=\s*[\"\']([^\"\']+)', match.group(), re.I)
        return image_link(src.group(1)) if src else ''

    text = re.sub(r'<img\b[^>]*>', html_image, text, flags=re.I)
    text = re.sub(r'!\[([^\]]*)\]\(<?([^\s)>]+)>?(?:\s+[\"\'][^\n]*?[\"\'])?\)',
                  lambda match: image_link(match.group(2), '查看图片' + (' · ' + match.group(1) if match.group(1) else '')),
                  text)
    # Reference-style images should remain clickable too, without resource loading.
    return re.sub(r'!\[([^\]]*)\](\[[^\]]*\])', r'[查看图片 · \1]\2', text)


class ReadmeBrowser(QtWidgets.QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._open_link)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setMinimumSize(0, 150)
        self.setLineWrapMode(QtWidgets.QTextEdit.WidgetWidth)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.document().setBaseUrl(QtCore.QUrl(REPOSITORY + '/blob/main/'))
        self.accent = '#8bceef'

    def style_document(self, accent=None):
        if accent:
            self.accent = accent
        links = []
        block = self.document().begin()
        while block.isValid():
            level = block.blockFormat().headingLevel()
            if level:
                cursor = QtGui.QTextCursor(block)
                cursor.select(QtGui.QTextCursor.BlockUnderCursor)
                fmt = QtGui.QTextCharFormat()
                fmt.setFontPointSize(16 if level == 1 else 14)
                cursor.mergeCharFormat(fmt)
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid() and fragment.charFormat().isAnchor():
                    links.append((fragment.position(), fragment.length()))
                iterator += 1
            block = block.next()
        for position, length in links:
            cursor = QtGui.QTextCursor(self.document())
            cursor.setPosition(position)
            cursor.setPosition(position + length, QtGui.QTextCursor.KeepAnchor)
            fmt = QtGui.QTextCharFormat()
            fmt.setForeground(QtGui.QColor(self.accent))
            cursor.mergeCharFormat(fmt)

    def loadResource(self, resource_type, url):
        # QTextDocument must never decode arbitrarily large README images.
        if resource_type == QtGui.QTextDocument.ImageResource:
            return QtGui.QImage()
        return super().loadResource(resource_type, url)

    def _open_link(self, url):
        if not url.scheme() and url.fragment() and not url.path():
            self.scrollToAnchor(url.fragment())
            return
        if url.isRelative():
            url = self.document().baseUrl().resolved(url)
        if url.scheme() in ('https', 'http'):
            QtGui.QDesktopServices.openUrl(url)


class ReadmeController(QtCore.QObject):
    finished = QtCore.pyqtSignal(object)

    def __init__(self, page, directory):
        super().__init__(page)
        self.page = page
        self.directory = Path(directory)
        self.cache = self.directory / '.readme_cache.md'
        self.metadata_path = self.directory / '.readme_cache.json'
        self.metadata = {}
        self.text = ''
        self.busy = False
        self.closed = threading.Event()
        try:
            self.metadata = json.loads(_read_bounded(self.metadata_path))
            if not isinstance(self.metadata, dict):
                self.metadata = {}
        except (OSError, ValueError, UnicodeError):
            pass
        self._load_local()
        self.finished.connect(self._finished, QtCore.Qt.QueuedConnection)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(800)

    def _load_local(self):
        for path in (self.cache, self.directory / 'README.md'):
            try:
                content = _valid_markdown(_read_bounded(path))
            except (OSError, ValueError, UnicodeError):
                continue
            self._display(content)
            self.page.readme_status.setText('已加载缓存' if path == self.cache else '本地说明 · 自动检查更新')
            return
        self.page.readme_status.setText('正在准备项目说明')
        self._display('## 欢迎使用 AetherLoom\n\n打开 RunningHub 添加应用，或在画布中连接应用与素材。')

    def _display(self, content):
        if content == self.text:
            return
        bar = self.page.readme.verticalScrollBar()
        position = bar.value()
        self.text = content
        self.page.readme.setMarkdown(display_markdown(content))
        self.page.readme.style_document()
        bar.setValue(min(position, bar.maximum()))

    def refresh(self, force=False):
        if self.closed.is_set() or self.busy:
            return
        try:
            age = time.time() - float(self.metadata.get('checked_at', 0))
        except (TypeError, ValueError):
            age = REFRESH_AGE
        if not force and 0 <= age < REFRESH_AGE and self.cache.is_file():
            self.page.readme_status.setText('已是最新缓存 · ' + time.strftime('%H:%M', time.localtime(float(self.metadata['checked_at']))))
            self.timer.start(max(1000, int((REFRESH_AGE - age) * 1000)))
            return
        self.busy = True
        self.page.refresh_button.setEnabled(False)
        self.page.readme_status.setText('正在从 GitHub 更新…')
        threading.Thread(target=self._fetch, args=(dict(self.metadata),), name='readme-refresh', daemon=True).start()

    def _fetch(self, metadata):
        result = dict(ok=False, message='网络暂不可用，保留当前说明')
        for url in (RAW_URL, API_URL):
            if self.closed.is_set():
                return
            headers = {'Accept': 'application/vnd.github.raw+json', 'User-Agent': 'AetherLoom/' + __version__}
            if metadata.get('url') == url and self.cache.is_file():
                if metadata.get('etag'):
                    headers['If-None-Match'] = str(metadata['etag'])
                elif metadata.get('last_modified'):
                    headers['If-Modified-Since'] = str(metadata['last_modified'])
            try:
                started = time.monotonic()
                with requests.get(url, headers=headers, timeout=(3, 7), stream=True) as response:
                    if response.status_code == 304:
                        content = _valid_markdown(_read_bounded(self.cache))
                    else:
                        response.raise_for_status()
                        if int(response.headers.get('Content-Length', 0)) > MAX_BYTES:
                            raise ValueError('README 超过大小限制')
                        data = bytearray()
                        for chunk in response.iter_content(16384):
                            if self.closed.is_set():
                                return
                            data.extend(chunk)
                            if len(data) > MAX_BYTES or time.monotonic() - started > 15:
                                raise ValueError('README 响应超过限制')
                        content = bytes(data).decode('utf-8-sig')
                        if url == API_URL and content.lstrip().startswith('{'):
                            payload = json.loads(content)
                            if not isinstance(payload, dict) or payload.get('encoding') != 'base64':
                                raise ValueError('GitHub README 响应无效')
                            content = base64.b64decode(payload['content']).decode('utf-8-sig')
                        _valid_markdown(content)
                    updated = dict(url=url, checked_at=time.time(),
                                   etag=response.headers.get('ETag', metadata.get('etag', '') if metadata.get('url') == url else ''),
                                   last_modified=response.headers.get('Last-Modified', ''))
                if self.closed.is_set():
                    return
                message = '已更新 · ' + time.strftime('%H:%M')
                try:
                    _atomic_write(self.cache, content)
                    _atomic_write(self.metadata_path, json.dumps(updated, ensure_ascii=False))
                except OSError:
                    message = '已更新 · 本次未能写入缓存'
                result = dict(ok=True, content=content, metadata=updated, message=message)
                break
            except (requests.RequestException, OSError, ValueError, KeyError, TypeError, UnicodeError):
                continue
        if not self.closed.is_set():
            try:
                self.finished.emit(result)
            except RuntimeError:
                pass

    def _finished(self, result):
        if self.closed.is_set():
            return
        self.busy = False
        self.page.refresh_button.setEnabled(True)
        if result['ok']:
            self.metadata = result['metadata']
            self._display(result['content'])
        self.page.readme_status.setText(result['message'])
        self.timer.start(REFRESH_AGE * 1000 if result['ok'] else 5 * 60 * 1000)

    def close(self):
        self.closed.set()
        self.timer.stop()


class HomePage(QtWidgets.QWidget):
    def __init__(self, owner, directory):
        super().__init__()
        self.setObjectName('aetherHome')
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(16)
        self.hero = QtWidgets.QHBoxLayout()
        self.hero.setSpacing(16)
        self.logo = QtWidgets.QLabel()
        self.logo.setFixedSize(72, 72)
        self.logo.setPixmap(QtGui.QIcon(str(Path(directory) / 'icons' / 'home_emblem.svg')).pixmap(72, 72))
        self.hero.addWidget(self.logo)
        heading = QtWidgets.QVBoxLayout()
        heading.setSpacing(4)
        title = QtWidgets.QLabel('AetherLoom')
        title.setObjectName('homeHeading')
        heading.addWidget(title)
        self.subtitle = QtWidgets.QLabel('连接云端应用，在本地组织创作。')
        self.subtitle.setObjectName('homeMuted')
        self.subtitle.setWordWrap(True)
        heading.addWidget(self.subtitle)
        self.hero.addLayout(heading, 1)
        version = QtWidgets.QLabel('v' + __version__)
        version.setObjectName('homeVersion')
        self.hero.addWidget(version, 0, QtCore.Qt.AlignTop)
        layout.addLayout(self.hero)
        self.actions = QtWidgets.QHBoxLayout()
        self.actions.setSpacing(10)
        for label, description, attr in (
                ('RunningHub', '添加与运行应用', 'runninghub_btn'),
                ('画布', '连接节点，编排工作流', 'canvas_btn'),
                ('本地文件', '浏览素材与生成结果', 'local_btn')):
            button = QtWidgets.QPushButton(label + '\n' + description)
            button.setObjectName('homeAction')
            button.setMinimumWidth(0)
            button.setMinimumHeight(58)
            button.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            button.setCursor(QtCore.Qt.PointingHandCursor)
            button.clicked.connect(lambda checked=False, name=attr: getattr(owner, name).click())
            self.actions.addWidget(button, 1)
        layout.addLayout(self.actions)
        panel = QtWidgets.QFrame()
        panel.setObjectName('homeDocument')
        doc_layout = QtWidgets.QVBoxLayout(panel)
        doc_layout.setContentsMargins(16, 12, 16, 10)
        doc_layout.setSpacing(6)
        toolbar = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel('项目说明')
        label.setObjectName('homeSection')
        toolbar.addWidget(label)
        toolbar.addStretch(1)
        self.refresh_button = QtWidgets.QPushButton('刷新')
        self.refresh_button.setToolTip('检查 GitHub README 更新')
        toolbar.addWidget(self.refresh_button)
        open_button = QtWidgets.QPushButton('GitHub ↗')
        open_button.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(README_URL)))
        toolbar.addWidget(open_button)
        doc_layout.addLayout(toolbar)
        self.readme_status = QtWidgets.QLabel()
        self.readme_status.setObjectName('homeMuted')
        self.readme_status.setWordWrap(True)
        doc_layout.addWidget(self.readme_status)
        self.readme = ReadmeBrowser()
        self.readme.setObjectName('homeReadme')
        doc_layout.addWidget(self.readme, 1)
        layout.addWidget(panel, 1)
        footer = QtWidgets.QLabel()
        self.footer = footer
        footer.setWordWrap(True)
        footer.setOpenExternalLinks(True)
        footer.setObjectName('homeMuted')
        layout.addWidget(footer)
        self.set_theme(getattr(owner, '_theme_mode', 'dark'))
        self.controller = ReadmeController(self, directory)
        self.refresh_button.clicked.connect(lambda: self.controller.refresh(force=True))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = self.width() < 520
        self.logo.setVisible(not narrow)
        self.actions.setDirection(QtWidgets.QBoxLayout.TopToBottom if narrow else QtWidgets.QBoxLayout.LeftToRight)

    def set_theme(self, mode):
        light = mode == 'light'
        text, muted, surface, border, accent = (('#172b46', '#586d87', '#ffffff', '#d8e3ef', '#146eac') if light
                                               else ('#e5edf9', '#9aadc7', '#182235', '#2d3d54', '#8bceef'))
        self.setStyleSheet('''
            QWidget#aetherHome { background: %s; }
            QWidget#aetherHome QLabel { color: %s; background: transparent; }
            QLabel#homeHeading { font-size: 34px; font-weight: 700; }
            QWidget#aetherHome QLabel#homeMuted { color: %s; font-size: 12px; }
            QLabel#homeVersion { color: %s; border: 1px solid %s; border-radius: 8px; padding: 4px 9px; }
            QLabel#homeSection { font-size: 16px; font-weight: 600; }
            QFrame#homeDocument { background: %s; border: 1px solid %s; border-radius: 12px; }
            QWidget#aetherHome QPushButton { background: %s; color: %s; border: 1px solid %s; border-radius: 8px; padding: 6px 12px; }
            QWidget#aetherHome QPushButton:hover { border-color: %s; }
            QWidget#aetherHome QPushButton:disabled { color: %s; }
            QPushButton#homeAction { font-size: 13px; text-align: left; padding: 10px 14px; }
            QTextBrowser#homeReadme { background: transparent; color: %s; border: none; font-size: 13px; selection-background-color: #386b9c; }
        ''' % ('#f3f6fb' if light else '#101827', text, muted, accent, border, surface, border, surface, text, border, accent, muted, text))
        self.footer.setText('<a style="color:%s" href="%s">项目仓库</a>  ·  '
                            '<a style="color:%s" href="https://www.runninghub.ai/user-center/1911823721911500801/webapp?inviteCode=rh-v1380">作者的 RunningHub 应用 ↗</a>' % (accent, REPOSITORY, accent))
        self.readme.document().setDefaultStyleSheet('body { color: %s; } a { color: %s; } h1 { font-size: 21px; } h2 { font-size: 18px; } pre { white-space: pre-wrap; }' % (text, accent))
        self.readme.style_document(accent)

    def close_updates(self):
        self.controller.close()
