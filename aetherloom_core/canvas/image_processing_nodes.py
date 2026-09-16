"""Bounded Pillow/NumPy/OpenCV image tools; source files are never modified."""
from contextlib import ExitStack
import math
import os
from pathlib import Path
import re
import uuid


MAX_PIXELS = 16_000_000
MAX_DIMENSION = 16384
MAX_SOURCE_BYTES = 128 * 1024 * 1024
TILE_PIXELS = 131072

SCHEMAS = {
    'image_adjust': ('图像色彩调整', 'image_tools', [
        ('brightness', '亮度', 'float', 1., (0., 4.)),
        ('contrast', '对比度', 'float', 1., (0., 4.)),
        ('saturation', '饱和度', 'float', 1., (0., 4.)),
        ('gamma', 'Gamma', 'float', 1., (.1, 5.)),
        ('black_point', '色阶黑点', 'int', 0, (0, 254)),
        ('white_point', '色阶白点', 'int', 255, (1, 255)),
        ('shadows', '阴影（负值压暗）', 'float', 0., (-1., 1.)),
        ('highlights', '高光（负值压暗）', 'float', 0., (-1., 1.)),
        ('sharpen', '锐化强度', 'float', 0., (0., 5.)),
        ('blur', '高斯模糊半径', 'float', 0., (0., 64.)),
    ]),
    'image_blend': ('图像混合模式', 'image_tools', [
        ('mode', '混合模式', 'enum', 'normal', [(value, label) for value, label in (
            ('normal', '普通'), ('multiply', '正片叠底'), ('screen', '滤色'),
            ('overlay', '叠加'), ('darken', '变暗'), ('lighten', '变亮'), ('difference', '差值'))]),
        ('opacity', '前景不透明度', 'float', 1., (0., 1.)),
    ]),
    'image_transform': ('图像旋转 / 翻转', 'image_tools', [
        ('operation', '变换', 'enum', 'rotate', [('rotate', '旋转'), ('flip_horizontal', '水平翻转'),
            ('flip_vertical', '垂直翻转'), ('transpose', '主对角线转置'), ('transverse', '副对角线转置')]),
        ('angle', '逆时针角度', 'float', 90., (-360., 360.)),
        ('expand', '旋转后扩展画布', 'bool', True, None),
        ('fill', '补角颜色', 'line', '#00000000', None),
    ]),
    'image_edges': ('图像边缘检测', 'image_tools', [
        ('method', '算法', 'enum', 'canny', [('canny', 'Canny'), ('sobel', 'Sobel')]),
        ('low_threshold', 'Canny 低阈值', 'int', 50, (0, 255)),
        ('high_threshold', 'Canny 高阈值', 'int', 150, (0, 255)),
        ('blur', '预模糊半径', 'float', 1., (0., 10.)),
        ('sobel_kernel', 'Sobel 核尺寸（奇数）', 'int', 3, (1, 7)),
    ]),
    'image_gradient': ('生成颜色渐变', 'image_tools', [
        ('width', '宽度', 'int', 1024, (1, MAX_DIMENSION)),
        ('height', '高度', 'int', 1024, (1, MAX_DIMENSION)),
        ('stops', '颜色停止点', 'text', '0:#000000\n1:#ffffff', None),
        ('mode', '渐变方式', 'enum', 'linear', [('linear', '线性'), ('radial', '径向')]),
        ('angle', '线性方向（度）', 'float', 0., (-360., 360.)),
        ('center_x', '径向中心 X', 'float', .5, (0., 1.)),
        ('center_y', '径向中心 Y', 'float', .5, (0., 1.)),
        ('radius', '径向半径 / 短边', 'float', .5, (.01, 2.)),
    ]),
    'image_gradient_map': ('图像渐变映射', 'image_tools', [
        ('stops', '颜色停止点', 'text', '0:#000000\n1:#ffffff', None),
        ('strength', '映射强度', 'float', 1., (0., 1.)),
        ('reverse', '反转明暗映射', 'bool', False, None),
    ]),
    'image_palette': ('提取图像调色板', 'image_tools', [
        ('colors', '最多颜色数', 'int', 8, (2, 64)),
        ('swatch_size', '色块边长', 'int', 64, (8, 256)),
        ('columns', '色块列数', 'int', 8, (1, 64)),
    ]),
    'image_seamless': ('图像边缘无缝融合', 'image_tools', [
        ('ratio', '每侧融合宽度比例', 'float', .15, (0., .5)),
        ('axes', '融合方向', 'enum', 'both', [('both', '横向和纵向'), ('horizontal', '仅左右'), ('vertical', '仅上下')]),
    ]),
}
KINDS = frozenset(SCHEMAS)
OPTIONAL_INPUTS = {'image_blend': frozenset({'mask'})}
OUTPUT_TYPES = {kind: ('image',) for kind in KINDS}
OUTPUT_TYPES.update(image_edges=('image', 'mask'), image_palette=('image', 'text'))
DESCRIPTIONS = {
    'image_adjust': '调整色阶、Gamma、亮度、对比度、饱和度、阴影与高光，再进行 RGB 模糊 / 锐化。Alpha 和附带遮罩保持不变。',
    'image_blend': '前景自动适应背景尺寸，以所选混合模式按透明度叠加合成。可选遮罩白色应用混合、黑色保留背景，灰度按比例混合。',
    'image_transform': '旋转、水平 / 垂直翻转或对角线转置。附带遮罩同步进行相同变换，灰度保持连续，旋转补角遮罩为黑色。',
    'image_edges': '输出黑底白边图像及独立灰度 MASK。透明区域按黑底计算；Canny 使用双阈值，Sobel 输出连续梯度强度。',
    'image_gradient': '使用至少两个颜色停止点生成线性或径向渐变。格式为 0:#000000、0.5:#ff0000、1:#ffffff；可逐行填写纯 HEX，自动等距排列。支持 #RRGGBBAA。',
    'image_gradient_map': '按原图亮度映射到颜色停止点，可反转明暗并调节强度。默认保留原 Alpha；停止点的透明度会进一步降低 Alpha。支持连接调色板的 HEX 文本。',
    'image_palette': '从最多 256 × 256 的缩略样本提取主要颜色，忽略完全透明像素，按出现频率排列。输出色块图及逐行 HEX 文本，可连接渐变节点。',
    'image_seamless': '将相对两边按渐弱权重融合为相同边界，保持原尺寸。适合制作可重复平铺的纹理，会改变边缘内容；附带遮罩同步融合。',
}
HINTS = {kind: '仅处理本地副本，不改原件；每边最多 16384，总计最多 1600 万像素，源文件最多 128 MiB。'
         for kind in KINDS}
