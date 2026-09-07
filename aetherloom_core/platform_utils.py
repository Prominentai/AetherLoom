"""Platform integration and optional SVG support."""
import os
import sys
from PyQt5 import QtCore, QtGui
try:
    from PyQt5 import QtSvg
except Exception:
    QtSvg = None

def _api_debug(msg):
    # debug logging disabled; keep stub for compatibility
    return


def _set_native_titlebar_dark(widget, enable):
    """Best-effort request for dark title bars on Windows 10+."""
    try:
        if sys.platform != 'win32' or widget is None:
            return
        hwnd = int(widget.winId()) if hasattr(widget, 'winId') else None
        if not hwnd:
            return
        import ctypes
        dark = ctypes.c_int(1 if enable else 0)
        size = ctypes.sizeof(dark)
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        res = ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark), size)
        if res != 0:
            DWMWA_USE_IMMERSIVE_DARK_MODE = 19
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark), size)
    except Exception:
        pass


def _move_to_trash(paths):
    """Move given paths to recycle bin when possible; fallback to delete."""
    if not paths:
        return 0, []
    if isinstance(paths, (str, bytes)):
        paths = [paths]
    removed = 0
    errors = []
    try:
        from send2trash import send2trash  # type: ignore
    except Exception:
        send2trash = None

    for path in paths:
        try:
            if not path or not os.path.exists(path):
                continue
            if send2trash:
                try:
                    send2trash(path)
                except Exception:
                    # fall through to other methods
                    pass
                else:
                    removed += 1
                    continue

            if sys.platform == 'win32':
                try:
                    import ctypes
                    from ctypes import wintypes

                    FO_DELETE = 3
                    FOF_ALLOWUNDO = 0x40
                    FOF_NOCONFIRMATION = 0x10
                    FOF_NOERRORUI = 0x400
                    FOF_SILENT = 0x4

                    class SHFILEOPSTRUCTW(ctypes.Structure):
                        _fields_ = [
                            ('hwnd', wintypes.HWND),
                            ('wFunc', ctypes.c_uint),
                            ('pFrom', wintypes.LPCWSTR),
                            ('pTo', wintypes.LPCWSTR),
                            ('fFlags', ctypes.c_uint16),
                            ('fAnyOperationsAborted', wintypes.BOOL),
                            ('hNameMappings', ctypes.c_void_p),
                            ('lpszProgressTitle', wintypes.LPCWSTR),
                        ]

                    p = os.path.abspath(path)
                    buf = ctypes.create_unicode_buffer(p + '\0\0')
                    op = SHFILEOPSTRUCTW()
                    op.wFunc = FO_DELETE
                    op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
                    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
                    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
                    ok = (res == 0 and not op.fAnyOperationsAborted)
                    if not ok and os.path.exists(path):
                        os.remove(path)
                except Exception:
                    try:
                        os.remove(path)
                    except Exception as e:
                        errors.append((path, e))
                        continue
            else:
                os.remove(path)

            removed += 1
        except Exception as e:
            errors.append((path, e))

    return removed, errors


def _svg_to_icon(svg_text, size_px):
    """Render inline SVG markup into a QIcon of the requested size."""
    try:
        if not svg_text or size_px <= 0:
            return None
        if QtSvg is None:
            return None
        data = QtCore.QByteArray(svg_text.encode('utf-8'))
        renderer = QtSvg.QSvgRenderer(data)
        if not renderer.isValid():
            return None
        pm = QtGui.QPixmap(size_px, size_px)
        pm.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(pm)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        renderer.render(painter)
        painter.end()
        return QtGui.QIcon(pm)
    except Exception:
        return None
