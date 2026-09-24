"""NovelAI canvas contracts and immutable request options, without Qt or HTTP."""

import copy
import os
from decimal import Decimal

from aetherloom_core.novelai import catalog


KIND_ACTIONS = {
    'novelai_generate': 'generate',
    'novelai_img2img': 'img2img',
    'novelai_infill': 'infill',
    'novelai_enhance': 'img2img',
    'novelai_upscale': 'upscale',
    'novelai_augment': 'augment',
}
KINDS = frozenset(KIND_ACTIONS)
TITLES = dict(zip(KIND_ACTIONS, (
    'NovelAI 图像生成', 'NovelAI 图生图', 'NovelAI 局部重绘',
    'NovelAI 图像增强', 'NovelAI 超分辨率', 'NovelAI Director Tools',
)))
# Match the existing NovelAI upscaler contract without importing its HTTP client.
UPSCALE_MODEL = 'nai-diffusion-5-curated'
PORT_OPTION_KEYS = {'prompt': 'prompt', 'negative': 'negative_prompt',
                    'image': 'image_path', 'mask': 'mask_path'}
_GENERATION_PORTS = (
    ('prompt', '提示词', 'text_input'), ('negative', '负面提示词', 'text_input'),
    ('references', '参考图', 'image_input'), ('seed', '种子', 'int'),
    ('width', '宽度', 'int'), ('height', '高度', 'int'),
    ('steps', '步数', 'int'), ('scale', '提示词引导', 'float'),
    ('n_samples', '出图数量', 'int'),
)
_IMAGE_PORT = ('image', '图像（单张）', 'image_input')
PORTS = {
    'novelai_generate': _GENERATION_PORTS,
    'novelai_img2img': (_IMAGE_PORT,) + _GENERATION_PORTS + (
        ('strength', '重绘强度', 'float'), ('noise', '噪声', 'float')),
    'novelai_infill': (_IMAGE_PORT, ('mask', '蒙版（单张）', 'mask_input'))
        + _GENERATION_PORTS + (('inpaint_strength', '局部重绘强度', 'float'),),
    'novelai_enhance': (_IMAGE_PORT,) + _GENERATION_PORTS + (
        ('strength', '增强强度', 'float'), ('noise', '噪声', 'float')),
    'novelai_upscale': (_IMAGE_PORT,),
    'novelai_augment': (_IMAGE_PORT, ('prompt', '工具提示词', 'text_input')),
}


def _kind(node_or_kind):
    kind = node_or_kind.get('kind') if isinstance(node_or_kind, dict) else node_or_kind
    if kind not in KINDS:
        raise ValueError('不支持的 NovelAI 节点类型')
    return kind


def option_key(node_or_kind, port):
    if port == 'prompt' and _kind(node_or_kind) == 'novelai_augment':
        return 'tool_prompt'
    return PORT_OPTION_KEYS.get(port, port)


def default_params(kind):
    kind = _kind(kind)
    options = catalog.default_options()
    options['action'] = KIND_ACTIONS[kind]
    if kind == 'novelai_enhance':
        options.update(enhancement=True, strength=.2, noise=0.)
    elif kind == 'novelai_upscale':
        options['model'] = UPSCALE_MODEL
    return {'options': options}


def inputs(node):
    return [dict(key=key, label=label, type=kind) for key, label, kind in PORTS[_kind(node)]]


def validate(node):
    """Validate saved structure while allowing incomplete, editable workflows."""
    _kind(node)
    params = node.get('params', {})
    if not isinstance(params, dict) or not isinstance(params.get('options', {}), dict):
        raise ValueError('NovelAI 节点 options 必须是参数字典')
    options = params.get('options', {})
    for key in ('model', 'prompt', 'negative_prompt', 'tool_prompt', 'image_path', 'mask_path'):
        if key in options and not isinstance(options[key], str):
            raise ValueError('NovelAI 文本或路径参数格式错误：' + key)
    for key in ('references', 'characters'):
        if key in options and (not isinstance(options[key], list)
                or any(not isinstance(value, dict) for value in options[key])):
            raise ValueError('NovelAI 列表参数格式错误：' + key)


def local_options(node):
    """Return a fresh configuration; a node's operation is fixed by its kind."""
    validate(node)
    kind = node['kind']
    options = default_params(kind)['options']
    options.update(copy.deepcopy(node.get('params', {}).get('options', {})))
    options['action'] = KIND_ACTIONS[kind]
    options['enhancement'] = kind == 'novelai_enhance'
    if kind != 'novelai_enhance':
        options['upscaled_enhance'] = False
    if kind == 'novelai_upscale':
        options['model'] = UPSCALE_MODEL
    return options


def required_inputs(node):
    kind = _kind(node)
    return (() if kind == 'novelai_generate' else ('image',)) + (
        ('mask',) if kind == 'novelai_infill' else ())


