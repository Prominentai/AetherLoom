"""Bounded drawing history with disposable storage for large image snapshots."""
import tempfile

from PIL import Image
from PyQt5 import QtGui


class _StoredImage:
    def __init__(self, image):
        # TemporaryFile is deleted on close, including process termination on Windows.
        self.stream = tempfile.TemporaryFile(prefix='aetherloom-drawing-history-')
        self.qt_format = None
        try:
            if isinstance(image, QtGui.QImage):
                self.qt_format = (image.width(), image.height(), image.format())
                bits = image.constBits()
                bits.setsize(image.byteCount())
                self.stream.write(memoryview(bits))
            else:
                image.save(self.stream, format='PNG', compress_level=1)
            self.stream.flush()
        except Exception:
            self.stream.close()
            raise

    def restore(self):
        self.stream.seek(0)
        if self.qt_format is not None:
            image = QtGui.QImage(*self.qt_format)
            if image.isNull():
                raise MemoryError('Unable to allocate drawing history image')
            bits = image.bits()
            bits.setsize(image.byteCount())
            if self.stream.readinto(memoryview(bits)) != image.byteCount():
                raise OSError('Incomplete drawing history snapshot')
            return image
        with Image.open(self.stream) as image:
            return image.copy()


def restore(value):
    if isinstance(value, _StoredImage):
        return value.restore()
    if isinstance(value, (tuple, list)):
        return type(value)(restore(item) for item in value)
    return value


def _size(value):
    if isinstance(value, QtGui.QImage):
        return value.byteCount()
    if isinstance(value, Image.Image):
        return value.width * value.height * len(value.getbands())
    if isinstance(value, (tuple, list)):
        return sum(_size(item) for item in value)
    return 0


def _spill(value):
    if isinstance(value, (QtGui.QImage, Image.Image)) and _size(value):
        return _StoredImage(value)
    if isinstance(value, (tuple, list)):
        return type(value)(_spill(item) for item in value)
    return value


def trim(undo, redo, byte_limit, max_steps=30):
    """Retain the newest steps, spilling old pixel data instead of losing an edit."""
    while len(undo) + len(redo) > max_steps:
        (undo if undo else redo).pop(0)
    used = _size(undo) + _size(redo)
    for stack in (undo, redo):
        for index in range(len(stack)):
            if used <= byte_limit:
                return ''
            size = _size(stack[index])
            if size:
                try:
                    stack[index] = _spill(stack[index])
                except (OSError, MemoryError):
                    # Keep painting usable if the OS cannot allocate temporary storage.
                    while _size(undo) + _size(redo) > byte_limit:
                        (undo if undo else redo).pop(0)
                    return '临时存储不足，部分撤销记录已释放'
                used -= size
    return ''
