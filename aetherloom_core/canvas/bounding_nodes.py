"""Mask-selected crops and their reversible, typed bounding coordinates."""
import json
import math
from contextlib import ExitStack
from pathlib import Path


SCHEMAS = {
    'image_crop_mask': ('按遮罩裁剪图像', 'mask_tools', [
        ('padding', '边界扩展像素', 'int', 0, (0, 16384)),
        ('threshold', '遮罩阈值', 'float', 0., (0., 1.)),
    ]),
    'image_paste_bounding': ('按 Bounding 合并图像', 'image_tools', []),
}
KINDS = frozenset(SCHEMAS)
_BOUNDING_KEYS = ('version', 'x', 'y', 'width', 'height', 'source_width', 'source_height')
_MAX_JSON_BYTES = 64 * 1024


def inputs(node):
    if node['kind'] == 'image_crop_mask':
        ports = [('image', '图像', 'image'), ('mask', '遮罩', 'mask')]
    elif node['kind'] == 'image_paste_bounding':
        ports = [('background', '背景图像', 'image'), ('foreground', '裁剪图像', 'image'),
                 ('bounding', 'Bounding', 'bounding'), ('mask', '裁剪遮罩', 'mask')]
    else:
        raise ValueError('不支持的 bounding 节点')
    for key, label, editor, _, _ in SCHEMAS[node['kind']][2]:
        ports.append((key, label, 'number' if editor == 'float' else 'int'))
    return [dict(key=key, label=label, type=kind) for key, label, kind in ports]


def _size(size):
    if min(size) < 1 or max(size) > 16384 or size[0] * size[1] > 40_000_000:
        raise ValueError('图像尺寸超限：每边 1–16384，总计最多 4000 万像素')


def validate_bounding(value, source_size=None):
    """Return canonical version-1 bounds without coercing coordinate types."""
    if not isinstance(value, dict) or any(type(value.get(key)) is not int for key in _BOUNDING_KEYS):
        raise ValueError('bounding 必须包含整数 version、x、y、width、height、source_width、source_height')
    bounds = {key: value[key] for key in _BOUNDING_KEYS}
    if bounds['version'] != 1:
        raise ValueError('不支持的 bounding 版本')
    _size((bounds['source_width'], bounds['source_height']))
    _size((bounds['width'], bounds['height']))
    if (bounds['x'] < 0 or bounds['y'] < 0
            or bounds['x'] + bounds['width'] > bounds['source_width']
            or bounds['y'] + bounds['height'] > bounds['source_height']):
        raise ValueError('bounding 区域超出原图范围')
    if source_size is not None and tuple(source_size) != (bounds['source_width'], bounds['source_height']):
        raise ValueError('背景图像尺寸与 bounding 原图尺寸不一致，请使用裁剪时的背景尺寸')
    return bounds


def read_bounding(result, source_size=None):
    """Prefer the snapshotted JSON file over any potentially stale inline value."""
    if not isinstance(result, dict) or (result.get('_content_type') or result.get('type')) != 'bounding':
        raise ValueError('请连接有效的 bounding 输出')
    path = result.get('path')
    if path:
        try:
            with Path(path).open('rb') as stream:
                data = stream.read(_MAX_JSON_BYTES + 1)
            if len(data) > _MAX_JSON_BYTES:
                raise ValueError('bounding 文件超过 64 KiB')
            value = json.loads(data.decode('utf-8-sig'))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError('bounding JSON 文件无法读取或格式无效') from error
    else:
        value = result.get('value')
    return validate_bounding(value, source_size)


def _media_path(batch, key, kind, label):
    from . import model
    item = batch.get(key)
    if not isinstance(item, dict) or model.result_type(item) != kind or not item.get('path'):
        raise ValueError('请连接有效的' + label)
    return item['path']


def _open_image(path, mask=False):
    from PIL import Image, ImageOps
    with Image.open(path) as original:
        # Check both dimensions before decoding or allocating conversion buffers.
        _size(original.size)
        mode = 'L' if mask else 'RGBA' if 'A' in original.getbands() or 'transparency' in original.info else 'RGB'
        with ImageOps.exif_transpose(original) as oriented:
            return oriented.convert(mode)


def _crop_params(params):
    padding = params.get('padding', 0)
    try:
        number = int(padding)
        if isinstance(padding, bool) or str(number) != str(padding) or not 0 <= number <= 16384:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise ValueError('边界扩展像素必须是 0–16384 的整数') from None
    threshold = params.get('threshold', 0.)
    try:
        cutoff = float(threshold)
        if isinstance(threshold, bool) or not math.isfinite(cutoff) or not 0. <= cutoff <= 1.:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise ValueError('遮罩阈值必须在 0–1 之间') from None
    return number, cutoff