def input_batches(node, values):
    """Zip List entries and broadcast singletons; keep each Batch grouped."""
    from . import model
    allowed = {port['key'] for port in inputs(node)}
    if not isinstance(values, dict) or any(key not in allowed for key in values):
        raise ValueError('NovelAI 输入端口无效')
    if not values:
        return [{}]
    if any(not isinstance(items, list) or not items
           or any(not isinstance(item, dict) for item in items) for items in values.values()):
        raise ValueError('NovelAI 已连接的输入列表不能为空')
    count = max(map(len, values.values()))
    if any(len(items) not in (1, count) for items in values.values()):
        raise ValueError('NovelAI 多个输入 List 长度必须相同，或仅有一项以供复用')
    batches = []
    for index in range(count):
        batch = {key: copy.deepcopy(items[0 if len(items) == 1 else index])
                 for key, items in values.items()}
        # Explicit positional pairing may combine different positions from a
        # shared ancestor. Keep only unambiguous ancestry; the output node adds
        # its own axis, so downstream lists still have a stable correspondence.
        origins = {}
        for value in batch.values():
            for key, position in model.lineage(value).items():
                origins.setdefault(key, set()).add(position)
        conflicts = {key for key, positions in origins.items() if len(positions) > 1}
        if conflicts:
            for value in batch.values():
                value['lineage'] = {key: position for key, position in model.lineage(value).items()
                                    if key not in conflicts}
        batches.append(batch)
    return batches


def _media_paths(value, *, mask=False, single=False):
    from . import model
    members = model.batch_items(value) if model.result_type(value) == 'batch' else [value]
    label = '蒙版' if mask else '图像'
    if single and len(members) != 1:
        raise ValueError(f'NovelAI {label}一次只能接受一张；请先将多图 Batch 转为 List 逐项执行')
    expected = 'mask' if mask else 'image'
    paths = []
    for member in members:
        item = model.normalize_result(member)
        if model.result_type(item) != expected:
            raise ValueError(f'NovelAI {label}输入类型错误，Batch 内也必须全部是{label}')
        path = item.get('path')
        if not isinstance(path, str) or not os.path.isfile(path):
            raise ValueError(f'NovelAI {label}输入文件不存在')
        paths.append(path)
    return paths


def _wired_references(options, paths):
    templates = catalog.active_references(options)
    kind = 'vibe' if options.get('reference_mode', 'vibe') == 'vibe' else 'character&style'
    refs = []
    for index, path in enumerate(paths):
        reference = (copy.deepcopy(templates[index]) if index < len(templates)
                     else catalog.reference_defaults(options['model'], kind))
        reference.update(path=path, enabled=True)
        refs.append(reference)
    return refs


def prepare_options(node, batch=None):
    """Overlay one connected row onto an independent, validated snapshot."""
    from . import model
    options = local_options(node)
    ports = {port['key']: port for port in inputs(node)}
    batch = {} if batch is None else batch
    if not isinstance(batch, dict) or any(key not in ports for key in batch):
        raise ValueError('NovelAI 输入端口无效')
    for key, value in batch.items():
        if not isinstance(value, dict):
            raise ValueError('NovelAI 输入结果格式错误：' + key)
        target = option_key(node, key)
        if key in ('image', 'mask'):
            options[target] = _media_paths(value, mask=key == 'mask', single=True)[0]
        elif key == 'references':
            options[target] = _wired_references(options, _media_paths(value))
        elif ports[key]['type'] == 'text_input':
            options[target] = model.input_value(value, {'_canvas_text_batch': True})
        else:
            if model.result_type(value) == 'batch':
                raise ValueError('NovelAI 数值参数不接受 Batch，请先转为 List')
            raw = model.input_value(value, {'fieldType': ports[key]['type']})
            options[target] = int(Decimal(raw)) if ports[key]['type'] == 'int' else float(raw)
    options = catalog.validate_options(options)
    for key in required_inputs(node):
        path = options[option_key(node, key)]
        if not os.path.isfile(path):
            raise ValueError('NovelAI 本地输入文件不存在：' + ports[key]['label'])
    if options['action'] in ('generate', 'img2img', 'infill'):
        for reference in catalog.active_references(options):
            if not os.path.isfile(reference['path']):
                raise ValueError('NovelAI 参考图文件不存在')
    return options


def build_requests(node, batches):
    """Validate every row before the caller submits any paid request."""
    if not isinstance(batches, (list, tuple)) or not batches:
        raise ValueError('NovelAI 没有可执行的输入组')
    requests = [prepare_options(node, batch) for batch in batches]
    checked = {}
    for options in requests:
        validate_media(options, checked=checked)
    return requests


