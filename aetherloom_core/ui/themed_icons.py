"""Render the navigation's source SVGs in theme colors at the target DPI."""
from pathlib import Path
from PyQt5 import QtCore, QtGui, QtSvg
from aetherloom_core.rh_ui import palette
from aetherloom_core.paths import resource_path


class StrokeIcon(QtGui.QIconEngine):
    def __init__(self, source, colors):
        super().__init__()
        self.source, self.colors = source, colors
        self.renderers = {key: QtSvg.QSvgRenderer(source.replace('#64748b', value).encode('utf8'))
                          for key, value in colors.items()}

    def clone(self):
        return StrokeIcon(self.source, self.colors)

    def paint(self, painter, rect, mode, state):
        color = 'accent' if state == QtGui.QIcon.On else 'text' if mode == QtGui.QIcon.Active else 'muted'
        painter.save()
        if mode == QtGui.QIcon.Disabled:painter.setOpacity(.4)
        self.renderers[color].render(painter, QtCore.QRectF(rect))
        painter.restore()

    def pixmap(self, size, mode, state):
        pixmap = QtGui.QPixmap(size);pixmap.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(pixmap)
        self.paint(painter, pixmap.rect(), mode, state)
        painter.end()
        return pixmap


def refresh_navigation(owner, mode):
    colors = palette(mode)
    colors = {key: colors[key] for key in ('muted', 'text', 'accent')}
    for name, file in [('home_btn', 'home_icon'), ('runninghub_btn', 'runninghub'),
                       ('rh_models_btn', 'rh_models'), ('canvas_btn', 'canvas'),
                       ('decode_btn', 'local_decoding'), ('local_btn', 'local_files'),
                       ('api_btn', 'api'), ('settings_btn', 'setting')]:
        button = getattr(owner, name, None)
        path = Path(resource_path('icons', file + '.svg'))
        if button is not None and path.is_file():
            button.setIcon(QtGui.QIcon(StrokeIcon(path.read_text(encoding='utf8'), colors)))
    reveal = getattr(owner, 'navigation_reveal', None)
    if reveal is not None:reveal.update()
