"""Native model gallery cards with explicit version selection and cover preview."""
from PyQt5 import QtCore, QtGui, QtWidgets

from .rh_ui import palette


class ModelTabs(QtWidgets.QTabBar):
    def wheelEvent(self, event):
        # Browsing a gallery must not change the model version under the cursor.
        event.ignore()


def model_versions(record):
    """Follow RH AIModel: selected versionResourceName's final path component."""
    values = record.get('versions')
    result = []
    for index, value in enumerate(values if isinstance(values, list) else []):
        if not isinstance(value, dict) or value.get('syncStatus') in ('SYNCING', 'SYNC_FAILED'):
            continue
        path = value.get('versionResourceName')
        token = path.rsplit('/', 1)[-1] if isinstance(path, str) else ''
        if not token and index == 0:
            token = record.get('nodeModelName') or ''
        result.append(dict(value, node_token=token if isinstance(token, str) else '', source_index=index))
    if not values and isinstance(record.get('nodeModelName'), str) and record['nodeModelName'].strip():
        result.append(dict(version='默认版本', node_token=record['nodeModelName'], source_index=0))
    return result


def cover_url(record, version):
    posters = version.get('posterInfos')
    if isinstance(posters, list):
        for poster in posters:
            if isinstance(poster, dict):
                value = poster.get('thumbnailUrl') or poster.get('posterUrl')
                if isinstance(value, str) and value:
                    return value
    return str(record.get('thumbnailUrl') or record.get('posterUrl') or '') if version.get('source_index', 0) == 0 else ''


def model_title(record):
    # Some public API rows have resourceName=null although their file is valid.
    name = record.get('resourceName')
    if isinstance(name, str) and name.strip():
        return name
    token = record.get('nodeModelName') or next((v['node_token'] for v in model_versions(record) if v['node_token']), '')
    return str(token).rsplit('.', 1)[0] or '未命名模型'


def website_url(site, record):
    import re
    local = record.get('_local_favorite') or {}
    identity = str(local.get('remote_id') or record.get('id') or '')
    if site not in ('https://www.runninghub.cn','https://www.runninghub.ai') or not re.fullmatch(r'\d{1,30}', identity):
        return ''
    visibility = local.get('visibility') or ('self' if local.get('bucket') == 'uploads' else 'public')
    return site+'/model/'+('self' if visibility=='self' else 'public')+'/'+identity


class Cover(QtWidgets.QWidget):
    clicked = QtCore.pyqtSignal()

    def __init__(self, card):
        super().__init__(card)
        self.card = card
        self.image = QtGui.QImage()
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setMinimumHeight(160)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

    def paintEvent(self, event):
        p = palette(self.card.picker.mode())
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QtCore.QRectF(self.rect()).adjusted(0, 0, -1, -1)
        clip = QtGui.QPainterPath(); clip.addRoundedRect(rect, 9, 9)
        painter.setClipPath(clip)
        painter.fillRect(rect, QtGui.QColor(p['input']))
        if not self.image.isNull():
            ratio = max(self.width() / self.image.width(), self.height() / self.image.height())
            width, height = self.image.width() * ratio, self.image.height() * ratio
            painter.drawImage(QtCore.QRectF((self.width()-width)/2, (self.height()-height)/2, width, height), self.image)
        else:
            painter.setPen(QtGui.QColor(p['muted']))
            missing = not self.card.url or self.card.url in self.card.picker.thumbs.failed
            preview_rect = rect.adjusted(0,0,0,-max(185,self.card.overlay.sizeHint().height()+65))
            painter.drawText(preview_rect, QtCore.Qt.AlignCenter, '暂无封面' if missing else '封面加载中')
        shade_height = max(185, self.card.overlay.sizeHint().height()+65)
        shade = QtGui.QLinearGradient(0, self.height()-shade_height, 0, self.height())
        shade.setColorAt(0, QtGui.QColor(8, 13, 20, 0))
        shade.setColorAt(.2, QtGui.QColor(8, 13, 20, 200))
        shade.setColorAt(1, QtGui.QColor(8, 13, 20, 235))
        painter.fillRect(QtCore.QRectF(0, max(0,self.height()-shade_height), self.width(), shade_height), shade)
        for text, top in ((self.card.record.get('resourceType',''), True), (str(self.card.version.get('baseModel') or ''), False)):
            if not text:
                continue
            font = painter.font(); font.setPixelSize(11); painter.setFont(font)
            text = painter.fontMetrics().elidedText(text, QtCore.Qt.ElideRight, self.width()-30)
            width = painter.fontMetrics().horizontalAdvance(text)+16
            badge = QtCore.QRectF(9 if top else self.width()-width-9, 9, width, 23)
            painter.setPen(QtCore.Qt.NoPen);painter.setBrush(QtGui.QColor(15, 20, 28, 200));painter.drawRoundedRect(badge, 5, 5)
            painter.setPen(QtGui.QColor('white'));painter.drawText(badge, QtCore.Qt.AlignCenter, text)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _ElidedLabel(QtWidgets.QLabel):
    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setPen(self.palette().color(QtGui.QPalette.WindowText))
        painter.drawText(self.rect(), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
            self.fontMetrics().elidedText(self.text(), QtCore.Qt.ElideRight, self.width()))