HINTS['image_adjust'] += ' Gamma 大于 1 提亮；黑点必须小于白点。'
HINTS['image_transform'] += ' 正角度为逆时针；任意角旋转使用图像双三次 / 遮罩双线性插值。'
HINTS['image_edges'] += ' Canny 检测阶段由 OpenCV 执行，取消在该阶段前后检查。'
HINTS['image_seamless'] += ' 0 不处理，0.5 为每侧最多半幅；RGB 按预乘 Alpha 融合。'


def inputs(node):
    kind = node['kind']
    if kind not in KINDS:raise ValueError('不支持的图像处理节点')
    ports = [] if kind == 'image_gradient' else [('image', '图像', 'image')]
    if kind == 'image_blend':ports = [('background', '背景图像', 'image'), ('foreground', '前景图像', 'image'), ('mask', '混合遮罩', 'mask')]
    for key, label, editor, _, _ in SCHEMAS[kind][2]:
        if editor in ('int', 'float') or key in ('stops', 'fill'):
            ports.append((key, label, 'int' if editor == 'int' else 'number' if editor == 'float' else 'text'))
    return [dict(key=key, label=label, type=typ) for key, label, typ in ports]


def output_type(node):
    if node['kind'] not in KINDS:raise ValueError('不支持的图像处理节点')
    return 'image'


