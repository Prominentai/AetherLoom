"""Cancellable decoding thread supervising spawned worker processes."""
import multiprocessing
import os
import tempfile
import threading
import traceback
from PyQt5 import QtCore
from aetherloom_core.resources import IMAGE_EXTS
from aetherloom_core.services.decoding import grc, _file_process_worker

class Worker(QtCore.QThread):
    progress = QtCore.pyqtSignal(int)
    log = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal()

    def __init__(self, files, input_dir, output_dir, keep_audio=True, decode_mode='grc', overwrite=False, password='', parent=None, grid_cols=None):
        super().__init__(parent)
        self.files = list(files)
        self.input_dir = os.path.abspath(os.fspath(input_dir))
        self.output_dir = os.path.abspath(os.fspath(output_dir))
        self.results = {}
        self.keep_audio = keep_audio
        self.decode_mode = decode_mode
        self.overwrite = overwrite
        self.password = password or ''
        self.grid_cols = int(grc.grid_cols if grid_cols is None else grid_cols)
        self._is_cancelled = False
        self._publication_lock = threading.Lock()

    def cancel(self):
        # A published file commits before cancellation, or cancellation prevents
        # publication. The lock never covers decoding or waiting on the child.
        with self._publication_lock:
            self._is_cancelled = True

    def run(self):
        try:
            os.makedirs(self.output_dir, exist_ok=True)
        except OSError as exc:
            self.log.emit(f'无法创建解码结果目录: {exc}')
            self.finished.emit()
            return
        total = len(self.files)
        processed = 0
        mode_for_decode = self.decode_mode if self.decode_mode in ('grc', 'sst') else 'grc'
        for f in self.files:
            if self._is_cancelled:
                self.log.emit('已取消批处理')
                break
            try:
                src = os.path.join(self.input_dir, f)
                name, ext = os.path.splitext(os.path.basename(f))
                out_name = f"{name}_restored{ext}"
                dst = os.path.join(self.output_dir, out_name)
                # SST's payload determines the final extension. An input PNG
                # may contain a video/text file, so its guessed PNG destination
                # cannot decide either output reuse or overwrite permission.
                if mode_for_decode != 'sst' and os.path.exists(dst) and not self.overwrite:
                    if os.path.isfile(dst):
                        self.results[f] = dst
                    processed += 1
                    self.progress.emit(int(processed / total * 100))
                    self.log.emit(f"跳过已存在: {out_name}")
                    continue
                is_image = ext.lower() in IMAGE_EXTS
                # Decode away from existing results. Failed/cancelled workers
                # must never truncate them, even when overwrite was requested.
                with tempfile.TemporaryDirectory(prefix='.decode-', dir=self.output_dir) as staging:
                    temporary = os.path.join(staging, out_name)
                    try:
                        ctx = multiprocessing.get_context('spawn')
                    except Exception:
                        ctx = multiprocessing
                    q = ctx.Queue()
                    p = ctx.Process(target=_file_process_worker,
                        args=(q, src, temporary, is_image, self.keep_audio,
                              mode_for_decode, self.grid_cols, self.grid_cols + 2, self.password))
                    ok = False
                    try:
                        p.start()
                        self.log.emit(f'子进程已启动 PID={getattr(p, "pid", "?")} 处理文件 {f}')
                        while p.is_alive():
                            if self._is_cancelled:
                                p.terminate()
                                p.join(1)
                                if p.is_alive():
                                    p.kill()
                                    p.join(1)
                                self.log.emit(f'已取消: {f}')
                                break
                            QtCore.QThread.msleep(200)
                        p.join(0.1)
                        if not self._is_cancelled:
                            try:
                                result, msg = q.get(timeout=0.5)
                            except Exception:
                                result, msg = 'UNKNOWN', ''
                            if result == 'OK' and msg and not self._is_cancelled:
                                actual = os.path.abspath(os.fspath(msg))
                                if (os.path.realpath(os.path.dirname(actual)) != os.path.realpath(staging)
                                        or not os.path.isfile(actual)
                                        or (mode_for_decode != 'sst' and os.path.getsize(actual) <= 0)):
                                    raise ValueError('Decoder returned an invalid output file')
                                out_name = os.path.basename(actual)
                                dst = os.path.join(self.output_dir, out_name)
                                with self._publication_lock:
                                    if not self._is_cancelled:
                                        if self.overwrite:
                                            os.replace(actual, dst)
                                        else:
                                            try:
                                                if os.name == 'nt':
                                                    # Windows rename is atomic and refuses an existing target.
                                                    os.rename(actual, dst)
                                                else:
                                                    # POSIX rename replaces files; a same-directory hard link
                                                    # provides an atomic no-replace publication instead.
                                                    os.link(actual, dst)
                                            except FileExistsError:
                                                if not os.path.isfile(dst):
                                                    raise
                                                self.log.emit(f"跳过已存在: {out_name}")
                                        self.results[f] = dst
                                        ok = True
                    finally:
                        if p.pid is not None:
                            if p.is_alive():
                                p.terminate()
                                p.join(1)
                            if not p.is_alive():
                                p.close()
                        q.close()
                        q.join_thread()
                processed += 1
                self.progress.emit(int(processed / total * 100))
                self.log.emit(f"处理 {f} -> {out_name} {'OK' if ok else 'FAIL'}")
            except Exception as e:
                self.log.emit(f"错误处理 {f}: {e}")
                self.log.emit(traceback.format_exc())
        self.finished.emit()
