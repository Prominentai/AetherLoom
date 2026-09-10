"""Leased run directories, retaining one latest run per saved canvas."""
import atexit
import hashlib
import os
from pathlib import Path
import shutil
import stat
import threading
import json
import re
from PyQt5 import QtCore


class RunFiles:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.root = self.project / '.canvas_cache'
        if self.root.is_symlink() or (self.root.exists() and os.name=='nt' and self.root.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise OSError('临时目录不能使用符号链接或目录联接')
        self.lock = threading.RLock()
        self.closed = False
        self.leases, self.pending, self.cleaning = {}, set(), set()
        self.retired = set()
        self.latest = {}
        self.owners = {}
        try:
            data = json.loads((self.root / 'latest.json').read_text(encoding='utf-8'))
            self.latest = {k:v for k,v in data.items() if re.fullmatch(r'[A-Za-z0-9_-]+', k)
                           and isinstance(v,str) and re.fullmatch(r'[0-9a-f]{24}',v)
                           and (self.project/'canvases'/(k+'.json')).is_file()}
        except (OSError,ValueError,AttributeError,TypeError):pass
        if self.root.exists():
            self.pending.update(p.name for p in self.root.iterdir() if p.is_dir() and p.name not in self.latest.values())
        self.retry()
        atexit.register(lambda:self.close(force=True))

    def _index(self):
        self.root.mkdir(parents=True,exist_ok=True)
        part=self.root/'latest.part'
        with part.open('w',encoding='utf-8') as stream:
            json.dump(self.latest,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(part,self.root/'latest.json')

    def begin(self, canvas_id, run_id):
        with self.lock:
            key=hashlib.sha256(str(run_id).encode()).hexdigest()[:24]
            self.directory(run_id)
            previous=self.latest.get(canvas_id)
            self.owners[key]=(canvas_id,previous)
            self.latest[canvas_id]=key
            try:self._index()
            except OSError:
                if previous:self.latest[canvas_id]=previous
                else:self.latest.pop(canvas_id,None)
                self.owners.pop(key,None)
                raise

    def _discard_locked(self, canvas_id):
        canvas_id = str(canvas_id)
        keys = {self.latest.pop(canvas_id, None)}
        for key, owner in self.owners.items():
            if owner[0] == canvas_id:
                keys.update((key, owner[1]))
        keys.discard(None)
        self.pending.update(keys)
        self.retired.update(keys)
        return keys

    def discard(self, canvas_id):
        with self.lock:
            if self._discard_locked(canvas_id):
                try:self._index()
                except OSError:pass
        self.retry()

    def _remove(self, path):
        path = Path(path)
        if not path.exists() and not path.is_symlink():return
        if path.is_symlink() or (os.name == 'nt' and path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise OSError('临时目录不能使用符号链接或目录联接：' + str(path))
        resolved = path.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ValueError('临时目录超出允许范围')
        if self.project not in resolved.parents:raise ValueError('临时目录超出项目目录')
        shutil.rmtree(resolved)

    def directory(self, run_id, node_id=''):
        with self.lock:
            if self.closed:raise RuntimeError('客户端正在关闭，已停止创建临时文件')
            key = hashlib.sha256(str(run_id).encode()).hexdigest()[:24]
            path = self.root / key
            if key in self.pending or key in self.retired:raise RuntimeError('本次运行已结束')
            if node_id:path /= hashlib.sha256(str(node_id).encode()).hexdigest()[:16]
            path.mkdir(parents=True, exist_ok=True)
            return path

    def retain(self, paths):
        """Pending uploads may still read a canceled workflow's input."""
        keys = set()
        with self.lock:
            if self.closed:raise RuntimeError('客户端正在关闭')
            for value in paths:
                try:
                    relative = Path(value).resolve().relative_to(self.root)
                    if relative.parts:keys.add(relative.parts[0])
                except (ValueError, OSError, TypeError):pass
            if keys & (self.pending | self.cleaning | self.retired):raise RuntimeError('所需临时文件所属运行已结束，请重新执行上游节点')
            for key in keys:self.leases[key] = self.leases.get(key, 0) + 1
        return keys

    def release(self, keys):
        with self.lock:
            for key in keys:
                count = self.leases.get(key, 0) - 1
                if count > 0:self.leases[key] = count
                else:self.leases.pop(key, None)
        self.retry()

    def finish(self, run_id):
        with self.lock:
            key=hashlib.sha256(str(run_id).encode()).hexdigest()[:24]
            owner=self.owners.pop(key,None)
            if owner and owner[1] and owner[1]!=key:
                self.pending.add(owner[1]);self.retired.add(owner[1])
            if key not in self.latest.values():
                self.pending.add(key);self.retired.add(key)
        self.retry()

    def retry(self):
        with self.lock:
            orphaned=[canvas_id for canvas_id in self.latest if not (self.project/'canvases'/(canvas_id+'.json')).is_file()]
            for canvas_id in orphaned:self._discard_locked(canvas_id)
            if orphaned:
                try:self._index()
                except OSError:pass
            keys={key for key in self.pending if not self.leases.get(key) and key not in self.latest.values()} - self.cleaning
            self.cleaning.update(keys)
        # Removing a large completed run must not block new task admission.
        for key in keys:
            removed=False
            try:self._remove(self.root / key);removed=True
            except OSError:pass
            finally:
                with self.lock:
                    self.cleaning.discard(key)
                    if removed:self.pending.discard(key)
        if self.root.exists():
            try:self.root.rmdir()
            except OSError:pass

    def close(self, force=False):
        with self.lock:
            self.closed = True
            if force:self.leases.clear()
            if self.root.exists():self.pending.update(p.name for p in self.root.iterdir() if p.is_dir() and p.name not in self.latest.values())
        self.retry()


_managers = {}
_lock = threading.Lock()


def run_files(project=None):
    if project is None:
        from aetherloom_core.paths import current_dir
        project = current_dir
    key = str(Path(project).resolve())
    with _lock:
        if key not in _managers:_managers[key] = RunFiles(key)
        return _managers[key]


class CanvasCacheCleaner(QtCore.QObject):
    def __init__(self, page):
        super().__init__(page.owner)
        self.page = page
        self.files = run_files(page.store.root.parent)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.files.retry)
        self._timer.start()

    def close(self):
        self._timer.stop()
        self.files.close()