def _resize_patch(image, size):
    from PIL import Image
    if image.mode != 'RGBA':
        return image.resize(size, Image.Resampling.BILINEAR)
    # Pillow's RGBA resize premultiplies alpha and can erase hidden RGB. These
    # nodes blend channels independently, including on images with alpha=1-mask.
    with ExitStack() as stack:
        channels = [stack.enter_context(channel) for channel in image.split()]
        resized = [stack.enter_context(channel.resize(size, Image.Resampling.BILINEAR)) for channel in channels]
        return Image.merge('RGBA', resized)


def _background_mask(item, size, directory, stop):
    """Carry a spatially valid, independent selection mask onto the same canvas."""
    from .advanced_nodes import check_stop
    path = item.get('mask_path')
    if not path:
        return {}
    check_stop(stop)
    try:
        mask = _open_image(path, mask=True)
    except (OSError, ValueError):
        return {}
    with mask:
        if mask.size != size:
            return {}
        check_stop(stop)
        target = directory / 'background_mask.png'
        mask.save(target)
        return dict(mask_path=str(target), _processed_mask=True, _mask_nonempty=bool(mask.getbbox()))


def operation(node, batch, params, directory, stop):
    """Return typed outputs; the caller supplies the batch directory and lineage.

    Bounds use the EXIF-oriented image dimensions. Cropping keeps grayscale mask
    values and image alpha independent. Merging replaces the full rectangle when
    no mask is connected; a connected mask blends all channels, including alpha,
    like image_mask_composite. This preserves an unchanged RGBA crop round trip.
    """
    from PIL import Image
    from .advanced_nodes import check_stop
    check_stop(stop)
    kind = node['kind']
    if kind not in KINDS:
        raise ValueError('不支持的 bounding 节点')
    directory = Path(directory)
    with ExitStack() as stack:
        if kind == 'image_crop_mask':
            padding, threshold = _crop_params(params)
            image = stack.enter_context(_open_image(_media_path(batch, 'image', 'image', '图像')))
            check_stop(stop)
            mask = stack.enter_context(_open_image(_media_path(batch, 'mask', 'mask', '遮罩'), mask=True))
            if mask.size != image.size:
                check_stop(stop)
                mask = stack.enter_context(mask.resize(image.size, Image.Resampling.BILINEAR))
            check_stop(stop)
            selection = stack.enter_context(mask.point([255 if value > threshold * 255 else 0 for value in range(256)]))
            box = selection.getbbox()
            if box is None:
                raise ValueError('遮罩在当前阈值下没有选中像素，无法裁剪')
            x, y = max(0, box[0] - padding), max(0, box[1] - padding)
            right, bottom = min(image.width, box[2] + padding), min(image.height, box[3] + padding)
            box = (x, y, right, bottom)
            bounds = validate_bounding(dict(version=1, x=x, y=y, width=right-x, height=bottom-y,
                                            source_width=image.width, source_height=image.height))
            cropped_image = stack.enter_context(image.crop(box))
            cropped_mask = stack.enter_context(mask.crop(box))
            check_stop(stop)
            directory.mkdir(parents=True, exist_ok=True)
            image_path, mask_path, bounding_path = directory / 'cropped_image.png', directory / 'cropped_mask.png', directory / 'bounding.json'
            cropped_image.save(image_path)
            check_stop(stop)
            cropped_mask.save(mask_path)
            check_stop(stop)
            bounding_path.write_text(json.dumps(bounds, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            outputs = [dict(type='image', path=str(image_path), mask_path=str(mask_path),
                            _processed_mask=True, _mask_nonempty=bool(cropped_mask.getbbox())),
                       dict(type='bounding', path=str(bounding_path), value=bounds),
                       dict(type='mask', path=str(mask_path))]
        else:
            background = stack.enter_context(_open_image(_media_path(batch, 'background', 'image', '背景图像')))
            bounds = read_bounding(batch.get('bounding'), background.size)
            check_stop(stop)
            foreground = stack.enter_context(_open_image(_media_path(batch, 'foreground', 'image', '裁剪图像')))
            if foreground.mode != background.mode:
                foreground = stack.enter_context(foreground.convert(background.mode))
            size = bounds['width'], bounds['height']
            if foreground.size != size:
                check_stop(stop)
                foreground = stack.enter_context(_resize_patch(foreground, size))
            mask = None
            if 'mask' in batch:
                mask = stack.enter_context(_open_image(_media_path(batch, 'mask', 'mask', '裁剪遮罩'), mask=True))
                if mask.size != size:
                    check_stop(stop)
                    mask = stack.enter_context(mask.resize(size, Image.Resampling.BILINEAR))
            check_stop(stop)
            background.paste(foreground, (bounds['x'], bounds['y']), mask)
            directory.mkdir(parents=True, exist_ok=True)
            image_path = directory / 'result.png'
            background.save(image_path)
            metadata = _background_mask(batch['background'], background.size, directory, stop)
            outputs = [dict(type='image', path=str(image_path), **metadata)]
    check_stop(stop)
    return outputs
