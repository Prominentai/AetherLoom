"""Platform integration and optional SVG support."""
import os
import sys
import errno
import weakref
from PyQt5 import QtCore, QtGui
try:
    from PyQt5 import QtSvg
except Exception:
    QtSvg = None

def _api_debug(msg):
    # debug logging disabled; keep stub for compatibility
    return


def _reveal_path_values(paths):
    if isinstance(paths, (str, os.PathLike)):
        paths = (paths,)
    try:
        values = tuple(os.fspath(path) for path in paths)
    except (TypeError, ValueError) as error:
        raise OSError(errno.EINVAL, '请选择有效的文件或文件夹路径') from error
    if not values or any(not isinstance(path, str) or not path or '\0' in path for path in values):
        raise OSError(errno.EINVAL, '请选择有效的文件或文件夹路径')
    return values


def _reveal_actions(paths):
    """Validate all targets before opening anything; keep file selections intact."""
    actions, groups, seen = [], {}, set()
    for value in _reveal_path_values(paths):
        path = os.path.abspath(os.path.expanduser(value))
        identity = os.path.normcase(path)
        if identity in seen:
            continue
        seen.add(identity)
        if not os.path.exists(path):
            raise FileNotFoundError(errno.ENOENT, '文件或文件夹不存在，可能已移动或删除', path)
        if os.path.isdir(path):
            actions.append(('directory', path, ()))
        elif os.path.isfile(path):
            parent = os.path.dirname(path)
            key = os.path.normcase(parent)
            if key not in groups:
                groups[key] = []
                actions.append(('files', parent, groups[key]))
            groups[key].append(path)
        else:
            raise OSError(errno.EINVAL, '此路径不是普通文件或文件夹', path)
    return actions


def _reveal_windows(actions):
    import ctypes
    if not any(kind == 'files' for kind, _, _ in actions):
        for _, folder, _ in actions:
            os.startfile(folder)
        return
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    ole = ctypes.WinDLL('ole32', use_last_error=True)
    pointer, hresult, uint32 = ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint32
    ole.CoInitializeEx.argtypes = [pointer, uint32]
    ole.CoInitializeEx.restype = hresult
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoTaskMemFree.argtypes = [pointer]
    ole.CoTaskMemFree.restype = None
    shell.SHParseDisplayName.argtypes = [ctypes.c_wchar_p, pointer, ctypes.POINTER(pointer), uint32, ctypes.POINTER(uint32)]
    shell.SHParseDisplayName.restype = hresult
    shell.ILFindLastID.argtypes = [pointer]
    shell.ILFindLastID.restype = pointer
    shell.SHOpenFolderAndSelectItems.argtypes = [pointer, uint32, ctypes.POINTER(pointer), uint32]
    shell.SHOpenFolderAndSelectItems.restype = hresult

    def check(result, action, target=''):
        if ctypes.c_int32(result).value < 0:
            raise OSError(f'{action}失败（HRESULT 0x{result & 0xffffffff:08X}）' + (f'：{target}' if target else ''))

    # A Qt pool thread may already use a different COM apartment. In that case
    # COM is available, but this call owns no reference to uninitialize.
    status = ole.CoInitializeEx(None, 0x2 | 0x4)
    initialized = status in (0, 1)
    if (status & 0xffffffff) != 0x80010106:  # RPC_E_CHANGED_MODE
        check(status, '初始化 Windows 文件管理器')
    try:
        for kind, folder, files in actions:
            if kind == 'directory':
                os.startfile(folder)
                continue
            allocated = []
            def parse(path):
                pidl = pointer()
                result = shell.SHParseDisplayName(path, None, ctypes.byref(pidl), 0, None)
                if pidl.value:
                    allocated.append(pidl.value)
                check(result, '解析文件位置', path)
                if not pidl.value:
                    raise OSError('Windows 未返回有效的文件位置：' + path)
                return pidl.value
            try:
                if len(files) == 1:
                    # cidl=0 means the absolute PIDL identifies the item itself.
                    result = shell.SHOpenFolderAndSelectItems(parse(files[0]), 0, None, 0)
                else:
                    parent_pidl = parse(folder)
                    children = []
                    for path in files:
                        child = shell.ILFindLastID(parse(path))
                        if not child:
                            raise OSError('Windows 未返回有效的文件项目：' + path)
                        children.append(child)
                    # Child PIDLs point inside the full allocated PIDLs. Keep
                    # those allocations alive through this call, free only roots.
                    array = (pointer * len(children))(*children)
                    result = shell.SHOpenFolderAndSelectItems(parent_pidl, len(children), array, 0)
                check(result, '打开并选中文件', folder)
            finally:
                for pidl in reversed(allocated):
                    ole.CoTaskMemFree(pidl)
    finally:
        if initialized:
            ole.CoUninitialize()


