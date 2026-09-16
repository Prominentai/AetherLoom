"""Bounded local mask regions, hole filling and soft-mask arithmetic."""
import math
import os
from contextlib import ExitStack
from pathlib import Path
import uuid


SCHEMAS = {
    'mask_regions': ('遮罩区域筛选', 'mask_tools', [
        ('mode', '保留区域', 'enum', 'largest', [('largest', '最大区域'), ('smallest', '最小区域'), ('area', '按面积筛选')]),
        ('threshold', '区域阈值', 'float', .5, (0., 1.)),
        ('minimum', '最小面积（像素）', 'int', 1, (1, 16_000_000)),
        ('maximum', '最大面积（0 为不限）', 'int', 0, (0, 16_000_000)),
        ('connectivity', '相邻规则', 'enum', '8', [('8', '八邻域'), ('4', '四邻域')]),
    ]),
    'mask_fill_holes': ('遮罩填孔', 'mask_tools', [
        ('threshold', '区域阈值', 'float', .5, (0., 1.)),
        ('maximum', '最大孔洞面积（0 为不限）', 'int', 0, (0, 16_000_000)),
        ('connectivity', '相邻规则', 'enum', '8', [('8', '八邻域'), ('4', '四邻域')]),
    ]),
    'mask_rectangle': ('矩形遮罩', 'mask_tools', [
        ('canvas_width', '画布宽度', 'int', 512, (1, 16384)),
        ('canvas_height', '画布高度', 'int', 512, (1, 16384)),
        ('unit', '位置 / 尺寸单位', 'enum', 'pixels', [('pixels', '像素'), ('percent', '百分比')]),
        ('x', '横向位置', 'float', 0., (-16384., 16384.)),
        ('y', '纵向位置', 'float', 0., (-16384., 16384.)),
        ('width', '矩形宽度', 'float', 256., (0., 16384.)),
        ('height', '矩形高度', 'float', 256., (0., 16384.)),
        ('feather', '羽化半径（像素）', 'float', 0., (0., 128.)),
    ]),
    'mask_math': ('遮罩运算', 'mask_tools', [
        ('operation', '运算', 'enum', 'intersection', [
            ('intersection', '交集（相乘）'), ('union', '并集（滤色）'),
            ('subtract', '差集 A − B'), ('add', '相加（饱和）'),
            ('maximum', '最大值'), ('minimum', '最小值'),
        ]),
    ]),
}
KINDS = frozenset(SCHEMAS)
OUTPUT_TYPES = {kind: ('mask',) for kind in KINDS}
DESCRIPTIONS = {
    'mask_regions': '按连通区域保留最大、最小或指定面积的遮罩，保留选中区域原有灰度。',
    'mask_fill_holes': '填充遮罩内部的封闭孔洞；连接图像边缘的背景不会被填充。',
    'mask_rectangle': '按像素或百分比生成可羽化的矩形遮罩。',
    'mask_math': '对同尺寸软遮罩进行交集、并集、差集、饱和相加或最大 / 最小值运算。',
}
HINTS = {
    'mask_regions': '面积按高于阈值的像素计数；同面积时保留从上到下、从左到右最先出现的区域。最多 1600 万像素。',
    'mask_fill_holes': '孔洞填为白色，其他像素保留原灰度；面积 0 表示填充所有封闭孔洞。最多 1600 万像素。',
    'mask_rectangle': '百分比相对于画布宽高，位置可为负数，超出部分裁掉；羽化半径始终使用像素。',
    'mask_math': '按 0–1 灰度计算：交集 A×B；并集 A+B−A×B；差集 max(A−B,0)；相加 min(A+B,1)。两张遮罩尺寸必须相同。',
}
_MAX_COMPONENT_PIXELS = 16_000_000
_MAX_SOURCE_BYTES = 128 * 1024 * 1024


