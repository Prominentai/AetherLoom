"""Consistent media path acceptance and worker-side folder expansion."""
import os
from pathlib import Path
from .model import MEDIA_SUFFIXES


def accepts(path, kind):
    return os.path.isdir(path) or (os.path.isfile(path) and Path(path).suffix.lower() in MEDIA_SUFFIXES[kind])


def resolve_files(paths, kind, stop=None):
    files, seen = [], set()
    def check():
        if stop is not None and stop.is_set():
            from .save_results import SaveCanceled
            raise SaveCanceled('读取素材已取消')
    def add(path):
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key);files.append(str(path))
    for value in paths:
        check()
        path = Path(value).expanduser()
        if path.is_dir():
            matching = []
            with os.scandir(path) as entries:
                for entry in entries:
                    check()
                    if Path(entry.name).suffix.lower() in MEDIA_SUFFIXES[kind] and entry.is_file():matching.append(entry.path)
            for item in sorted(matching, key=lambda p:(Path(p).name.casefold(), Path(p).name)):
                add(item)
        elif path.is_file():
            if path.suffix.lower() in MEDIA_SUFFIXES[kind]:add(path)
        else:
            raise FileNotFoundError('输入路径不存在：' + str(path))
    return files