def reveal_in_file_manager(paths):
    """Reveal files, grouping Windows selections by folder; open directories.

    Accept a path or an iterable of paths. Missing targets and native Shell
    failures raise OSError instead of silently opening an unrelated folder.
    Shell parsing may block on network locations; GUI callers should use the
    asynchronous wrapper below.
    """
    actions = _reveal_actions(paths)
    if sys.platform == 'win32':
        _reveal_windows(actions)
        return
    import subprocess
    for kind, folder, files in actions:
        command = (['open', '-R', *files] if kind == 'files' else ['open', folder]) if sys.platform == 'darwin' else ['xdg-open', folder]
        try:
            subprocess.run(command, check=True, timeout=15)
        except (subprocess.SubprocessError, OSError) as error:
            raise OSError('无法在文件管理器中打开：' + folder + '；' + str(error)) from error


class _RevealRequest(QtCore.QObject):
    finished = QtCore.pyqtSignal(object)

    def __init__(self, parent, on_error):
        # The application keeps this signal source alive while native Shell
        # parsing runs, even when the initiating window has been destroyed.
        super().__init__(QtCore.QCoreApplication.instance())
        self._owner = weakref.ref(parent) if parent is not None else None
        self._on_error = on_error
        self._canceled = False
        self.finished.connect(self._complete, QtCore.Qt.QueuedConnection)
        if parent is not None:
            parent.destroyed.connect(self.cancel)

    @QtCore.pyqtSlot()
    def cancel(self):
        """Stop GUI delivery; an in-flight Windows Shell call cannot be aborted."""
        self._canceled = True
        self._on_error = None

    @QtCore.pyqtSlot(object)
    def _complete(self, error):
        from PyQt5 import sip
        try:
            owner = self._owner() if self._owner is not None else None
            if self._canceled or self._owner is not None and (owner is None or sip.isdeleted(owner)):
                return
            if error is not None and self._on_error is not None:
                self._on_error(error)
        finally:
            self._on_error = None
            self.deleteLater()


class _RevealJob(QtCore.QRunnable):
    def __init__(self, paths, request, error=None):
        super().__init__()
        self.paths, self.request, self.error = paths, request, error

    def run(self):
        error = self.error
        if error is None:
            try:
                reveal_in_file_manager(self.paths)
            except Exception as exception:
                error = exception
        try:
            self.request.finished.emit(error)
        except RuntimeError:
            pass  # Application teardown already deleted the signal source.


def reveal_in_file_manager_async(paths, parent, on_error):
    """Run filesystem/Shell work in Qt's pool and report errors on the GUI thread.

    Call from the GUI thread. The returned request can cancel error delivery;
    destroying parent does so automatically. on_error receives the exception.
    """
    request = _RevealRequest(parent, on_error)
    try:
        captured, error = _reveal_path_values(paths), None
    except OSError as exception:
        captured, error = (), exception
    QtCore.QThreadPool.globalInstance().start(_RevealJob(captured, request, error))
    return request


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