def _checked_media(path, checked):
    """Decode each unchanged local file once, retaining only small metadata."""
    from PIL import Image, UnidentifiedImageError
    before = os.stat(path)
    identity = (os.path.normcase(os.path.abspath(path)), before.st_dev, before.st_ino,
                before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    key = ('image', identity)
    if key in checked:
        return checked[key]
    if before.st_size > 32 * 1024 * 1024:
        raise ValueError('NovelAI 输入图像超过 32 MiB')
    try:
        with Image.open(path) as image:
            if image.width * image.height > 16_777_216 or getattr(image, 'n_frames', 1) != 1:
                raise ValueError('NovelAI 输入图像须为单帧且不超过 16777216 像素')
            if image.format not in ('PNG', 'JPEG', 'WEBP', 'BMP'):
                raise ValueError('NovelAI 输入图像格式只支持 PNG、JPEG、WebP、BMP')
            image.verify()
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            if image.getexif().get(274) in (5, 6, 7, 8):
                width, height = height, width
            info = dict(size=(width, height), mode=image.mode, bytes=before.st_size, identity=identity)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise ValueError('无法读取 NovelAI 输入图像，请选择有效的本地图像文件') from None
    after = os.stat(path)
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('NovelAI 输入文件在预检时发生变化，请重新运行')
    checked[key] = info
    return info


def _check_vibe_png(path, info, checked):
    """A compact JPEG may exceed the client's limit after PNG conversion."""
    from PIL import Image, ImageOps
    key = ('vibe_png', info['identity'])
    if key in checked:
        return

    class SizeCounter:
        size = 0

        def write(self, data):
            self.size += len(data)
            if self.size > 32 * 1024 * 1024:
                raise ValueError('NovelAI Vibe 参考图编码为 PNG 后超过 32 MiB')
            return len(data)

        def flush(self):
            pass

    with Image.open(path) as source, ImageOps.exif_transpose(source) as oriented:
        with oriented.convert('RGBA') as rgba, Image.new('RGBA', oriented.size, 'white') as flattened:
            flattened.alpha_composite(rgba)
            with flattened.convert('RGB') as image:
                image.save(SizeCounter(), format='PNG')
    checked[key] = True


def validate_media(options, *, checked=None):
    """Check active media for all rows before the queue can submit the first.

    These are the existing image client's local limits. Disabled references and
    inactive image/mask settings are drafts and must not block another action.
    The queue still freezes and revalidates inputs at submission time.
    """
    from PIL import Image, ImageOps
    checked = {} if checked is None else checked
    action = options['action']
    if action != 'generate':
        original = _checked_media(options['image_path'], checked)
        if action == 'upscale' and original['size'][0] * original['size'][1] > 4_194_304:
            raise ValueError('NovelAI 2×超分的输入图像不能超过 4194304 像素')
    if action == 'infill':
        info = _checked_media(options['mask_path'], checked)
        if info['size'] != original['size']:
            raise ValueError('NovelAI 蒙版尺寸必须与原输入图像一致')
        if info['mode'] not in ('1', 'L', 'RGB'):
            raise ValueError('NovelAI 蒙版须为灰度图（白色编辑、黑色保留）')
        focused = bool(options.get('focused'))
        padding = int(options.get('focus_padding', 64)) if focused else 0
        if focused and not 0 <= padding <= 2048:
            raise ValueError('聚焦重绘边距必须为 0 到 2048 像素')
        key = ('mask', info['identity'], options['width'], options['height'], focused, padding)
        if key not in checked:
            with Image.open(options['mask_path']) as source, ImageOps.exif_transpose(source) as oriented:
                mask = oriented.convert('L')
            try:
                if focused:
                    box = mask.getbbox()
                    if not box:
                        raise ValueError('NovelAI 蒙版没有可编辑区域')
                    box = (max(0, box[0] - padding), max(0, box[1] - padding),
                           min(mask.width, box[2] + padding), min(mask.height, box[3] + padding))
                    cropped = mask.crop(box)
                    mask.close()
                    mask = cropped
                from aetherloom_core.novelai.masks import quantize_mask
                # Use the same sampling/threshold as the eventual submission.
                with quantize_mask(mask, (options['width'], options['height'])):
                    pass
            finally:
                mask.close()
            checked[key] = True
    if action in ('generate', 'img2img', 'infill'):
        total = 0
        for reference in catalog.active_references(options):
            info = _checked_media(reference['path'], checked)
            total += info['bytes']
            if total > 128 * 1024 * 1024:
                raise ValueError('NovelAI 参考图总大小超过 128 MiB')
            if reference['kind'] == 'vibe':
                _check_vibe_png(reference['path'], info, checked)


def fingerprint_params(node, connected=()):
    """Hash active local files and options; connected values are hashed by model."""
    from . import model
    options = local_options(node)
    connected = set(connected)
    for port in connected:
        key = option_key(node, port)
        if port == 'references':
            # The connection replaces paths, but retains active card settings.
            options[key] = [{name: value for name, value in ref.items() if name != 'path'}
                            for ref in catalog.active_references(options)]
        else:
            options[key] = {'connected': True}
    for port in required_inputs(node):
        key = option_key(node, port)
        if port not in connected and options.get(key):
            options[key] = {'sha256': model.file_hash(options[key])}
    if 'references' not in connected and options['action'] in ('generate', 'img2img', 'infill'):
        for reference in options.get('references', []):
            if reference.get('enabled', True) and reference.get('path'):
                reference['path'] = {'sha256': model.file_hash(reference['path'])}
    params = copy.deepcopy(node.get('params', {}))
    params['options'] = options
    return params
