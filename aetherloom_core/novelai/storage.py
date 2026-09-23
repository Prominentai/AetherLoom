"""Bounded local NovelAI settings, original image bytes and parameter imports."""
import copy
import io
import json
import os
import time
import threading
import uuid
from pathlib import Path

from PIL import Image
from .catalog import default_options

MAX_JSON = 12 * 1024 * 1024
MAX_IMAGES = 500
_HISTORY_LOCK = threading.RLock()
SECRET_FIELDS = {'api_key', 'api_keys', 'token', 'authorization', 'password', 'access_token', 'cookie'}
PUBLIC_FIELDS = set(default_options()) | {
    'model', 'action', 'prompt', 'negative_prompt', 'width', 'height', 'steps', 'scale',
    'sampler', 'noise_schedule', 'seed', 'n_samples', 'cfg_rescale', 'strength', 'noise',
    'quality_preset', 'uc_preset', 'characters', 'references', 'image_path', 'mask_path',
    'timeout', 'stream', 'straight_alpha', 'transparent', 'variety', 'skip_cfg_above_sigma',
    'dynamic_thresholding', 'sm', 'sm_dyn', 'color_correct', 'req_type', 'defry',
    'extra_noise_seed', 'upscale', 'params_version', 'prefer_brownian', 'normalize_reference_strengths',
    'chunks', 'focused', 'focus_padding', 'resolved_prompt', 'resolved_negative_prompt',
    'resolved_characters', 'resolved_negative_characters', 'request_seed', 'character_position_mode',
}