class ModelCard(QtWidgets.QFrame):
    def __init__(self, record, picker):
        super().__init__(picker.content)
        self.record, self.picker = record, picker
        self.versions = model_versions(record)
        self.version = {}
        self.url = ''
        self.setObjectName('rhModelCard')
        self.setMinimumWidth(205)
        self.setFixedHeight(320)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(5, 5, 5, 5);box.setSpacing(0)
        self.cover = Cover(self)
        self.cover.clicked.connect(self.select)
        box.addWidget(self.cover, 1)
        cover_box = QtWidgets.QVBoxLayout(self.cover)
        cover_box.setContentsMargins(7, 0, 7, 8);cover_box.addStretch()
        overlay = QtWidgets.QWidget(self.cover);overlay.setObjectName('rhModelOverlay')
        self.overlay = overlay
        info_box = QtWidgets.QVBoxLayout(overlay);info_box.setContentsMargins(3, 0, 3, 0);info_box.setSpacing(6)
        cover_box.addWidget(overlay)
        self.name = _ElidedLabel(model_title(record))
        self.name.setObjectName('rhModelName');self.name.setTextFormat(QtCore.Qt.PlainText)
        self.name.setToolTip(self.name.text());self.name.setFixedHeight(23)
        self.name.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        name_row = QtWidgets.QHBoxLayout();name_row.addWidget(self.name, 1)
        self.website = QtWidgets.QPushButton('↗');self.website.setObjectName('rhModelSecondary')
        self.website.setFixedWidth(30);self.website.setToolTip('在默认浏览器中打开模型官网页面')
        self.website_url = website_url(picker.settings.host, record)
        self.website.setEnabled(bool(self.website_url));self.website.clicked.connect(self.open_website)
        if not self.website_url:self.website.setToolTip('此自建记录没有关联官网模型地址')
        name_row.addWidget(self.website);info_box.addLayout(name_row)
        self.filename = _ElidedLabel()
        self.filename.setObjectName('rhModelMuted');self.filename.setFixedHeight(18)
        self.filename.setTextFormat(QtCore.Qt.PlainText)
        self.filename.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        info_box.addWidget(self.filename)
        self.tabs = ModelTabs()
        self.tabs.setObjectName('rhModelVersions')
        self.tabs.setExpanding(False);self.tabs.setUsesScrollButtons(True)
        self.tabs.setDrawBase(False);self.tabs.setElideMode(QtCore.Qt.ElideRight)
        for version in self.versions:
            idx = self.tabs.addTab(str(version.get('version') or '默认版本'))
            self.tabs.setTabToolTip(idx, str(version.get('node_token') or '缺少模型文件名'))
        info_box.addWidget(self.tabs)
        row = QtWidgets.QHBoxLayout()
        self.favorite = QtWidgets.QPushButton('☆')
        self.favorite.setObjectName('rhModelFavorite');self.favorite.setFixedWidth(34)
        self.favorite.clicked.connect(lambda:picker.toggle_favorite(self))
        self.info = QtWidgets.QPushButton('详情')
        self.info.setObjectName('rhModelSecondary')
        self.info.clicked.connect(lambda: picker.show_details(self))
        self.use = QtWidgets.QPushButton('立即使用')
        self.use.setObjectName('rhModelPrimary')
        self.use.clicked.connect(lambda: picker.use_card(self))
        row.addWidget(self.favorite);row.addWidget(self.info);row.addWidget(self.use, 1);info_box.addLayout(row)
        local = record.get('_local_favorite')
        if local:
            management = QtWidgets.QHBoxLayout();management.setSpacing(4)
            edit = QtWidgets.QPushButton('编辑');edit.setObjectName('rhModelSecondary')
            edit.clicked.connect(lambda:picker.edit_favorite(local))
            pin = QtWidgets.QPushButton('取消置顶' if local.get('pinned') else '置顶');pin.setObjectName('rhModelSecondary')
            pin.clicked.connect(lambda:picker.pin_favorite(self))
            management.addWidget(edit);management.addWidget(pin);info_box.addLayout(management)
            if local.get('bucket')=='uploads':
                remove=QtWidgets.QPushButton('移除');remove.setObjectName('rhModelSecondary')
                remove.setToolTip('仅移除本地上传记录');remove.clicked.connect(lambda:picker.remove_local(local));management.addWidget(remove)
        for button in self.findChildren(QtWidgets.QPushButton):button.setAutoDefault(False)
        self.tabs.currentChanged.connect(self.set_version)
        initial = next((i for i,v in enumerate(self.versions) if v['node_token'] == picker.current_value), 0)
        self.tabs.setCurrentIndex(initial)
        self.set_version(initial)

    def set_version(self, index):
        self.version = self.versions[index] if 0 <= index < len(self.versions) else {}
        token = self.version.get('node_token') or ''
        self.filename.setText(token or '缺少模型文件名')
        self.filename.setToolTip(token)
        self.url = cover_url(self.record, self.version)
        self.local_cover = self.picker.favorites.cover_info(self.record.get('_local_favorite') or {})
        if self.local_cover:self.url = self.local_cover[0]
        self.cover.image = QtGui.QImage()
        self.use.setEnabled(bool(token) and (self.picker.library or self.record.get('resourceType') == self.picker.resource_type))
        self.use.setText('复制名称' if self.picker.library else '使用当前模型' if token and token == self.picker.current_value else '立即使用')
        self.refresh_favorite()
        self.cover.update()
        self.picker.schedule_visible()

    def refresh_favorite(self):
        token = self.version.get('node_token') or ''
        try:
            value = self.picker.favorites.lookup(self.picker.settings.host,self.record.get('resourceType'),token) if token else None
            self.favorite.setText('★' if value else '☆')
            self.favorite.setToolTip('移除本地收藏' if value else '收藏此版本到本地')
            self.favorite.setEnabled(bool(token))
        except Exception:
            self.favorite.setEnabled(False);self.favorite.setToolTip('收藏文件无法读取，请在本地收藏中查看错误')

    def select(self):
        self.picker.selected_card = self
        self.picker.show_details(self)

    def open_website(self):
        if self.website_url:QtGui.QDesktopServices.openUrl(QtCore.QUrl(self.website_url))

    def set_visible_image(self, visible):
        if not visible:
            if not self.cover.image.isNull():
                self.cover.image = QtGui.QImage();self.cover.update()
            return
        image = self.picker.thumbs.image(self.url)
        if image is not None and self.cover.image.cacheKey() != image.cacheKey():
            self.cover.image = image;self.cover.update()
        elif image is None:
            if self.local_cover:self.picker.thumbs.request_local(*self.local_cover)
            else:self.picker.thumbs.request(self.url)