def _open_mask(path, maximum_pixels=40_000_000):
    """Reject an oversized connected-component source before decoding it."""
    from PIL import Image, ImageOps
    from .bounding_nodes import _size
    path = Path(path)
    if path.stat().st_size > _MAX_SOURCE_BYTES:
        raise ValueError('遮罩源文件超过 128 MiB')
    with Image.open(path) as source:
        _size(source.size)
        if source.width * source.height > maximum_pixels:
            raise ValueError('遮罩区域处理最多支持 1600 万像素，请先缩小遮罩')
        with ImageOps.exif_transpose(source) as oriented:
            return oriented.convert('L')


def _save_mask(image, directory, stop):
    """Publish a fresh file only after saving and checking cancellation."""
    from .advanced_nodes import check_stop
    check_stop(stop)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ('mask-' + uuid.uuid4().hex + '.png')
    temporary = target.with_suffix('.part')
    try:
        image.save(temporary, format='PNG')
        check_stop(stop)
        os.replace(temporary, target)
        check_stop(stop)
        return str(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def inputs(node):
    kind = node['kind']
    if kind not in KINDS:
        raise ValueError('不支持的遮罩区域节点')
    ports = [] if kind == 'mask_rectangle' else ([('a', '遮罩 A', 'mask'), ('b', '遮罩 B', 'mask')]
                                                if kind == 'mask_math' else [('mask', '遮罩', 'mask')])
    for key, title, editor, _, _ in SCHEMAS[kind][2]:
        if editor in ('int', 'float'):
            ports.append((key, title, 'int' if editor == 'int' else 'number'))
    return [dict(key=key, label=title, type=kind) for key, title, kind in ports]


def output_type(node):
    return 'mask'


def _number(params, key, default, minimum, maximum, integer=False):
    raw = params.get(key, default)
    try:
        value = float(raw)
        if isinstance(raw, bool) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError()
        if integer and value != int(value):
            raise ValueError()
        return int(value) if integer else value
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f'{key} 必须是 {minimum}–{maximum} 范围内的' + ('整数' if integer else '有限数值')) from None


def _components(mask, params, background, stop):
    import cv2
    import numpy as np
    from .advanced_nodes import check_stop
    if mask.width * mask.height > _MAX_COMPONENT_PIXELS:
        raise ValueError('遮罩区域处理最多支持 1600 万像素，请先缩小遮罩')
    threshold = _number(params, 'threshold', .5, 0., 1.)
    connectivity = str(params.get('connectivity', '8'))
    if connectivity not in ('4', '8'):
        raise ValueError('相邻规则必须是四邻域或八邻域')
    array = np.array(mask, dtype=np.uint8)
    binary = np.asarray(array <= threshold * 255 if background else array > threshold * 255, dtype=np.uint8)
    check_stop(stop)
    # SAUF assigns labels in row-major order, including equal-area ties.
    count, labels = cv2.connectedComponentsWithAlgorithm(binary, int(connectivity), cv2.CV_32S, cv2.CCL_WU)
    del binary
    check_stop(stop)
    areas = np.bincount(labels.ravel(), minlength=count)
    return array, labels, areas


