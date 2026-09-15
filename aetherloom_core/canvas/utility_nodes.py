"""Local canvas operations. Schemas are shared by ports, forms and workers."""
import copy
import re
import string
import threading
from pathlib import Path


# Fields: key, caption, editor, default, options/range. Connected values override
# these defaults only in the immutable execution snapshot.
SCHEMAS = {
    'image_resize': ('图像缩放', 'image_tools', [
        ('width', '宽度', 'int', 1024, (1, 16384)), ('height', '高度', 'int', 1024, (1, 16384)),
        ('mode', '缩放方式', 'enum', 'contain', [('contain', '等比适应'), ('stretch', '拉伸到指定尺寸')]),
        ('resolution_steps', '分辨率倍数', 'int', 1, (1, 256))]),
    'image_crop': ('图像裁剪', 'image_tools', [
        ('width', '宽度', 'int', 512, (1, 16384)), ('height', '高度', 'int', 512, (1, 16384)),
        ('anchor', '裁剪位置', 'enum', 'center', [('center', '居中'), ('top_left', '左上角')])]),
    'image_pad': ('图像补边', 'image_tools', [
        ('width', '画布宽度', 'int', 1024, (1, 16384)), ('height', '画布高度', 'int', 1024, (1, 16384)),
        ('color', '背景颜色', 'line', '#00000000', None)]),
    'media_info': ('媒体信息', 'output', [
        ('property', '读取信息', 'enum', 'width', [('width', '宽度'), ('height', '高度'),
         ('duration', '时长（秒）'), ('fps', '帧率'), ('frames', '帧数（视频估算）')])]),
    'text_template': ('文本模板', 'text_tools', [('template', '模板', 'text', '{主题}', None)]),
    'text_split': ('文本拆分', 'text_tools', [
        ('text', '文本', 'text', '', None), ('separator', '分隔符', 'line', '\n', None),
        ('drop_empty', '跳过空条目', 'bool', True, None)]),
    'text_join': ('文本拼接', 'text_tools', [('separator', '分隔符', 'line', '\n', None)]),
    'image_compare': ('图像对比', 'image_tools', []),
    'reroute': ('转接点', 'utility', []),
    'note': ('便笺', 'utility', [('text', '说明', 'text', '', None)]),
}
from . import advanced_nodes
SCHEMAS.update(advanced_nodes.SCHEMAS)
KINDS = frozenset(SCHEMAS)
_IMAGE_SLOT = threading.BoundedSemaphore(1)


def defaults(kind):
    return {key: copy.deepcopy(default) for key, _, _, default, _ in SCHEMAS[kind][2]}


def template_keys(text):
    keys = []
    try:
        for _, key, spec, conversion in string.Formatter().parse(text):
            if key is None:continue
            if not key or not re.fullmatch(r'[\w\-]+', key) or spec or conversion:
                raise ValueError('模板使用 {名称} 占位符；字面括号请写成 {{ 和 }}')
            if key not in keys:keys.append(key)
    except ValueError as error:
        raise ValueError('模板括号或占位符格式错误：' + str(error)) from error
    if len(keys) > 32:raise ValueError('模板最多支持 32 个占位符')
    return keys


def inputs(node):
    if node['kind'] in advanced_nodes.KINDS:return advanced_nodes.inputs(node)
    kind = node['kind']
    ports = []
    if kind in ('image_resize', 'image_crop', 'image_pad'):
        ports.append(('image', '图像', 'image'))
    elif kind == 'media_info':ports.append(('value', '媒体', 'any'))
    elif kind == 'image_compare':ports.extend([('a', '图像 A', 'image'), ('b', '图像 B', 'image')])
    elif kind == 'text_join':ports.append(('value', '文本', 'text_input'))
    elif kind == 'reroute':ports.append(('value', '内容', 'any'))
    elif kind == 'text_template':
        # Only applied templates enter params; a text draft cannot alter cables.
        try:keys = template_keys(node.get('params', {}).get('template', '{主题}'))
        except ValueError:keys = []
        ports.extend(('var:' + key, key, 'text') for key in keys)
    for key, label, editor, _, _ in SCHEMAS[kind][2]:
        if editor == 'int' or kind == 'text_split' and key == 'text':
            ports.append((key, label, 'int' if editor == 'int' else 'text'))
    return [dict(key=key, label=label, type=kind) for key, label, kind in ports]


def output_type(node):
    if node['kind'] in advanced_nodes.KINDS:return advanced_nodes.output_type(node)
    kind = node['kind']
    if kind == 'media_info':return 'float' if node.get('params', {}).get('property', 'width') in ('duration', 'fps') else 'int'
    if kind.startswith('image_'):return 'image'
    if kind.startswith('text_'):return 'text'
    return 'any'


