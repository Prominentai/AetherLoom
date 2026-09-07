"""Background thumbnail, preview, and media information jobs."""
import os
import tempfile
import cv2
from PIL import Image
from PyQt5 import QtCore
from aetherloom_core.resources import IMAGE_EXTS, VIDEO_EXTS

class ThumbnailSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(str, str, str, str)


class ThumbnailJob(QtCore.QRunnable):
    """Generate a PNG thumbnail (bytes written to cache) off the UI thread.
    Emits finished(path, cache_path, key, list_name) when done.
    """
    def __init__(self, path, size, cache_path, key, list_name, cancel_token=None):
        super().__init__()
        self.path = path
        self.size = (int(size[0]), int(size[1]))
        self.cache_path = cache_path
        self.key = key
        self.list_name = list_name
        # optional cancel token: object with .is_cancelled() or .cancelled attr
        self.cancel_token = cancel_token
        self.signals = ThumbnailSignals()

    def run(self):
        def cancelled():
            check = getattr(self.cancel_token, 'is_cancelled', None)
            return bool(check()) if callable(check) else bool(getattr(self.cancel_token, 'cancelled', False))

        result_path = ''
        if cancelled():
            self.signals.finished.emit(self.path, '', self.key, self.list_name)
            return
        try:
            from io import BytesIO
            target_w, target_h = self.size
            from aetherloom_core.media_limits import load_media_frame
            frame, _original_size, _is_video = load_media_frame(self.path, self.size)
            try:
                frame.thumbnail(self.size, Image.LANCZOS)
                with Image.new('RGB', self.size, (0, 0, 0)) as background:
                    background.paste(frame, ((target_w - frame.width) // 2, (target_h - frame.height) // 2))
                    with BytesIO() as buffer:
                        background.save(buffer, format='PNG')
                        data = buffer.getvalue()
            finally:
                frame.close()
            if cancelled():
                return
            if self.cancel_token is not None:
                self.cancel_token.png_bytes = data
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(dir=os.path.dirname(self.cache_path),
                                                 suffix='.tmp', delete=False) as output:
                    tmp_path = output.name
                    output.write(data)
                os.replace(tmp_path, self.cache_path)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)
            result_path = self.cache_path
        except Exception as exc:
            # Always notify a failed job so it can be retried instead of remaining
            # permanently marked as in flight. Raw bytes can survive a disk error.
            if self.cancel_token is not None:
                self.cancel_token.error = str(exc)[:240]
            if getattr(self.cancel_token, 'png_bytes', None):
                result_path = self.cache_path
        finally:
            self.signals.finished.emit(self.path, result_path, self.key, self.list_name)


class PreviewSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(str, str, bytes)


class PreviewJob(QtCore.QRunnable):
    """Load a preview pixmap (image or first video frame) off the UI thread.
    Emits finished(path, which, png_bytes).
    """
    def __init__(self, path, which, size, show_grid=False, grid_cols=32):
        super().__init__()
        self.path = path
        self.which = which
        self.size = (int(size[0]), int(size[1]))
        self.show_grid = bool(show_grid)
        self.grid_cols = int(grid_cols)
        self.signals = PreviewSignals()

    def run(self):
        try:
            from io import BytesIO
            from PIL import Image as PILImage, ImageDraw
            import cv2 as _cv2
            path = self.path
            # load representative frame
            if path.lower().endswith(VIDEO_EXTS):
                if path.lower().endswith('.gif'):
                    img = PILImage.open(path)
                    frame = img.copy().convert('RGB')
                else:
                    cap = _cv2.VideoCapture(path)
                    ret, fr = cap.read()
                    cap.release()
                    if not ret:
                        frame = PILImage.new('RGB', (self.size[0], self.size[1]), (0, 0, 0))
                    else:
                        frame = PILImage.fromarray(_cv2.cvtColor(fr, _cv2.COLOR_BGR2RGB))
            else:
                frame = PILImage.open(path).convert('RGB')

            # optionally draw grid overlay using provided grid_cols
            if self.show_grid:
                try:
                    w, h = frame.size
                    cols = max(4, int(self.grid_cols))
                    rows = cols + 2
                    d = ImageDraw.Draw(frame)
                    tw = w / cols
                    th = h / rows
                    for i in range(1, cols):
                        x = int(i * tw)
                        d.line([(x, 0), (x, h)], fill=(255, 0, 0), width=1)
                    for j in range(1, rows):
                        y = int(j * th)
                        d.line([(0, y), (w, y)], fill=(255, 0, 0), width=1)
                except Exception:
                    pass

            # scale to requested size preserving aspect ratio to avoid huge memory
            try:
                target_w, target_h = int(self.size[0]), int(self.size[1])
                frame.thumbnail((target_w, target_h), PILImage.LANCZOS)
                # center on black bg of exact size
                bg = PILImage.new('RGB', (target_w, target_h), (0, 0, 0))
                x = (target_w - frame.width) // 2
                y = (target_h - frame.height) // 2
                bg.paste(frame, (x, y))
            except Exception:
                bg = frame

            buf = BytesIO()
            try:
                bg.save(buf, format='PNG')
                data = buf.getvalue()
            except Exception:
                data = b''

            try:
                self.signals.finished.emit(self.path, self.which, data)
            except Exception:
                pass
        except Exception:
            try:
                self.signals.finished.emit(self.path, self.which, b'')
            except Exception:
                pass


class FileInfoSignals(QtCore.QObject):
    finished = QtCore.pyqtSignal(str, str)


class FileInfoJob(QtCore.QRunnable):
    """Background job to compute concise file info string for UI.
    Emits finished(path, info_text).
    """
    def __init__(self, path):
        super().__init__()
        self.path = path
        self.signals = FileInfoSignals()

    def run(self):
        try:
            parts = []
            try:
                name = os.path.basename(self.path)
            except Exception:
                name = self.path
            parts.append(name)
            # size
            try:
                sz = os.path.getsize(self.path)
                if sz >= 1024 * 1024:
                    parts.append(f"{sz / (1024*1024):.2f} MB")
                else:
                    parts.append(f"{sz / 1024:.2f} KB")
            except Exception:
                pass

            ext = os.path.splitext(self.path)[1].lower()
            # image
            if ext in IMAGE_EXTS:
                try:
                    img = Image.open(self.path)
                    w, h = img.size
                    parts.insert(1, f"{w}x{h} (IMAGE)")
                except Exception:
                    pass
            elif ext == '.gif':
                try:
                    img = Image.open(self.path)
                    w, h = img.size
                    frames = int(getattr(img, 'n_frames', 1) or 1)
                    # sum per-frame duration if available
                    total_ms = 0
                    try:
                        if frames > 1:
                            for i in range(frames):
                                try:
                                    img.seek(i)
                                    total_ms += int(img.info.get('duration', 0) or 0)
                                except Exception:
                                    pass
                        else:
                            total_ms = int(img.info.get('duration', 0) or 0)
                    except Exception:
                        try:
                            total_ms = int(img.info.get('duration', 0) or 0)
                        except Exception:
                            total_ms = 0
                    total_s = (total_ms / 1000.0) if total_ms else 0.0
                    fps = (frames / total_s) if total_s > 0 else 0.0
                    extras = []
                    if fps > 0:
                        extras.append(f"{fps:.2f} FPS")
                    if total_s > 0:
                        extras.append(f"{total_s:.2f}s")
                    extras.append(f"{frames} frames")
                    note = (' ' + '/'.join(extras)) if extras else ''
                    parts.insert(1, f"{w}x{h} (GIF){note}")
                except Exception:
                    pass
            elif ext in VIDEO_EXTS:
                try:
                    cap = cv2.VideoCapture(self.path)
                    if cap.isOpened():
                        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
                        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
                        duration = frames / fps if fps > 0 else 0.0
                        extras = []
                        if fps > 0:
                            extras.append(f"{fps:.2f} FPS")
                        if duration > 0:
                            extras.append(f"{duration:.2f}s")
                        note = (' ' + '/'.join(extras)) if extras else ''
                        parts.insert(1, f"{w}x{h} (VIDEO){note}")
                    cap.release()
                except Exception:
                    pass

            info_text = ' | '.join(parts)
            try:
                self.signals.finished.emit(self.path, info_text)
            except Exception:
                pass
        except Exception:
            try:
                self.signals.finished.emit(self.path, '')
            except Exception:
                pass
