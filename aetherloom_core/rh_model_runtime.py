"""RH Standard/LLM wire adapters. No implicit retry of ambiguous paid POSTs."""
import base64
import json
import math
import mimetypes
import os
from urllib.parse import urlsplit

import requests

from api_calls import call_rh
from .rh_model_apps import endpoint_path, official_site
from .rh_model_errors import accepted_model_task_id, classify_model_error


def llm_base(base_url):
    host = urlsplit(official_site(base_url)).hostname
    return 'https://llm.runninghub.' + ('ai' if host.endswith('.ai') else 'cn') + '/v1'


def payload(snapshot, nodes):
    definition = snapshot.get('model_definition') or {}
    params = definition.get('params')
    if not isinstance(params, list):raise ValueError('缺少模型参数定义，请重新添加模型应用')
    values = {}
    for field in nodes:
        key = field.get('_model_field') or field.get('fieldName')
        value = field.get('fieldValue', '')
        if field.get('_model_multiple'):
            from .rh_multi_inputs import values as array_values
            value = array_values(value)
        values.setdefault(key, []).extend(value if isinstance(value, list) else [value])
    result = {}
    for param in params:
        key, kind = param['fieldKey'], param['type']
        present = [v for v in values.get(key, []) if v is not None and v != '' and v != 'empty']
        if not present:
            if param.get('required'):raise ValueError('请填写必需参数：' + key)
            continue
        value = present if param.get('multipleInputs') else present[0]
        if param.get('multipleInputs') and len(value) > int(param.get('maxInputNum') or 1):
            raise ValueError(key + ' 的输入数量超过模型上限')
        if kind in ('INT', 'FLOAT'):
            try:
                number = float(value)
                if not math.isfinite(number) or kind == 'INT' and not number.is_integer():raise ValueError()
                value = int(str(value)) if kind == 'INT' else number
            except (ValueError, TypeError, OverflowError):raise ValueError(key + ' 需要有效数值') from None
            if 'min' in param and value < float(param['min']) or 'max' in param and value > float(param['max']):
                raise ValueError(key + ' 超出模型允许范围')
        elif kind == 'BOOLEAN':
            if str(value).lower() not in ('true', 'false', '1', '0'):raise ValueError(key + ' 需要布尔值')
            value = str(value).lower() in ('true', '1')
        elif kind == 'LIST':
            allowed = [str(v.get('value') if isinstance(v, dict) else v) for v in param.get('options', [])]
            if allowed and str(value) not in allowed:raise ValueError(key + ' 不是可用选项')
        if kind in ('STRING', 'SIZE') and param.get('maxLength') and len(str(value)) > int(param['maxLength']):
            raise ValueError(key + ' 超过最大文本长度')
        result[key] = value
    return result


def validate_inputs(snapshot, nodes):
    payload(snapshot, nodes)
    params = {p['fieldKey']: p for p in snapshot['model_definition']['params']}
    for field in nodes:
        param = params.get(field.get('_model_field') or field.get('fieldName'), {})
        if param.get('type') not in ('IMAGE', 'VIDEO', 'AUDIO'):continue
        value = field.get('fieldValue')
        from .rh_multi_inputs import values, validate_file
        for path in values(value):
            if not path:continue
            validate_file(path,param)


def request_description(snapshot, nodes):
    body = payload(snapshot, nodes)
    if snapshot['backend'] == 'rh_standard':
        endpoint = official_site(snapshot['base_url']) + endpoint_path(snapshot['model_definition']['endpoint'])
    else:
        endpoint = llm_base(snapshot['base_url']) + '/chat/completions'
        messages = []
        if body.get('system'):messages.append(dict(role='system', content=str(body.pop('system'))))
        prompt = str(body.pop('prompt', ''))
        image = body.pop('image', '')
        content = prompt
        if image:
            if not str(image).startswith('https://'):
                if os.path.getsize(image) > 20 * 1024 * 1024:raise ValueError('LLM 图像上限为 20 MB')
                with open(image, 'rb') as stream:raw = stream.read()
                image = 'data:' + (mimetypes.guess_type(image)[0] or 'image/png') + ';base64,' + base64.b64encode(raw).decode('ascii')
            content = [dict(type='text', text=prompt), dict(type='image_url', image_url=dict(url=image))]
        messages.append(dict(role='user', content=content))
        body.update(messages=messages, stream=False)
    return dict(endpoint=endpoint, body=body)


class StandardAdapter:
    def __init__(self, api, snapshot):self.api, self.snapshot = api, snapshot
    def __getattr__(self, name):return getattr(self.api, name)
    def run_task(self, unused_id, key, nodes, *, base_url, timeout):
        request = request_description(self.snapshot, nodes)
        response = requests.post(request['endpoint'], headers={'Authorization': 'Bearer ' + key},
                                 json=request['body'], timeout=timeout, allow_redirects=False)
        try:
            try:data = response.json()
            except ValueError:data = None
            task_id = accepted_model_task_id(data)
            if task_id:
                return dict(code=0, data=dict(taskId=task_id, taskStatus=str(data.get('status') or 'QUEUED').upper()))
            error = classify_model_error(data, response.status_code, key)
            if error:
                if error['kind'] == 'busy':
                    return dict(code=421, model_error_code=error['code'], msg=error['message'])
                if error['kind'] == 'rejected':
                    return dict(code=802 if response.status_code in (401, 403) else 301,
                                model_error_code=error['code'], msg=error['message'])
                suffix = ' (code=' + error['code'] + ')' if error['code'] else ''
                raise call_rh.RunningHubResponseError('标准模型返回错误' + suffix + '：' + error['message'])
            response.raise_for_status()
            raise call_rh.RunningHubResponseError('标准模型未返回 taskId，不能确认是否已受理')
        finally:response.close()