def validate(node):
    params = node.get('params', {})
    for key, label, editor, default, options in SCHEMAS[node['kind']][2]:
        value = params.get(key, default)
        if editor == 'int':
            try:
                number = int(value)
                if isinstance(value, bool) or str(number) != str(value) or not options[0] <= number <= options[1]:raise ValueError()
            except (TypeError, ValueError):raise ValueError(label + '超出允许的整数范围')
        elif editor == 'float':
            try:
                import math
                number=float(value)
                if isinstance(value,bool) or not math.isfinite(number) or not options[0]<=number<=options[1]:raise ValueError()
            except (ValueError,TypeError):raise ValueError(label+'超出允许的数值范围')
        elif editor == 'bool' and type(value) is not bool:raise ValueError(label + '必须为开关值')
        elif editor == 'enum' and value not in dict(options):raise ValueError(label + '选项无效')
        elif editor in ('text', 'line') and not isinstance(value, str):raise ValueError(label + '必须为文本')
    if node['kind'] == 'text_template':template_keys(params.get('template', '{主题}'))
    if node['kind']=='subgraph' and any(key in params and type(params[key]) is not bool for key in ('collapsed','expose_unconnected')):
        raise ValueError('组合节点开关格式错误')


def _text(result):
    from . import model
    if model.result_type(result) != 'text':raise ValueError('需要文本内容')
    return str(model.input_value(result, {'fieldType': 'STRING'}))


def _image(path):
    from PIL import Image, ImageOps
    with Image.open(path) as source:
        if source.width * source.height > 40_000_000:raise ValueError('本地图像处理上限为 4000 万像素')
        return ImageOps.exif_transpose(source).convert('RGBA')


def resize_dimensions(source_size, width, height, mode, multiple=1):
    """Compute the final size before resampling, including latent alignment."""
    if not 1 <= multiple <= 256:raise ValueError('分辨率倍数必须为 1–256 的整数')
    if mode == 'contain':
        source_w, source_h = source_size
        if source_w * height > source_h * width:
            height = max(1, round(source_h / source_w * width))
        elif source_w * height < source_h * width:
            width = max(1, round(source_w / source_h * height))
    elif mode != 'stretch':raise ValueError('缩放方式无效')
    # Same nearest-multiple rule as ComfyUI's resolution_steps. Clamp tiny
    # dimensions to one multiple rather than creating an invalid zero size.
    width = max(1, round(width / multiple)) * multiple
    height = max(1, round(height / multiple)) * multiple
    if width > 16384 or height > 16384 or width * height > 40_000_000:
        raise ValueError('对齐后尺寸超限：每边最多 16384，最多 4000 万像素')
    return width, height


def _information(result, prop):
    from . import model
    kind = model.result_type(result)
    if kind == 'batch':raise ValueError('媒体信息逐项读取，请先使用 Batch 转 List')
    path = result.get('path')
    if not path or not Path(path).is_file():raise ValueError('请连接有效的本地媒体文件')
    if kind == 'image':
        from PIL import Image
        with Image.open(path) as image:
            size = image.size
            if image.getexif().get(274) in (5, 6, 7, 8):size = size[::-1]
            values = dict(width=size[0], height=size[1], frames=getattr(image, 'n_frames', 1))
    elif kind in ('video', 'audio'):
        # Metadata-only reader: no frame iteration, audio decode or player.
        from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos
        info = ffmpeg_parse_infos(str(path))
        values = {'duration': float(info.get('duration', 0))}
        if kind == 'video' and info.get('video_found'):
            size = info.get('video_size', [0, 0])
            if int(info.get('video_rotation', 0)) % 180 == 90:size = size[::-1]
            fps = float(info.get('video_fps', 0))
            values.update(width=int(size[0]), height=int(size[1]), fps=float(info.get('video_fps', 0)),
                          frames=round(float(info.get('video_duration', values['duration'])) * fps))
    else:raise ValueError('媒体信息只接收图像、视频或音频')
    if prop not in values:raise ValueError('该媒体不具有所选信息，请更换“读取信息”')
    return values[prop]


def execute(node, directory, batches, stop):
    if node['kind'] in advanced_nodes.KINDS:return advanced_nodes.execute(node,directory,batches,stop)
    # Large local pixel buffers are serialized independently of cloud branches.
    # Waiting for this slot remains cancellable and never blocks the GUI thread.
    if node['kind'] not in ('image_resize', 'image_crop', 'image_pad'):
        return _execute(node, directory, batches, stop)
    from .save_results import SaveCanceled
    while not _IMAGE_SLOT.acquire(timeout=.1):
        if stop.is_set():raise SaveCanceled('本地处理已取消')
    try:return _execute(node, directory, batches, stop)
    finally:_IMAGE_SLOT.release()