def public(value):
    if isinstance(value, dict):
        return {str(k): public(v) for k, v in value.items()
                if str(k).lower() not in SECRET_FIELDS and not str(k).startswith('_')}
    if isinstance(value, (list, tuple)):
        return [public(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def atomic_json(path, value):
    path = Path(path)
    data = json.dumps(public(value), ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')
    if len(data) > MAX_JSON:
        raise ValueError('NovelAI 配置数据过大，请减少历史条目或提示词长度。')
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name('.' + uuid.uuid4().hex + '.part')
    try:
        with part.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)


def read_json(path, default):
    path = Path(path)
    try:
        if path.stat().st_size > MAX_JSON:
            return copy.deepcopy(default)
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return copy.deepcopy(default)


def load_settings(directory):
    data = read_json(Path(directory) / 'novelai' / 'settings.json', {})
    return data if isinstance(data, dict) else {}


def save_settings(directory, settings):
    atomic_json(Path(directory) / 'novelai' / 'settings.json', settings)


def load_history(directory):
    with _HISTORY_LOCK:
        data = read_json(Path(directory) / 'novelai' / 'history.json', [])
        if not isinstance(data, list):
            return []
        return [v for v in data[:MAX_IMAGES] if isinstance(v, dict) and isinstance(v.get('path'), str)]


def bounded_history(items):
    result, size = [], 2
    for record in items[:MAX_IMAGES]:
        clean = public(record)
        try:
            rendered = json.dumps(clean, ensure_ascii=False, allow_nan=False, indent=2)
            length = len(rendered.encode('utf8')) + 2 * rendered.count('\n') + 8
        except (TypeError, ValueError, RecursionError):
            continue
        if size + length > MAX_JSON - 65536:
            break
        result.append(clean)
        size += length
    return result


def save_history(directory, items):
    """Replace the index atomically. Concurrent callers should use update_history."""
    with _HISTORY_LOCK:
        atomic_json(Path(directory) / 'novelai' / 'history.json', bounded_history(items))


def update_history(directory, additions=(), removed=()):
    """Apply an atomic delta shared by queue workers and the history UI.

    `removed` contains record IDs (or paths for legacy records without IDs).
    A caller's stale list can never erase records added by another worker.
    """
    additions = list(additions)
    removed = set(removed)
    with _HISTORY_LOCK:
        previous = load_history(directory)
        merged, seen = [], set()
        for record in additions + previous:
            if not isinstance(record, dict) or not isinstance(record.get('path'), str):
                continue
            identity = record.get('id') or record['path']
            if not isinstance(identity, str) or identity in seen or identity in removed:
                continue
            seen.add(identity)
            merged.append(record)
        merged = bounded_history(merged)
        save_history(directory, merged)
        return merged


def delete_result_files(records):
    """Delete only explicitly referenced result files; keep every input intact.

    A failed batch can be retried with the original records: missing files are
    already complete, and duplicate paths are processed only once. Nested
    settings, masks, references, and directories are never traversed.
    """
    paths, errors = {}, []
    for record in records:
        value = record.get('path') if isinstance(record, dict) else None
        if not isinstance(value, str) or not value.strip():
            errors.append('结果记录缺少有效的本地文件路径。')
            continue
        try:
            # Normalize spelling without resolving a file symlink to its target.
            path = Path(os.path.abspath(value))
            paths.setdefault(os.path.normcase(str(path)), path)
        except (OSError, ValueError) as error:
            errors.append(f'结果文件路径无效：{error}')
    for path in paths.values():
        try:
            if path.is_dir():
                raise OSError('结果路径是目录，拒绝删除目录。')
            path.unlink(missing_ok=True)
        except (OSError, ValueError) as error:
            errors.append(f'{path}：{error}')
    if errors:
        raise OSError('部分结果文件删除失败，任务已保留，可重试；已删除的文件不会恢复。\n' + '\n'.join(errors))


class SaveError(OSError):
    def __init__(self, message, saved, pending):
        super().__init__(message)
        self.saved, self.pending = saved, pending


def output_directory(directory):
    target = Path(directory) / 'NovelAI' / time.strftime('%Y-%m-%d')
    target.mkdir(parents=True, exist_ok=True)
    return target


def verify_output_directory(directory):
    target = output_directory(directory) / ('.write-' + uuid.uuid4().hex)
    try:
        with target.open('xb') as stream:
            stream.write(b'')
    finally:
        target.unlink(missing_ok=True)


def save_results(directory, snapshot, results):
    saved = []
    target_dir = output_directory(directory)
    for position, result in enumerate(results):
        try:
            raw = result['bytes']
            if not isinstance(raw, bytes) or not raw or len(raw) > 64 * 1024 * 1024:
                raise ValueError('图像内容无效或超过 64 MB')
            with Image.open(io.BytesIO(raw)) as im:
                suffix = {'PNG': '.png', 'WEBP': '.webp', 'JPEG': '.jpg'}.get(im.format)
                if not suffix or im.width * im.height > 40_000_000:
                    raise ValueError('结果图像格式或尺寸不受支持')
                width, height = im.size
                im.verify()
            identity = uuid.uuid4().hex
            target = target_dir / (time.strftime('%H%M%S') + '_' + identity[:12] + suffix)
            part = target.with_suffix('.part')
            try:
                with part.open('xb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(part, target)
            finally:
                part.unlink(missing_ok=True)
            options = public(snapshot)
            # None means the service did not report this image's actual seed.
            options['seed'] = result.get('seed')
            for key in ('resolved_prompt', 'resolved_negative_prompt', 'resolved_characters', 'resolved_negative_characters', 'request_seed'):
                if key in result:
                    options[key] = public(result[key])
            saved.append(dict(id=identity, path=str(target), created=time.time(), width=width,
                              height=height, seed=options['seed'], index=result.get('index', position),
                              settings=options))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SaveError('图片已生成，但本地保存失败：' + str(exc), saved, results[position:]) from None
    return saved


def import_options(data, *, trusted_paths=False):
    if not isinstance(data, dict):
        raise ValueError('参数文件必须是 JSON 对象')
    if isinstance(data.get('settings'), dict):
        data = data['settings']
    result = {key: public(value) for key, value in data.items() if key in PUBLIC_FIELDS}
    for old, current in (('transparent', 'transparent_background'), ('variety', 'variety_boost'),
                         ('req_type', 'augment_method')):
        if current not in result and old in result:
            result[current] = result[old]
    if not trusted_paths:
        # An imported picture/preset must not cause unrelated local files to be uploaded.
        result.pop('image_path', None)
        result.pop('mask_path', None)
        result.pop('references', None)
    return result


def read_image_settings(path):
    path = Path(path)
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('导入图片超过 64 MB')
    with Image.open(path) as image:
        if image.width * image.height > 40_000_000:
            raise ValueError('图片尺寸过大')
        text = image.info.get('Comment', '')
        source = image.info.get('Source', '')
        software = image.info.get('Software', '')
        description = image.info.get('Description', '')
        dimensions = image.size
    if not isinstance(text, str) or len(text) > 1024 * 1024:
        return {}
    if not text and not description:
        return {}
    try:
        data = json.loads(text) if text else {}
    except (ValueError, TypeError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    signature = (str(source) + ' ' + str(software)).lower()
    caption_schema = any(isinstance(data.get(key), dict) and isinstance(data[key].get('caption'), dict)
                         for key in ('v4_prompt', 'v4_negative_prompt'))
    actual_schema = isinstance(data.get('actual_prompts'), dict) and any(
        isinstance(data['actual_prompts'].get(key), dict)
        and isinstance(data['actual_prompts'][key].get('base_caption'), str)
        for key in ('prompt', 'negative_prompt'))
    compatible = (isinstance(data.get('prompt') or data.get('uc'), str)
                  and len({'steps', 'scale', 'seed', 'sampler'} & data.keys()) >= 2)
    if not ('novelai' in signature or caption_schema or actual_schema or compatible):
        return {}
    result = import_options(data)
    result.setdefault('width', dimensions[0])
    result.setdefault('height', dimensions[1])
    result['prompt'] = str(data.get('prompt') or description or '')
    result['negative_prompt'] = str(data.get('uc') or data.get('negative_prompt') or '')
    if data.get('model_name'):
        result['model'] = str(data['model_name'])
    elif 'V4.5' in str(source):
        result['model'] = 'nai-diffusion-4-5-curated' if 'Curated' in str(source) else 'nai-diffusion-4-5-full'
    elif 'V5' in str(source):
        result['model'] = 'nai-diffusion-5-curated' if 'Curated' in str(source) else 'nai-diffusion-5-full'
    positive = data.get('v4_prompt')
    negative = data.get('v4_negative_prompt')
    actual = data.get('actual_prompts')
    # Preserve editable prompts and resolved captions independently, so imports
    # never silently replace the original random/chunk expressions.
    if isinstance(actual, dict):
        for key, resolved, raw_characters in (
            ('prompt', 'resolved_prompt', 'resolved_characters'),
            ('negative_prompt', 'resolved_negative_prompt', 'resolved_negative_characters'),
        ):
            caption = actual.get(key)
            if not isinstance(caption, dict):
                continue
            if isinstance(caption.get('base_caption'), str):
                result[resolved] = caption['base_caption']
            characters = caption.get('char_captions')
            if isinstance(characters, list):
                result[raw_characters] = [public(item) for item in characters[:32] if isinstance(item, dict)]
        # Older images can provide only actual_prompts, without editable captions.
        if not isinstance(positive, dict) and isinstance(actual.get('prompt'), dict):
            positive = dict(caption=actual['prompt'], use_coords=False)
        if not isinstance(negative, dict) and isinstance(actual.get('negative_prompt'), dict):
            negative = dict(caption=actual['negative_prompt'])
    if text:
        result.setdefault('quality_preset', 'none')
        result.setdefault('uc_preset', 'none')
    negative_caption = negative.get('caption') if isinstance(negative, dict) else None
    if isinstance(negative_caption, dict) and isinstance(negative_caption.get('base_caption'), str):
        result['negative_prompt'] = negative_caption['base_caption']
    if isinstance(positive, dict):
        caption = positive.get('caption')
        if isinstance(caption, dict):
            if isinstance(caption.get('base_caption'), str):
                result['prompt'] = caption['base_caption']
            positive_chars = caption.get('char_captions')
            negative_chars = negative_caption.get('char_captions') if isinstance(negative_caption, dict) else None
            positive_chars = positive_chars if isinstance(positive_chars, list) else []
            negative_chars = negative_chars if isinstance(negative_chars, list) else []
            characters = []
            for i, character in enumerate(positive_chars[:32]):
                if not isinstance(character, dict):
                    continue
                centers = character.get('centers')
                center = centers[0] if isinstance(centers, list) and centers and isinstance(centers[0], dict) else {}
                negative_character = negative_chars[i] if i < len(negative_chars) else {}
                negative_text = negative_character.get('char_caption', '') if isinstance(negative_character, dict) else ''
                prompt = character.get('char_caption', '')
                # Invalid metadata must not fail while constructing numeric Qt controls.
                coordinates = {}
                for axis in ('x', 'y'):
                    value = center.get(axis, .5)
                    coordinates[axis] = value if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1 else .5
                characters.append(dict(prompt=prompt if isinstance(prompt, str) else '',
                                       negative_prompt=negative_text if isinstance(negative_text, str) else '',
                                       **coordinates, use_coords=positive.get('use_coords') is True))
            result['characters'] = characters
    return result
