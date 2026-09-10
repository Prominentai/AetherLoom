"""Read-only website model import, kept separate from paid/public API execution."""
import re
from urllib.parse import urlsplit

from .rh_model_cards import model_versions
from .rh_model_favorites import TYPES, favorite_data


def model_link(value):
    parts=urlsplit(str(value).strip())
    host=(parts.hostname or '').lower()
    if parts.scheme not in ('http','https') or parts.username or parts.password or parts.port not in (None,80,443):
        raise ValueError('请输入 RunningHub 模型网页地址')
    if host not in ('runninghub.cn','www.runninghub.cn','runninghub.ai','www.runninghub.ai'):
        raise ValueError('仅支持 RunningHub 中文站或国际站的模型网页')
    match=re.fullmatch(r'/(?:[a-z]{2}(?:-[a-z]{2})?/)?model/(public|self)/(\d{1,30})/?',parts.path,re.I)
    if not match:raise ValueError('请复制模型详情页地址，例如 /model/public/模型编号')
    return 'https://www.'+host.removeprefix('www.'),match[2],match[1].lower()


def read_model_link(value, authorization=None):
    import requests
    site,identity,visibility=model_link(value)
    if visibility=='self':
        if not authorization:raise ValueError('私有模型需要先登录官网')
        from .rh_model_http import post_json, website_user
        with requests.Session() as session:
            session.headers.update({'Authorization':authorization,'Content-Type':'application/json'})
            try:
                website_user(session,site)
                record=post_json(session,site,'/api/resource/detail/get',{'resourceId':identity})
                if not isinstance(record,dict) or str(record.get('id'))!=identity or record.get('resourceType') not in TYPES:
                    raise ValueError('无法读取该私有模型')
                if not any(v.get('node_token') for v in model_versions(record)):raise ValueError('模型没有可用版本')
                return site,dict(record,_visibility='self')
            finally:session.headers.clear()
    try:
        with requests.post(site+'/api/portal/model/detail',json={'resourceId':identity},
                headers={'Content-Type':'application/json'},timeout=(5,20),stream=True,allow_redirects=False) as response:
            response.raise_for_status()
            data=bytearray()
            for chunk in response.iter_content(32768):
                data.extend(chunk)
                if len(data)>2*1024*1024:raise ValueError('模型详情过大，无法导入')
        import json
        try:payload=json.loads(data)
        except (ValueError,UnicodeError):raise ValueError('官网返回了无法识别的模型信息，请稍后重试') from None
        if not isinstance(payload,dict):raise ValueError('官网返回了无法识别的模型信息，请稍后重试')
        record=payload.get('data')
        if str(payload.get('code'))!='0' or not isinstance(record,dict):
            raise ValueError('无法读取模型，请确认模型公开且地址有效')
        if str(record.get('id'))!=identity or record.get('resourceType') not in TYPES or not any(v.get('node_token') for v in model_versions(record)):
            raise ValueError('该页面没有可导入的模型版本')
        return site,dict(record,_visibility='public')
    except requests.RequestException:
        raise ValueError('模型网页读取失败，请检查网络后重试') from None


def import_values(site, record, all_versions=False):
    if not isinstance(record,dict) or record.get('resourceType') not in TYPES:return []
    versions=[v for v in model_versions(record) if v.get('node_token')]
    return [dict(favorite_data(site,record,v),source='website') for v in (versions[:50] if all_versions else versions[:1])]
