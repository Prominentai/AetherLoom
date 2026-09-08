"""Shared, asynchronous installation of referenced RunningHub applications."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit

from PyQt5 import QtCore

from aetherloom_core.paths import current_dir


def application_reference(reference):
    """Only send local credentials to a known RunningHub website."""
    raw = str(reference.get('url') or '').strip()
    wid = str(reference.get('webapp_id') or reference.get('webappId') or '').strip()
    if raw:
        parsed = urlsplit(raw if '://' in raw else 'https://' + raw)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or host not in ('runninghub.cn', 'www.runninghub.cn',
                                'runninghub.ai', 'www.runninghub.ai')):
            raise ValueError('应用地址必须是 RunningHub 官方网站的 HTTPS 地址')
        match = re.fullmatch(r'/(?:webapp|ai-detail)/(\d+)/?', parsed.path)
        if not match or (wid and wid != match.group(1)):
            raise ValueError('应用地址与应用 ID 不一致或地址格式无效')
        wid = match.group(1)
        host = host if host.startswith('www.') else 'www.' + host
    else:
        host = 'www.runninghub.cn'
    if not wid.isdigit():
        raise ValueError('应用缺少有效的 ID')
    base = 'https://' + host
    return dict(webapp_id=wid, url=base + '/webapp/' + wid, base_url=base,
                name=str(reference.get('name') or reference.get('title') or ''))


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class AppInstallJob(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, int, dict, str)
    finished = QtCore.pyqtSignal(dict)

    def __init__(self, references, keyrings, root, lock, parent=None):
        super().__init__(parent)
        self.references = list(references)
        self.keyrings = {host: list(keys) for host, keys in keyrings.items()}
        self.root, self.lock = Path(root), lock

    def start(self):
        threading.Thread(target=self._run, name='rh-install-apps', daemon=True).start()

    def _run(self):
        from api_calls import call_rh
        from get_apps import scrape_runninghub_detail
        report = dict(added=[], existing=[], failed=[], total=0)
        seen, references = set(), []
        for raw in self.references:
            try:
                ref = application_reference(raw)
            except (ValueError, TypeError) as exc:
                report['failed'].append(dict(webapp_id=str(raw.get('webapp_id') or ''),
                                             url=str(raw.get('url') or ''), error=str(exc)))
                continue
            # RH_apps uses a single directory per webapp ID, as does the App page.
            if ref['webapp_id'] not in seen:
                seen.add(ref['webapp_id'])
                references.append(ref)
        report['total'] = len(references) + len(report['failed'])
        for index, ref in enumerate(references, 1):
            error = ''
            try:
                with self.lock:
                    wid, base = ref['webapp_id'], ref['base_url']
                    path = self.root / wid / (wid + '.json')
                    if path.is_file():
                        with path.open(encoding='utf-8') as stream:
                            installed = json.load(stream)
                        if not isinstance(installed, dict) or not isinstance(installed.get('nodeInfoList'), list):
                            raise ValueError('本地应用定义损坏，请在 RH 应用页重新添加')
                        report['existing'].append(wid)
                    else:
                        keys = self.keyrings.get(base, [])
                        if not keys:
                            raise ValueError('请在 RH 连接设置中配置 ' + base + ' 的 API key')
                        nodes, last_error = None, None
                        for key in keys:
                            try:
                                response = call_rh.get_nodeinfo(wid, key, base_url=base, timeout=25)
                                data = json.loads(response) if isinstance(response, (bytes, bytearray, str)) else response
                                candidate = data if isinstance(data, list) else data.get('data', {}).get('nodeInfoList')
                                if not isinstance(candidate, list) or not all(isinstance(node, dict) for node in candidate):
                                    raise ValueError('应用未返回有效的参数定义')
                                nodes = candidate
                                break
                            except Exception as exc:
                                last_error = exc
                        if nodes is None:
                            raise last_error or ValueError('无法读取应用参数')
                        detail = {}
                        try:
                            detail = scrape_runninghub_detail(ref['url'], timeout=10, api_base=base)
                        except Exception:
                            pass  # Names and thumbnails are optional; parameter definitions are not.
                        covers = detail.get('covers') or []
                        thumbnail = (covers[0].get('thumbnailUri') or covers[0].get('url') or '') if covers and isinstance(covers[0], dict) else ''
                        _atomic_write(path, dict(webappId=wid, title=detail.get('name') or ref['name'] or wid,
                                                 description=detail.get('description') or '', thumbnail_uri=thumbnail,
                                                 url=ref['url'], base_url=base, nodeInfoList=nodes))
                        report['added'].append(wid)
            except Exception as exc:
                error = str(exc)
                for keys in self.keyrings.values():
                    for key in keys:
                        if key:
                            error = error.replace(key, '[API key]')
                report['failed'].append(dict(webapp_id=ref['webapp_id'], url=ref['url'], error=error))
            self.progress.emit(index, len(references), ref, error)
        self.finished.emit(report)
        self.keyrings.clear()


def install_apps(owner, references, on_progress=None, on_finished=None):
    """Capture credentials on the GUI thread; all result callbacks use that thread."""
    from aetherloom_core.rh_connections import ensure_connections
    settings = ensure_connections(owner)
    if not hasattr(owner, '_rh_app_install_lock'):
        owner._rh_app_install_lock = threading.Lock()
    job = AppInstallJob(references, settings.snapshot()['site_keyrings'], Path(current_dir) / 'RH_apps',
                        owner._rh_app_install_lock, owner)
    if on_progress is not None:
        job.progress.connect(on_progress)

    def finished(report):
        if report['added'] and not getattr(owner, '_closing', False):
            reload_apps = getattr(owner, '_rh_reload_apps', None)
            if reload_apps is not None:
                reload_apps()
        if not getattr(owner, '_closing', False) and on_finished is not None:
            on_finished(report)
        job.deleteLater()

    job.finished.connect(finished)
    job.start()
    return job
