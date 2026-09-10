"""Local version-specific model favorites; no login state or API keys stored."""
import copy
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from PyQt5 import QtCore, QtWidgets

from .rh_model_cards import cover_url, model_title

TYPES = ('CHECKPOINT', 'LORA', 'UNET', 'GGUF')
SITES = ('https://www.runninghub.cn', 'https://www.runninghub.ai')


def favorite_data(site, record, version):
    """Project only useful catalog fields; never serialize an arbitrary response."""
    return dict(site=site, resource_type=record.get('resourceType'),
        model_name=version.get('node_token') or record.get('nodeModelName'),
        title=model_title(record), version=str(version.get('version') or '默认'),
        base_model=str(version.get('baseModel') or ''), trigger_words=str(version.get('triggerWords') or ''),
        notes='', description=str(version.get('desc') or record.get('desc') or '')[:8000],
        thumbnail=cover_url(record, version),
        tags=[dict(id=int(t['id']), name=str(t.get('name') or '')[:100])
              for t in (record.get('tags') or [])[:48] if isinstance(t,dict) and str(t.get('id','')).isdigit()],
        remote_id=str(record.get('id') or ''), visibility=record.get('_visibility') or ('self' if record.get('systemResource') is False else 'public'), custom=False, pinned=False)


class ModelFavorites(QtCore.QObject):
    changed = QtCore.pyqtSignal()

    def __init__(self, owner, path=None, bucket='favorites'):
        super().__init__(owner)
        if bucket not in ('favorites','uploads'):raise ValueError('无效的模型分组')
        self.bucket=bucket
        if path is None:
            from .paths import current_dir
            path = Path(current_dir) / 'model_library' / (bucket+'.sqlite3')
        self.path = Path(path)
        self.connection = None
        app = QtWidgets.QApplication.instance()
        if app is not None:app.aboutToQuit.connect(self.close)

    def _db(self, write=False):
        if self.connection is not None:return self.connection
        if not write and not self.path.exists():return None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=2)
        try:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):raise ValueError('收藏文件版本较新，请使用新版客户端打开')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS favorites (
                id TEXT PRIMARY KEY, site TEXT NOT NULL, resource_type TEXT NOT NULL,
                model_name TEXT NOT NULL, title TEXT NOT NULL, base_model TEXT NOT NULL,
                tag_ids TEXT NOT NULL, pinned INTEGER NOT NULL, custom INTEGER NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, metadata TEXT NOT NULL,
                UNIQUE(site, resource_type, model_name))''')
            db.execute('CREATE INDEX IF NOT EXISTS favorites_browse ON favorites(site, resource_type, pinned, updated)')
            db.execute('PRAGMA user_version=1');db.commit()
        except Exception:
            db.close();raise
        db.row_factory = sqlite3.Row
        self.connection = db
        return db

    def close(self):
        if self.connection is not None:
            self.connection.close();self.connection=None

    def _value(self, row):
        if row is None:return None
        data = json.loads(row['metadata'])
        if not isinstance(data,dict):raise ValueError('收藏记录格式损坏')
        data.update(id=row['id'], pinned=bool(row['pinned']), custom=bool(row['custom']),bucket=self.bucket)
        return data

    def lookup(self, site, resource_type, model_name):
        db = self._db()
        if db is None:return None
        return self._value(db.execute('SELECT * FROM favorites WHERE site=? AND resource_type=? AND model_name=?',
            (site, resource_type, model_name)).fetchone())

    def get(self, identity):
        db = self._db()
        return self._value(db.execute('SELECT * FROM favorites WHERE id=?',(identity,)).fetchone()) if db else None

    def cover_info(self, value):
        name = value.get('cover_file') or ''
        if not re.fullmatch(r'[0-9a-f]{32}\.png',name):return None
        path = self.path.parent / 'covers' / name
        try:
            if path.resolve().parent != (self.path.parent/'covers').resolve():return None
            stat = path.stat()
            if stat.st_size > 3*1024*1024:return None
            return ('local-cover:'+str(path)+':'+str(stat.st_mtime_ns),path)
        except OSError:return None

    def save(self, value, identity=None, cover_bytes=None):
        data = copy.deepcopy(value)
        if data.get('site') not in SITES or data.get('resource_type') not in TYPES:
            raise ValueError('请选择有效的站点和模型类型')
        token = str(data.get('model_name') or '').strip()
        if not token or len(token)>2048 or any(c in token for c in '\r\n\x00'):
            raise ValueError('请填写有效的 model name，不要填写换行文本')
        # Manual names are preserved exactly, including valid relative subpaths.
        clean = {name:str(data.get(name) or '')[:limit] for name,limit in
                 [('title',300),('version',200),('base_model',200),('trigger_words',4000),
                  ('notes',8000),('description',8000),('thumbnail',2048),('remote_id',100),('visibility',6)]}
        clean.update(site=data['site'], resource_type=data['resource_type'], model_name=token)
        clean['bucket']=self.bucket
        clean['title'] = clean['title'].strip() or token
        clean['version'] = clean['version'].strip() or '自定义'
        clean['tags'] = [dict(id=int(t['id']), name=str(t.get('name') or '')[:100])
                         for t in (data.get('tags') or [])[:48] if isinstance(t,dict) and str(t.get('id','')).isdigit()]
        pinned, custom = bool(data.get('pinned')), bool(data.get('custom', True))
        source=data.get('source') or ('custom' if custom else 'catalog')
        clean['source']=source if source in ('custom','catalog','website') else 'custom'
        db = self._db(write=True)
        existing = self.lookup(clean['site'], clean['resource_type'], token)
        if identity and existing and existing['id']!=identity:
            raise ValueError('该站点已有相同 model name 的收藏')
        if identity and self.get(identity) is None:raise ValueError('这条收藏已被移除，请重新创建')
        identity = identity or (existing['id'] if existing else uuid.uuid4().hex)
        old = self.get(identity)
        clean['cover_file'] = (old or {}).get('cover_file') or ''
        cover_path, previous = None, None
        if cover_bytes is not None:
            if not re.fullmatch(r'[0-9a-f]{32}',identity):raise ValueError('收藏标识无效')
            if not isinstance(cover_bytes,bytes) or len(cover_bytes)>3*1024*1024 or not cover_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError('封面图片无效')
            cover_path=self.path.parent/'covers'/(identity+'.png')
            cover_path.parent.mkdir(parents=True,exist_ok=True)
            previous=cover_path.read_bytes() if cover_path.exists() else None
            clean['cover_file']=cover_path.name;clean['thumbnail']=''
        now = time.time()
        tag_ids = ',' + ','.join(str(t['id']) for t in clean['tags']) + ','
        try:
            with db:
                db.execute('''INSERT INTO favorites VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET site=excluded.site,resource_type=excluded.resource_type,
                model_name=excluded.model_name,title=excluded.title,base_model=excluded.base_model,
                tag_ids=excluded.tag_ids,pinned=excluded.pinned,custom=excluded.custom,
                updated=excluded.updated,metadata=excluded.metadata''',
                (identity,clean['site'],clean['resource_type'],token,clean['title'],clean['base_model'],tag_ids,
                 int(pinned),int(custom),now,now,json.dumps(clean,ensure_ascii=False)))
                if cover_path:self._write_cover(cover_path,cover_bytes)
        except Exception:
            if cover_path:
                if previous is not None:self._write_cover(cover_path,previous)
                else:cover_path.unlink(missing_ok=True)
            raise
        self.changed.emit()
        return identity

    @staticmethod
    def _write_cover(path, data):
        temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            with temporary.open('wb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
        finally:temporary.unlink(missing_ok=True)

    def remove(self, identity):
        value=self.get(identity) or {};cover=self.cover_info(value)
        db = self._db()
        if db is not None:
            with db:db.execute('DELETE FROM favorites WHERE id=?',(identity,))
            if cover:
                try:cover[1].unlink(missing_ok=True)
                except OSError:pass
            self.changed.emit()

    def set_pinned(self, identity, pinned):
        db = self._db()
        if db is not None:
            with db:db.execute('UPDATE favorites SET pinned=? WHERE id=?',(int(bool(pinned)),identity))
            self.changed.emit()

    def page(self, site, resource_type, search='', base_models=(), tag=None, current=1, size=30):
        db = self._db()
        if db is None:return dict(records=[],current=current,total=0,hasNext=False)
        clauses=['site=?'];args=[site]
        if resource_type:
            clauses.append('resource_type=?');args.append(resource_type)
        if search:
            term = str(search).replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            clauses.append("(title || ' ' || model_name || ' ' || base_model) LIKE ? ESCAPE '\\'")
            args.append('%'+term+'%')
        if base_models:
            clauses.append('base_model IN ('+','.join('?' for _ in base_models)+')');args.extend(base_models)
        if tag is not None:clauses.append('tag_ids LIKE ?');args.append('%,'+str(int(tag))+',%')
        where=' AND '.join(clauses)
        total=db.execute('SELECT COUNT(*) FROM favorites WHERE '+where,args).fetchone()[0]
        rows=db.execute('SELECT * FROM favorites WHERE '+where+' ORDER BY pinned DESC, updated DESC, id LIMIT ? OFFSET ?',
                        args+[size,(max(1,current)-1)*size]).fetchall()
        return dict(records=[self.as_record(self._value(row)) for row in rows],current=current,total=total,hasNext=current*size<total)

    @staticmethod
    def as_record(value):
        return dict(id='favorite:'+value['id'], resourceName=value['title'],resourceType=value['resource_type'],
            nodeModelName=value['model_name'],thumbnailUrl=value.get('thumbnail') or '',desc=value.get('description') or '',
            tags=value.get('tags') or [],_local_favorite=copy.deepcopy(value),
            versions=[dict(version=value.get('version') or '自定义',baseModel=value.get('base_model') or '',
                           triggerWords=value.get('trigger_words') or '')])


def favorites(owner):
    value = getattr(owner, '_rh_model_favorites', None)
    if value is None:value=owner._rh_model_favorites=ModelFavorites(owner)
    return value


def uploads(owner):
    value=getattr(owner,'_rh_model_uploads',None)
    if value is None:value=owner._rh_model_uploads=ModelFavorites(owner,bucket='uploads')
    return value