def _check_stop(stop):
    if stop is not None and stop.is_set():
        from .save_results import SaveCanceled
        raise SaveCanceled('本地图像处理已取消')


def _size(size):
    if min(size) < 1 or max(size) > MAX_DIMENSION or size[0] * size[1] > MAX_PIXELS:
        raise ValueError('图像尺寸超限：每边 1–16384，总计最多 1600 万像素')


def _params(kind, supplied):
    values = {}
    for key, label, editor, default, options in SCHEMAS[kind][2]:
        value = supplied.get(key, default)
        if editor in ('float', 'int'):
            try:
                number = float(value)
                if isinstance(value, bool) or not math.isfinite(number) or not options[0] <= number <= options[1]:raise ValueError()
                if editor == 'int' and number != int(number):raise ValueError()
                value = int(number) if editor == 'int' else number
            except (TypeError, ValueError, OverflowError):raise ValueError(label + '超出允许范围') from None
        elif editor == 'bool':
            if type(value) is not bool:raise ValueError(label + '必须是开关值')
        elif editor == 'enum':
            if value not in dict(options):raise ValueError(label + '选项无效')
        elif not isinstance(value, str):raise ValueError(label + '必须是文本')
        values[key] = value
    return values


def _path(batch, key, expected='image'):
    from . import model
    value = batch.get(key)
    if not isinstance(value, dict) or model.result_type(value) != expected or not value.get('path'):
        raise ValueError('请连接有效的' + ('遮罩' if expected == 'mask' else '图像') + '：' + key)
    return value['path']


def _open(path, mask=False):
    from PIL import Image, ImageOps
    path = Path(path)
    if path.stat().st_size > MAX_SOURCE_BYTES:raise ValueError('图像源文件超过 128 MiB')
    with Image.open(path) as source:
        _size(source.size)
        with ImageOps.exif_transpose(source) as oriented:
            mode = 'L' if mask else 'RGBA' if 'A' in oriented.getbands() or 'transparency' in oriented.info else 'RGB'
            return oriented.convert(mode)