def _execute(node, directory, batches, stop):
    from . import model
    from .save_results import SaveCanceled
    kind = node['kind'];params = dict(defaults(kind), **node.get('params', {}))
    results = []
    for batch in batches:
        if stop.is_set():raise SaveCanceled('本地处理已取消')
        def value(key):
            if key not in batch:return params[key]
            return model.input_value(batch[key], {'fieldType': 'INT'})
        output = []
        if kind == 'reroute':output = [copy.deepcopy(batch['value'])]
        elif kind == 'note':continue
        elif kind == 'media_info':output = [dict(value=_information(batch['value'], params['property']), type=output_type(node))]
        elif kind == 'text_template':
            keys = template_keys(params['template'])
            variables = {key: _text(batch['var:' + key]) if 'var:' + key in batch else str(params.get('var:' + key, '')) for key in keys}
            output = [dict(text=params['template'].format_map(variables), type='text')]
        elif kind == 'text_split':
            text = _text(batch['text']) if 'text' in batch else params['text']
            if not params['separator']:raise ValueError('分隔符不能为空')
            parts = text.split(params['separator'], 10000)
            if len(parts) > 10000:raise ValueError('一次拆分最多 10000 条文本')
            output = [dict(text=part, type='text') for part in parts if part or not params['drop_empty']]
            if not output:raise ValueError('拆分后没有有效文本')
        elif kind == 'text_join':
            source = batch.get('value')
            if source is None:raise ValueError('请连接文本或文本 Batch')
            members = model.batch_items(source) if model.result_type(source) == 'batch' else [source]
            output = [dict(text=params['separator'].join(_text(item) for item in members), type='text')]
        elif kind == 'image_compare':
            if not all(key in batch for key in ('a', 'b')):raise ValueError('请连接两张图像')
            output = [copy.deepcopy(batch['a']), copy.deepcopy(batch['b'])]
        else:
            from PIL import Image, ImageOps, ImageColor
            if 'image' not in batch:raise ValueError('请连接图像')
            width, height = int(value('width')), int(value('height'))
            if not 1 <= width <= 16384 or not 1 <= height <= 16384 or width * height > 40_000_000:
                raise ValueError('目标尺寸超限：每边 1–16384，最多 4000 万像素')
            with _image(batch['image']['path']) as image:
                if kind == 'image_resize':
                    size = resize_dimensions(image.size, width, height, params['mode'], int(value('resolution_steps')))
                    processed = image.resize(size, Image.Resampling.LANCZOS)
                elif kind == 'image_crop':
                    if width > image.width or height > image.height:raise ValueError('裁剪尺寸超过原图，请先缩放或补边')
                    x, y = ((image.width-width)//2, (image.height-height)//2) if params['anchor'] == 'center' else (0, 0)
                    processed = image.crop((x, y, x+width, y+height))
                else:
                    if width < image.width or height < image.height:raise ValueError('补边画布小于原图，请先缩放或裁剪')
                    processed = Image.new('RGBA', (width, height), ImageColor.getcolor(params['color'], 'RGBA'))
                    processed.alpha_composite(image, ((width-image.width)//2, (height-image.height)//2))
                try:
                    if stop.is_set():raise SaveCanceled('本地处理已取消')
                    Path(directory).mkdir(parents=True, exist_ok=True)
                    target = Path(directory) / (kind + '_' + str(len(results)) + '.png')
                    processed.save(target)
                    output_size = processed.size
                finally:processed.close()
            output = [dict(path=str(target), type='image')]
            # A separate input mask follows the exact same spatial transform.
            # Keep it grayscale; never turn mask edits into image alpha edits.
            mask_path = batch['image'].get('mask_path')
            if mask_path:
                with Image.open(mask_path) as original:
                    if original.width*original.height > 40_000_000:raise ValueError('遮罩处理上限为 4000 万像素')
                    mask = ImageOps.exif_transpose(original).convert('L')
                try:
                    with Image.open(batch['image']['path']) as original:
                        source_size = original.size
                        if original.getexif().get(274) in (5,6,7,8):source_size = source_size[::-1]
                    if mask.size != source_size:
                        resized = mask.resize(source_size, Image.Resampling.BILINEAR);mask.close();mask = resized
                    if kind == 'image_resize':changed = mask.resize(output_size, Image.Resampling.BILINEAR)
                    elif kind == 'image_crop':changed = mask.crop((x,y,x+width,y+height))
                    else:
                        changed = Image.new('L',output_size,0)
                        changed.paste(mask,((width-mask.width)//2,(height-mask.height)//2))
                    try:
                        mask_target = target.with_name(target.stem+'_mask.png');changed.save(mask_target)
                    finally:changed.close()
                finally:mask.close()
                output[0].update(mask_path=str(mask_target), _processed_mask=True)
        for result in output:
            result['index'] = len(results)
            result['lineage'] = model.result_lineage(batch, node['id'], len(results))
            results.append(result)
    if stop.is_set():raise SaveCanceled('本地处理已取消')
    return results
