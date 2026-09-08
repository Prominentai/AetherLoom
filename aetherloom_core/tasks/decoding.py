"""Cancellable decoding thread supervising spawned worker processes."""
import multiprocessing
import os
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
        self.files = files
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.keep_audio = keep_audio
        self.decode_mode = decode_mode
        self.overwrite = overwrite
        self.password = password or ''
        self.grid_cols = int(grc.grid_cols if grid_cols is None else grid_cols)
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        total = len(self.files)
        processed = 0
        # normalize unexpected modes back to grc
        mode_for_decode = self.decode_mode if self.decode_mode in ('grc', 'sst') else 'grc'
        for f in self.files:
            if self._is_cancelled:
                self.log.emit('已取消批处理')
                break
            try:
                src = os.path.join(self.input_dir, f)
                name, ext = os.path.splitext(f)
                out_name = f"{name}_restored{ext}"
                dst = os.path.join(self.output_dir, out_name)
                # skip if output exists and overwrite not requested
                if os.path.exists(dst) and not self.overwrite:
                    processed += 1
                    pct = int(processed / total * 100)
                    self.progress.emit(pct)
                    self.log.emit(f"跳过已存在: {out_name}")
                    continue
                is_image = ext.lower() in IMAGE_EXTS

                # run the processing in a separate process so we can cancel mid-file
                try:
                    ctx = multiprocessing.get_context('spawn')
                except Exception:
                    ctx = multiprocessing
                q = ctx.Queue()
                cols = self.grid_cols
                rows = cols + 2
                p = ctx.Process(target=_file_process_worker, args=(q, src, dst, is_image, self.keep_audio, mode_for_decode, cols, rows, self.password))
                p.start()
                self.log.emit(f'子进程已启动 PID={getattr(p, "pid", "?")} 处理文件 {f}')
                # wait and monitor cancellation
                while p.is_alive():
                    if self._is_cancelled:
                        try:
                            p.terminate()
                        except Exception:
                            pass
                        p.join(1)
                        self.log.emit(f'已取消: {f}')
                        break
                    QtCore.QThread.msleep(200)
                # ensure process joined
                try:
                    p.join(0.1)
                except Exception:
                    pass

                result = None
                msg = ''
                try:
                    if not q.empty():
                        result, msg = q.get_nowait()
                    else:
                        result = 'UNKNOWN'
                except Exception:
                    result = 'ERROR'

                ok = (result == 'OK')
                if ok and msg:
                    try:
                        dst = msg  # use actual output path returned by worker
                        out_name = os.path.basename(dst)
                    except Exception:
                        pass
                processed += 1
                pct = int(processed / total * 100)
                self.progress.emit(pct)
                self.log.emit(f"处理 {f} -> {out_name} {'OK' if ok else 'FAIL'}")
            except Exception as e:
                self.log.emit(f"错误处理 {f}: {e}")
                self.log.emit(traceback.format_exc())
        self.finished.emit()