def _rows(width, height):
    step = max(1, min(128, TILE_PIXELS // width))
    return ((start, min(height, start + step)) for start in range(0, height, step))


def _save(image, directory, tag, created, stop):
    _check_stop(stop);_size(image.size)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (tag + '-' + uuid.uuid4().hex + '.png')
    temporary = target.with_suffix('.part')
    try:
        image.save(temporary, format='PNG')
        _check_stop(stop)
        os.replace(temporary, target);created.append(target)
    finally:
        temporary.unlink(missing_ok=True)
    return str(target)


def _attached(item, size, stack):
    path = item.get('mask_path')
    if not path:return None
    mask = stack.enter_context(_open(path, mask=True))
    if mask.size != size:raise ValueError('附带遮罩尺寸与图像不一致，请先对齐遮罩')
    return mask


def _mask_metadata(mask, directory, created, stop):
    if mask is None:return {}
    return dict(mask_path=_save(mask, directory, 'mask', created, stop),
                _processed_mask=True, _mask_nonempty=bool(mask.getbbox()))


def _rgba_color(value):
    from PIL import ImageColor
    if len(value) > 128:raise ValueError('颜色格式过长')
    try:return ImageColor.getcolor(value.strip(), 'RGBA')
    except (ValueError, TypeError):raise ValueError('颜色无效，请使用 #RRGGBB 或 #RRGGBBAA') from None


def _stops(value):
    if len(value) > 8192:raise ValueError('颜色停止点文本超过 8192 字符')
    entries = [entry.strip() for entry in re.split(r'[,;\n]+', value) if entry.strip()]
    if not 2 <= len(entries) <= 64:raise ValueError('请提供 2–64 个颜色停止点')
    explicit = [':' in entry for entry in entries]
    if any(explicit) and not all(explicit):raise ValueError('停止点请全部填写位置，或全部只填写 HEX 颜色')
    result = []
    for index, entry in enumerate(entries):
        if explicit[index]:
            position, color = entry.split(':', 1)
            try:position = float(position)
            except ValueError:raise ValueError('停止点位置必须在 0–1 之间') from None
        else:position, color = index / (len(entries) - 1), entry
        if not math.isfinite(position) or not 0 <= position <= 1:raise ValueError('停止点位置必须在 0–1 之间')
        result.append((position, _rgba_color(color)))
    result.sort(key=lambda entry: entry[0])
    if any(a[0] == b[0] for a, b in zip(result, result[1:])):raise ValueError('颜色停止点位置不能重复')
    return result


def _colorize(values, stops):
    import numpy as np
    positions = [entry[0] for entry in stops]
    colors = np.array([entry[1] for entry in stops], dtype=np.float32)
    return np.stack([np.interp(values, positions, colors[:, channel]) for channel in range(4)], axis=-1).astype(np.float32)


def _adjust(image, params, stop):
    import numpy as np
    from PIL import Image, ImageFilter
    black, white = params['black_point'], params['white_point']
    if black >= white:raise ValueError('色阶黑点必须小于白点')
    result = Image.new(image.mode, image.size)
    try:
        for top, bottom in _rows(image.width, image.height):
            _check_stop(stop)
            with image.crop((0, top, image.width, bottom)) as tile:
                pixels = np.array(tile, dtype=np.uint8)
            rgb = pixels[..., :3].astype(np.float32)
            rgb -= black;rgb /= white - black;np.clip(rgb, 0., 1., out=rgb)
            np.power(rgb, 1. / params['gamma'], out=rgb)
            rgb *= params['brightness'];rgb -= .5;rgb *= params['contrast'];rgb += .5
            np.clip(rgb, 0., 1., out=rgb)
            luminance = rgb @ np.array([.2126, .7152, .0722], dtype=np.float32)
            rgb -= luminance[..., None];rgb *= params['saturation'];rgb += luminance[..., None]
            np.clip(rgb, 0., 1., out=rgb)
            for value, weight in ((params['shadows'], (1. - luminance) ** 2), (params['highlights'], luminance ** 2)):
                if value:rgb += value * weight[..., None] * (1. - rgb if value > 0 else rgb)
            pixels[..., :3] = np.rint(np.clip(rgb, 0., 1.) * 255).astype(np.uint8)
            with Image.fromarray(pixels) as tile:result.paste(tile, (0, top))
        if params['blur'] or params['sharpen']:
            with ExitStack() as stack:
                rgb = stack.enter_context(result.convert('RGB'))
                if params['blur']:
                    _check_stop(stop);rgb = stack.enter_context(rgb.filter(ImageFilter.GaussianBlur(params['blur'])))
                if params['sharpen']:
                    _check_stop(stop);rgb = stack.enter_context(rgb.filter(ImageFilter.UnsharpMask(radius=2, percent=round(params['sharpen'] * 150), threshold=3)))
                if result.mode == 'RGBA':
                    alpha = stack.enter_context(result.getchannel('A'))
                    result.paste(rgb);result.putalpha(alpha)
                else:result.paste(rgb)
        return result
    except BaseException:result.close();raise


def _blend(background, foreground, mask, params, stop):
    import numpy as np
    from PIL import Image
    output = Image.new('RGBA' if 'A' in background.getbands() or 'A' in foreground.getbands() else 'RGB', background.size)
    try:
        for top, bottom in _rows(background.width, background.height):
            _check_stop(stop)
            with ExitStack() as stack:
                box = (0, top, background.width, bottom)
                back = stack.enter_context(background.crop(box));front = stack.enter_context(foreground.crop(box))
                back = stack.enter_context(back.convert('RGBA'));front = stack.enter_context(front.convert('RGBA'))
                b = np.array(back, dtype=np.float32) / 255.;f = np.array(front, dtype=np.float32) / 255.
                cb, cs, ab, af = b[..., :3], f[..., :3], b[..., 3:4], f[..., 3:4]
                af *= params['opacity']
                if mask is not None:
                    tile = stack.enter_context(mask.crop(box));af *= np.array(tile, dtype=np.float32)[..., None] / 255.
                mode = params['mode']
                mixed = (cs if mode == 'normal' else cb * cs if mode == 'multiply' else
                         1. - (1. - cb) * (1. - cs) if mode == 'screen' else
                         np.where(cb <= .5, 2. * cb * cs, 1. - 2. * (1. - cb) * (1. - cs)) if mode == 'overlay' else
                         np.minimum(cb, cs) if mode == 'darken' else np.maximum(cb, cs) if mode == 'lighten' else np.abs(cb - cs))
                alpha = af + ab * (1. - af)
                color = (1. - af) * ab * cb + (1. - ab) * af * cs + ab * af * mixed
                np.divide(color, alpha, out=color, where=alpha > 0)
                color = np.where(alpha > 0, color, cb)
                pixels = np.concatenate((color, alpha), axis=-1) if output.mode == 'RGBA' else color
                pixels = np.rint(np.clip(pixels, 0., 1.) * 255).astype(np.uint8)
                tile = stack.enter_context(Image.fromarray(pixels));output.paste(tile, (0, top))
        return output
    except BaseException:output.close();raise


def _transform(image, params, mask=False):
    from PIL import Image
    action = params['operation']
    transpose = {'flip_horizontal': Image.Transpose.FLIP_LEFT_RIGHT, 'flip_vertical': Image.Transpose.FLIP_TOP_BOTTOM,
                 'transpose': Image.Transpose.TRANSPOSE, 'transverse': Image.Transpose.TRANSVERSE}
    if action in transpose:return image.transpose(transpose[action])
    angle = params['angle'] % 360
    if not angle:return image.copy()
    fill = 0 if mask else _rgba_color(params['fill'])
    if params['expand']:
        if angle % 90 == 0:
            size = image.size[::-1] if angle in (90, 270) else image.size
        else:
            # Pixel bounds round both sides of the centered rotated rectangle.
            radians = math.radians(angle)
            cosine, sine = abs(round(math.cos(radians), 15)), abs(round(math.sin(radians), 15))
            extents = (image.width * cosine + image.height * sine, image.width * sine + image.height * cosine)
            size = tuple(math.ceil((side + extent) / 2) - math.floor((side - extent) / 2)
                         for side, extent in zip(image.size, extents))
        _size(size)
    converted = None
    try:
        if not mask and image.mode == 'RGB' and fill[3] < 255 and (angle % 90 or not params['expand']):
            converted = image.convert('RGBA');image = converted
        exact = angle == 180 or angle in (90, 270) and (params['expand'] or image.width == image.height)
        if image.mode == 'RGBA' and not exact:
            # Pillow resamples RGBA through premultiplied RGBa, but forwards
            # fillcolor unchanged. Premultiply both explicitly so translucent
            # corner colors are not brightened by the final unpremultiplication.
            premultiplied_fill = tuple(round(channel * fill[3] / 255.) for channel in fill[:3]) + (fill[3],)
            with image.convert('RGBa') as premultiplied:
                with premultiplied.rotate(angle, resample=Image.Resampling.BICUBIC,
                        expand=params['expand'], fillcolor=premultiplied_fill) as rotated:
                    return rotated.convert('RGBA')
        return image.rotate(angle, resample=Image.Resampling.BILINEAR if mask else Image.Resampling.BICUBIC,
                            expand=params['expand'], fillcolor=fill if mask or image.mode == 'RGBA' else fill[:3])
    finally:
        if converted is not None:converted.close()


def _edges(image, params, stop):
    import cv2
    import numpy as np
    from PIL import Image, ImageChops, ImageFilter
    if params['method'] == 'canny' and params['low_threshold'] > params['high_threshold']:raise ValueError('Canny 低阈值不能大于高阈值')
    kernel = params['sobel_kernel']
    if params['method'] == 'sobel' and kernel not in (1, 3, 5, 7):raise ValueError('Sobel 核尺寸必须是 1、3、5 或 7')
    with ExitStack() as stack:
        gray = stack.enter_context(image.convert('L'))
        if image.mode == 'RGBA':
            alpha = stack.enter_context(image.getchannel('A'));gray = stack.enter_context(ImageChops.multiply(gray, alpha))
        if params['blur']:
            _check_stop(stop);gray = stack.enter_context(gray.filter(ImageFilter.GaussianBlur(params['blur'])))
        pixels = np.array(gray, dtype=np.uint8)
    _check_stop(stop)
    if params['method'] == 'canny':
        output = cv2.Canny(pixels, params['low_threshold'], params['high_threshold'], L2gradient=True)
    else:
        output = np.empty_like(pixels)
        kx, ky = cv2.getDerivKernels(1, 0, kernel, normalize=False)
        scale = 2. / (float(np.abs(kx).sum()) * float(np.abs(ky).sum()))
        halo = max(1, kernel // 2)
        for top, bottom in _rows(image.width, image.height):
            _check_stop(stop);start, end = max(0, top - halo), min(image.height, bottom + halo)
            block = pixels[start:end]
            dx = cv2.Sobel(block, cv2.CV_32F, 1, 0, ksize=kernel, scale=scale, borderType=cv2.BORDER_REPLICATE)
            dy = cv2.Sobel(block, cv2.CV_32F, 0, 1, ksize=kernel, scale=scale, borderType=cv2.BORDER_REPLICATE)
            magnitude = cv2.magnitude(dx, dy)[top-start:bottom-start]
            output[top:bottom] = np.rint(np.clip(magnitude, 0., 255.)).astype(np.uint8)
    _check_stop(stop)
    return Image.fromarray(output)


def _gradient(params, stop):
    import numpy as np
    from PIL import Image
    width, height = params['width'], params['height'];_size((width, height))
    stops = _stops(params['stops']);mode = 'RGBA' if any(color[3] < 255 for _, color in stops) else 'RGB'
    output = Image.new(mode, (width, height))
    x = np.arange(width, dtype=np.float32)[None, :]
    angle = math.radians(params['angle'])
    # Cardinal directions must stay exact on a one-row/one-column image;
    # normalizing a ~1e-16 projection would otherwise invent a full gradient.
    cosine, sine = round(math.cos(angle), 15), round(math.sin(angle), 15)
    low = min(0., (width-1)*cosine) + min(0., (height-1)*sine)
    extent = abs((width-1)*cosine) + abs((height-1)*sine)
    try:
        for top, bottom in _rows(width, height):
            _check_stop(stop);y = np.arange(top, bottom, dtype=np.float32)[:, None]
            if params['mode'] == 'linear':values = (x * cosine + y * sine - low) / (extent or 1.)
            else:values = np.hypot(x - params['center_x']*(width-1), y - params['center_y']*(height-1)) / (params['radius']*min(width, height))
            pixels = np.rint(_colorize(values, stops)).astype(np.uint8)
            with Image.fromarray(pixels if mode == 'RGBA' else pixels[..., :3]) as tile:output.paste(tile, (0, top))
        return output
    except BaseException:output.close();raise


def _gradient_map(image, params, stop):
    import numpy as np
    from PIL import Image
    stops = _stops(params['stops'])
    values = np.linspace(1., 0., 256) if params['reverse'] else np.linspace(0., 1., 256)
    lookup = _colorize(values, stops)
    mode = 'RGBA' if image.mode == 'RGBA' or any(color[3] < 255 for _, color in stops) else 'RGB'
    output = Image.new(mode, image.size);strength = params['strength']
    try:
        for top, bottom in _rows(image.width, image.height):
            _check_stop(stop)
            with ExitStack() as stack:
                tile = stack.enter_context(image.crop((0, top, image.width, bottom)))
                gray = stack.enter_context(tile.convert('L'));rgba = stack.enter_context(tile.convert('RGBA'))
                original = np.array(rgba, dtype=np.uint8);mapped = lookup[np.array(gray, dtype=np.uint8)]
                rgb = original[..., :3] * (1. - strength) + mapped[..., :3] * strength
                alpha = original[..., 3:4].astype(np.float32) * (1. - strength + strength * mapped[..., 3:4] / 255.)
                pixels = np.concatenate((rgb, alpha), axis=-1) if mode == 'RGBA' else rgb
                result = stack.enter_context(Image.fromarray(np.rint(np.clip(pixels, 0, 255)).astype(np.uint8)))
                output.paste(result, (0, top))
        return output
    except BaseException:output.close();raise


def _palette(image, params, stop):
    import numpy as np
    from PIL import Image, ImageDraw
    with ExitStack() as stack:
        sample = stack.enter_context(image.copy());sample.thumbnail((256, 256), Image.Resampling.BOX)
        sample = stack.enter_context(sample.convert('RGBA'));pixels = np.array(sample, dtype=np.uint8).reshape(-1, 4)
        visible = pixels[pixels[:, 3] > 0, :3]
        if not len(visible):raise ValueError('图像完全透明，没有可提取的可见颜色')
        sample = stack.enter_context(Image.fromarray(visible.reshape(1, len(visible), 3)))
        _check_stop(stop)
        quantized = stack.enter_context(sample.quantize(colors=params['colors'], method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))
        palette = quantized.getpalette()
        counts = sorted(quantized.getcolors() or [], key=lambda pair: (-pair[0], tuple(palette[pair[1]*3:pair[1]*3+3])))
        colors = [tuple(palette[index*3:index*3+3]) for _, index in counts]
    columns = min(params['columns'], len(colors));rows = math.ceil(len(colors) / columns);side = params['swatch_size']
    _size((columns*side, rows*side));result = Image.new('RGB', (columns*side, rows*side), '#ffffff')
    try:
        draw = ImageDraw.Draw(result)
        for index, color in enumerate(colors):
            _check_stop(stop);x, y = index % columns * side, index // columns * side
            draw.rectangle((x, y, x+side-1, y+side-1), fill=color)
        return result, '\n'.join('#%02X%02X%02X' % color for color in colors)
    except BaseException:result.close();raise


def _pair_blend(left, right, weight):
    import numpy as np
    a, b = left.astype(np.float32) / 255., right.astype(np.float32) / 255.
    alpha = a.shape[-1] == 4
    if alpha:a[..., :3] *= a[..., 3:4];b[..., :3] *= b[..., 3:4]
    average = (a + b) * .5
    result = []
    for original, source in ((a, left), (b, right)):
        value = original * (1. - weight) + average * weight
        if alpha:np.divide(value[..., :3], value[..., 3:4], out=value[..., :3], where=value[..., 3:4] > 0)
        pixels = np.rint(np.clip(value, 0., 1.) * 255).astype(np.uint8)
        # No blending must also retain RGB hidden behind a zero alpha value.
        if alpha:np.copyto(pixels, source, where=weight == 0)
        result.append(pixels)
    return result


def _seamless(image, params, stop):
    import numpy as np
    from PIL import Image
    pixels = np.array(image, dtype=np.uint8)
    if pixels.ndim == 2:pixels = pixels[..., None]
    height, width = pixels.shape[:2];ratio = params['ratio']
    horizontal = min(width//2, round(width*ratio)) if params['axes'] != 'vertical' else 0
    vertical = min(height//2, round(height*ratio)) if params['axes'] != 'horizontal' else 0
    if horizontal:
        weight = (1. - np.arange(horizontal, dtype=np.float32) / max(1, horizontal-1))[None, :, None]
        for top, bottom in _rows(width, height):
            _check_stop(stop)
            left, right = _pair_blend(pixels[top:bottom, :horizontal], pixels[top:bottom, -horizontal:][:, ::-1], weight)
            pixels[top:bottom, :horizontal] = left;pixels[top:bottom, -horizontal:] = right[:, ::-1]
    if vertical:
        for top, bottom in _rows(width, vertical):
            _check_stop(stop)
            weight = (1. - np.arange(top, bottom, dtype=np.float32) / max(1, vertical-1))[:, None, None]
            upper, lower = _pair_blend(pixels[top:bottom], pixels[height-bottom:height-top][::-1], weight)
            pixels[top:bottom] = upper;pixels[height-bottom:height-top] = lower[::-1]
    return Image.fromarray(pixels[..., 0] if image.mode == 'L' else pixels)


def operation(node, batch, params, directory, stop):
    """Typed per-item results; shared execution attaches indices and lineage."""
    from PIL import Image
    _check_stop(stop)
    kind = node['kind']
    if kind not in KINDS:raise ValueError('不支持的图像处理节点')
    params = _params(kind, params);directory = Path(directory);created = []
    try:
        with ExitStack() as stack:
            if kind == 'image_gradient':
                result = stack.enter_context(_gradient(params, stop));attached = None
            else:
                primary = 'background' if kind == 'image_blend' else 'image'
                image = stack.enter_context(_open(_path(batch, primary)));_check_stop(stop)
                attached = None if kind in ('image_edges', 'image_palette') else _attached(batch[primary], image.size, stack)
                if kind == 'image_adjust':result = stack.enter_context(_adjust(image, params, stop))
                elif kind == 'image_blend':
                    front = stack.enter_context(_open(_path(batch, 'foreground')))
                    if front.size != image.size:
                        _check_stop(stop);front = stack.enter_context(front.resize(image.size, Image.Resampling.BILINEAR))
                    mask = None
                    if 'mask' in batch:
                        mask = stack.enter_context(_open(_path(batch, 'mask', 'mask'), mask=True))
                        if mask.size != image.size:mask = stack.enter_context(mask.resize(image.size, Image.Resampling.BILINEAR))
                    result = stack.enter_context(_blend(image, front, mask, params, stop))
                elif kind == 'image_transform':
                    _check_stop(stop);result = stack.enter_context(_transform(image, params))
                    if attached is not None:attached = stack.enter_context(_transform(attached, params, mask=True))
                elif kind == 'image_edges':
                    attached = stack.enter_context(_edges(image, params, stop))
                    result = stack.enter_context(attached.convert('RGB'))
                elif kind == 'image_gradient_map':result = stack.enter_context(_gradient_map(image, params, stop))
                elif kind == 'image_palette':
                    result, text = _palette(image, params, stop);stack.enter_context(result);attached = None
                else:
                    result = stack.enter_context(_seamless(image, params, stop))
                    if attached is not None:attached = stack.enter_context(_seamless(attached, params, stop))
            _check_stop(stop)
            output = dict(type='image', path=_save(result, directory, kind, created, stop))
            output.update(_mask_metadata(attached, directory, created, stop))
            outputs = [output]
            if kind == 'image_edges':outputs.append(dict(type='mask', path=output['mask_path']))
            elif kind == 'image_palette':
                path = directory / ('palette-' + uuid.uuid4().hex + '.txt')
                with path.open('x', encoding='utf-8', newline='\n') as stream:
                    created.append(path);stream.write(text + '\n')
                outputs.append(dict(type='text', path=str(path), text=text))
            _check_stop(stop)
            return outputs
    except BaseException:
        for path in created:path.unlink(missing_ok=True)
        raise