def operation(node, batch, params, directory, stop):
    import numpy as np
    from PIL import Image, ImageFilter
    from .advanced_nodes import check_stop
    from .bounding_nodes import _media_path, _size
    kind = node['kind']
    if kind not in KINDS:
        raise ValueError('不支持的遮罩区域节点')
    check_stop(stop)
    directory = Path(directory)
    with ExitStack() as stack:
        if kind == 'mask_rectangle':
            size = tuple(_number(params, key, 512, 1, 16384, True) for key in ('canvas_width', 'canvas_height'))
            _size(size)
            unit = params.get('unit', 'pixels')
            if unit not in ('pixels', 'percent'):
                raise ValueError('矩形单位必须是像素或百分比')
            # Values above 100% intentionally describe an oversize rectangle;
            # clipping is cheap and avoids silently changing settings on a unit switch.
            limit = 16384.
            values = [_number(params, key, default, -limit if key in ('x', 'y') else 0., limit)
                      for key, default in (('x', 0.), ('y', 0.), ('width', 256.), ('height', 256.))]
            if unit == 'percent':
                values = [value * size[index % 2] / 100. for index, value in enumerate(values)]
            x, y, width, height = values
            left, top = max(0, round(x)), max(0, round(y))
            right, bottom = min(size[0], round(x + width)), min(size[1], round(y + height))
            result = stack.enter_context(Image.new('L', size, 0))
            if right > left and bottom > top:
                result.paste(255, (left, top, right, bottom))
            radius = _number(params, 'feather', 0., 0., 128.)
            if radius:
                check_stop(stop)
                result = stack.enter_context(result.filter(ImageFilter.GaussianBlur(radius)))
        elif kind == 'mask_math':
            a = stack.enter_context(_open_mask(_media_path(batch, 'a', 'mask', '遮罩 A')))
            check_stop(stop)
            b = stack.enter_context(_open_mask(_media_path(batch, 'b', 'mask', '遮罩 B')))
            if a.size != b.size:
                raise ValueError('两张遮罩尺寸不一致，请先使用遮罩缩放节点对齐')
            mode = params.get('operation', 'intersection')
            if mode not in ('intersection', 'union', 'subtract', 'add', 'maximum', 'minimum'):
                raise ValueError('不支持的遮罩运算')
            result = stack.enter_context(Image.new('L', a.size))
            # Bands bound float intermediates even for a 40 MP mask.
            for top in range(0, a.height, 128):
                check_stop(stop)
                box = (0, top, a.width, min(a.height, top + 128))
                with a.crop(box) as part_a, b.crop(box) as part_b:
                    aa, bb = np.asarray(part_a, dtype=np.float32), np.asarray(part_b, dtype=np.float32)
                    if mode == 'intersection': value = aa * bb / 255.
                    elif mode == 'union': value = aa + bb - aa * bb / 255.
                    elif mode == 'subtract': value = aa - bb
                    elif mode == 'add': value = aa + bb
                    elif mode == 'maximum': value = np.maximum(aa, bb)
                    else: value = np.minimum(aa, bb)
                    with Image.fromarray(np.rint(np.clip(value, 0, 255)).astype(np.uint8)) as part:
                        result.paste(part, (0, top))
        else:
            mask = stack.enter_context(_open_mask(_media_path(batch, 'mask', 'mask', '遮罩'), _MAX_COMPONENT_PIXELS))
            array, labels, areas = _components(mask, params, kind == 'mask_fill_holes', stop)
            keep = np.zeros(len(areas), dtype=bool)
            maximum = _number(params, 'maximum', 0, 0, _MAX_COMPONENT_PIXELS, True)
            if kind == 'mask_fill_holes':
                keep[:] = True
                keep[np.unique(np.concatenate((labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1])))] = False
                if maximum:
                    keep &= areas <= maximum
            else:
                mode = params.get('mode', 'largest')
                if mode == 'area':
                    minimum = _number(params, 'minimum', 1, 1, _MAX_COMPONENT_PIXELS, True)
                    if maximum and maximum < minimum:
                        raise ValueError('最大面积不能小于最小面积')
                    keep = (areas >= minimum) & (areas <= (maximum or _MAX_COMPONENT_PIXELS))
                elif mode in ('largest', 'smallest'):
                    if len(areas) > 1:
                        selected = int((np.argmax if mode == 'largest' else np.argmin)(areas[1:])) + 1
                        keep[selected] = True
                else:
                    raise ValueError('不支持的区域筛选方式')
            keep[0] = False
            for top in range(0, mask.height, 128):
                check_stop(stop)
                selection = keep[labels[top:top + 128]]
                if kind == 'mask_fill_holes': array[top:top + 128][selection] = 255
                else: array[top:top + 128][~selection] = 0
            del labels, areas, keep
            result = stack.enter_context(Image.fromarray(array))
        check_stop(stop)
        target = _save_mask(result, directory, stop)
    return [dict(type='mask', path=target)]
