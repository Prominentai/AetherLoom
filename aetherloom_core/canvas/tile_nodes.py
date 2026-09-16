"""Typed image tiles and explicit-Batch reconstruction without new dependencies."""
import json
import os
import shutil
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path


SCHEMAS = {
    'image_tile': ('图像分块', 'image_tools', [
        ('width', '分块宽度', 'int', 512, (1, 4096)),
        ('height', '分块高度', 'int', 512, (1, 4096)),
        ('overlap', '重叠像素', 'int', 64, (0, 4095)),
    ]),
    'image_untile': ('图像分块合并', 'image_tools', [
        ('feather', '重叠区域羽化', 'bool', True, None),
    ]),
}
KINDS = frozenset(SCHEMAS)
OUTPUT_TYPES = {'image_tile': ('image', 'bounding'), 'image_untile': ('image',)}
DESCRIPTIONS = {
    'image_tile': '将图像和附带遮罩按行切成对应的图像列表与 Bounding 列表。',
    'image_untile': '将图像 Batch 与 Bounding Batch 按来源和坐标配对，分别合并每张原图，可羽化重叠区域。',
}
HINTS = {
    'image_tile': '从上到下、从左到右输出，最多 256 块；RGBA 和独立遮罩保留。分块尺寸须大于重叠像素。',
    'image_untile': '分别将图像 List、Bounding List 转为 Batch 后连接；多个来源按 Bounding 中首次出现的顺序输出图像 List。两路顺序可不同，但每个来源必须完整且唯一；普通 List 仍逐项运行，分块尺寸不得改变。',
}
_MAX_TILES = 256
_MAX_TILE_PIXELS = 160_000_000
_MAX_JSON_BYTES = 64 * 1024


def inputs(node):
    if node['kind'] == 'image_tile':
        ports = [('image', '图像', 'image')]
        ports.extend((key, title, 'int') for key, title, _, _, _ in SCHEMAS['image_tile'][2])
    elif node['kind'] == 'image_untile':
        ports = [('images', '图像', 'image_input'), ('bounding', 'Bounding', 'bounding_input')]
    else:
        raise ValueError('不支持的图像分块节点')
    return [dict(key=key, label=title, type=kind) for key, title, kind in ports]


def output_type(node):
    return 'image'


def _integer(params, key, default, low, high):
    value = params.get(key, default)
    try:
        number = int(value)
        if isinstance(value, bool) or number != float(value) or not low <= number <= high:
            raise ValueError()
        return number
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f'{key} 必须是 {low}–{high} 的整数') from None


def _starts(length, tile, overlap):
    if length <= tile:
        return [0]
    step = tile - overlap
    count = (length - tile + step - 1) // step + 1
    if count > _MAX_TILES:
        raise ValueError('图像分块最多 256 块，请增大分块尺寸或减小重叠')
    # The final tile may be smaller. Shifting it back to the edge would invent
    # overlap even when the user explicitly selected zero.
    return [index * step for index in range(count)]


def _tile(batch, params, directory, stop):
    from .advanced_nodes import check_stop
    from .bounding_nodes import _media_path, _open_image
    width = _integer(params, 'width', 512, 1, 4096)
    height = _integer(params, 'height', 512, 1, 4096)
    overlap = _integer(params, 'overlap', 64, 0, 4095)
    if overlap >= min(width, height):
        raise ValueError('重叠像素必须小于分块宽度和高度')
    with ExitStack() as stack:
        image = stack.enter_context(_open_image(_media_path(batch, 'image', 'image', '图像')))
        check_stop(stop)
        xs, ys = _starts(image.width, width, overlap), _starts(image.height, height, overlap)
        count = len(xs) * len(ys)
        if count > _MAX_TILES:
            raise ValueError('图像分块最多 256 块，请增大分块尺寸或减小重叠')
        total_pixels = (sum(min(width, image.width - x) for x in xs)
                        * sum(min(height, image.height - y) for y in ys))
        if total_pixels > _MAX_TILE_PIXELS:
            raise ValueError('分块含重叠的总面积超过 1.6 亿像素，请减小重叠')
        mask = None
        if batch['image'].get('mask_path'):
            mask = stack.enter_context(_open_image(batch['image']['mask_path'], mask=True))
            if mask.size != image.size:
                raise ValueError('图像附带遮罩的尺寸不一致，请先对齐遮罩')
        group, output = uuid.uuid4().hex, []
        directory.mkdir(parents=True, exist_ok=True)
        for y in ys:
            for x in xs:
                check_stop(stop)
                index = len(output) // 2
                box = (x, y, min(image.width, x + width), min(image.height, y + height))
                bounds = dict(version=1, x=x, y=y, width=box[2] - x, height=box[3] - y,
                              source_width=image.width, source_height=image.height,
                              tile_group=group, tile_index=index, tile_count=count, overlap=overlap)
                metadata = dict(tile_group=group, _tile_index=index, _tile_count=count)
                image_path = directory / f'tile_{index + 1:04d}.png'
                bounding_path = directory / f'bounding_{index + 1:04d}.json'
                with image.crop(box) as tile:
                    tile.save(image_path)
                item = dict(type='image', path=str(image_path), **metadata)
                if mask is not None:
                    check_stop(stop)
                    mask_path = directory / f'mask_{index + 1:04d}.png'
                    with mask.crop(box) as tile_mask:
                        tile_mask.save(mask_path)
                        item.update(mask_path=str(mask_path), _processed_mask=True,
                                    _mask_nonempty=bool(tile_mask.getbbox()))
                bounding_path.write_text(json.dumps(bounds, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                output.extend((item, dict(type='bounding', path=str(bounding_path), value=bounds, **metadata)))
    return output


def _members(item, kind, label):
    from . import model
    if not isinstance(item, dict):
        raise ValueError('请连接' + label)
    values = model.batch_items(item) if model.result_type(item) == 'batch' else [item]
    if any(model.result_type(value) != kind for value in values):
        raise ValueError(label + '中存在不兼容的类型')
    return values


def _tile_bounds(item):
    from .bounding_nodes import validate_bounding
    if item.get('path'):
        try:
            with Path(item['path']).open('rb') as stream:
                raw = stream.read(_MAX_JSON_BYTES + 1)
            if len(raw) > _MAX_JSON_BYTES:
                raise ValueError('Bounding 文件超过 64 KiB')
            value = json.loads(raw.decode('utf-8-sig'))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError('分块 Bounding JSON 无法读取') from error
    else:
        value = item.get('value')
    bounds = validate_bounding(value)
    if (not isinstance(value.get('tile_group'), str) or not 1 <= len(value['tile_group']) <= 128
            or type(value.get('tile_count')) is not int or not 1 <= value['tile_count'] <= _MAX_TILES
            or type(value.get('tile_index')) is not int or not 0 <= value['tile_index'] < value['tile_count']
            or type(value.get('overlap')) is not int or not 0 <= value['overlap'] <= 4095):
        raise ValueError('需要图像分块节点产生的 Bounding，普通裁剪坐标不能用于分块合并')
    bounds.update({key: value[key] for key in ('tile_group', 'tile_count', 'tile_index', 'overlap')})
    for top_key, file_key in (('tile_group', 'tile_group'), ('_tile_index', 'tile_index'), ('_tile_count', 'tile_count')):
        if top_key in item and item[top_key] != bounds[file_key]:
            raise ValueError('分块 Bounding 的文件内容与来源标记不一致')
    return bounds


def _same_origin(image, bounding, bounds):
    from . import model
    # Transforms retain lineage even when they intentionally replace other
    # image metadata. A common companion marker is the stronger fallback.
    def origins(result):
        return {key: value for key, value in model.lineage(result).items()
                if key.startswith(('__bounding_group__:', '__tile_run__:'))}
    image_origins, bound_origins = origins(image), origins(bounding)
    common = image_origins.keys() & bound_origins.keys()
    if common and any(image_origins[key] != bound_origins[key] for key in common):
        raise ValueError('图像与 Bounding 的分块来源或顺序不一致')
    present = [key for key in ('tile_group', '_tile_index', '_tile_count') if key in image]
    expected = dict(tile_group=bounds['tile_group'], _tile_index=bounds['tile_index'], _tile_count=bounds['tile_count'])
    if any(image[key] != expected[key] for key in present):
        raise ValueError('图像与 Bounding 来自不同的分块或顺序不一致')
    if len(present) != 3:
        run_confirmed = any(key.startswith('__tile_run__:') and value == bounds['tile_group']
                            for key, value in bound_origins.items())
        if not run_confirmed or any(image_origins.get(key) != value for key, value in bound_origins.items()):
            raise ValueError('图像缺少可确认的分块来源，请使用对应分块图像或保留来源的处理节点')


def _header(path, size, label):
    from PIL import Image
    from .bounding_nodes import _size
    try:
        with Image.open(path) as image:
            _size(image.size)
            # Our tiles are normalized PNGs. User-edited files may carry EXIF;
            # compare the oriented size without decoding the full image here.
            oriented = image.size[::-1] if image.getexif().get(274) in (5, 6, 7, 8) else image.size
            if oriented != size:
                raise ValueError(label + '尺寸与 Bounding 不一致，请勿改变分块尺寸')
            return 'A' in image.getbands() or 'transparency' in image.info
    except (OSError, TypeError) as error:
        raise ValueError(label + '文件不存在或无法读取') from error


def _axis_weights(length, start, full, feather):
    import numpy as np
    weights = np.ones(length, dtype=np.float32)
    if feather:
        if start > 0:
            weights = np.minimum(weights, (np.arange(length, dtype=np.float32) + .5) / feather)
        if start + length < full:
            weights = np.minimum(weights, (np.arange(length, 0, -1, dtype=np.float32) - .5) / feather)
    return weights


def _validate_coverage(bounds, width, height):
    """Rectangle sweep avoids allocating a full-size coverage map."""
    levels = sorted({0, height} | {b['y'] for b in bounds} | {b['y'] + b['height'] for b in bounds})
    for top, bottom in zip(levels, levels[1:]):
        intervals = sorted((b['x'], b['x'] + b['width']) for b in bounds
                           if b['y'] <= top and b['y'] + b['height'] >= bottom)
        end = 0
        for left, right in intervals:
            if left > end:
                break
            end = max(end, right)
        if end < width:
            raise ValueError('分块坐标没有覆盖完整原图，请连接完整的分块 Batch')


def _untile_group(images, bounding_items, bounds, params, directory, stop):
    import numpy as np
    from PIL import Image
    from .advanced_nodes import check_stop
    from .bounding_nodes import _open_image
    check_stop(stop)
    first = bounds[0]
    if len(images) != first['tile_count']:
        raise ValueError('需要完整分块；请分别用 List 转 Batch 节点连接图像和 Bounding，普通 List 只会逐项运行')
    if any((b['tile_group'], b['tile_count'], b['source_width'], b['source_height'], b['overlap']) !=
           (first['tile_group'], first['tile_count'], first['source_width'], first['source_height'], first['overlap']) for b in bounds):
        raise ValueError('Bounding 混入了其他图像、其他分块任务或不同的原图尺寸')
    if {b['tile_index'] for b in bounds} != set(range(len(bounds))):
        raise ValueError('分块序号缺失或重复')
    coordinates = {(b['x'], b['y'], b['width'], b['height']) for b in bounds}
    if len(coordinates) != len(bounds):
        raise ValueError('分块坐标重复')
    if sum(b['width'] * b['height'] for b in bounds) > _MAX_TILE_PIXELS:
        raise ValueError('分块含重叠的总面积超过 1.6 亿像素')
    width, height = first['source_width'], first['source_height']
    _validate_coverage(bounds, width, height)
    has_mask = [bool(item.get('mask_path')) for item in images]
    if any(has_mask) and not all(has_mask):
        raise ValueError('仅部分分块保留了附带遮罩，请统一保留或移除后再合并')
    rgba = False
    for image, bound_item, bound in zip(images, bounding_items, bounds):
        check_stop(stop)
        _same_origin(image, bound_item, bound)
        size = (bound['width'], bound['height'])
        rgba |= _header(image.get('path'), size, '分块图像')
        if all(has_mask):
            _header(image['mask_path'], size, '分块遮罩')
    channels, mode = (4, 'RGBA') if rgba else (3, 'RGB')
    feather = params.get('feather', True)
    if not isinstance(feather, bool):
        raise ValueError('重叠区域羽化必须是开关值')
    # Keep the main accumulation bands below 32 MiB. The output itself is
    # bounded by the existing 40 MP limit; no full-frame float buffer is used.
    # RGBA has a separate straight-RGB fallback for wholly transparent pixels.
    band_height = max(16, min(512, 32 * 1024 * 1024 // (width * (channels + (6 if rgba else 3)) * 4)))
    axes = [(_axis_weights(b['width'], b['x'], width, b['overlap'] if feather else 0),
             _axis_weights(b['height'], b['y'], height, b['overlap'] if feather else 0)) for b in bounds]
    with ExitStack() as stack:
        result = stack.enter_context(Image.new(mode, (width, height)))
        mask_result = stack.enter_context(Image.new('L', (width, height))) if all(has_mask) else None
        for top in range(0, height, band_height):
            check_stop(stop)
            bottom = min(height, top + band_height)
            accum = np.zeros((bottom - top, width, channels), dtype=np.float32)
            weights = np.zeros((bottom - top, width), dtype=np.float32)
            mask_accum = np.zeros_like(weights) if mask_result is not None else None
            hidden_rgb = np.zeros((bottom - top, width, 3), dtype=np.float32) if rgba else None
            for image, bound, (xx, yy) in zip(images, bounds, axes):
                start, end = max(top, bound['y']), min(bottom, bound['y'] + bound['height'])
                if start >= end:
                    continue
                check_stop(stop)
                local_top, local_bottom = start - bound['y'], end - bound['y']
                x, right = bound['x'], bound['x'] + bound['width']
                blend = yy[local_top:local_bottom, None] * xx[None, :]
                target = np.s_[start - top:end - top, x:right]
                with _open_image(image['path']) as tile:
                    with tile.crop((0, local_top, bound['width'], local_bottom)) as part:
                        with part.convert(mode) as normalized:
                            values = np.asarray(normalized, dtype=np.float32)
                            if rgba:
                                hidden_rgb[target] += values[:, :, :3] * blend[:, :, None]
                                # Premultiply before blending, so hidden colors
                                # cannot bleed into visible antialiased edges.
                                values[:, :, :3] *= values[:, :, 3:4] / 255.
                            accum[target] += values * blend[:, :, None]
                weights[target] += blend
                if mask_result is not None:
                    with _open_image(image['mask_path'], mask=True) as mask:
                        with mask.crop((0, local_top, bound['width'], local_bottom)) as part:
                            mask_accum[target] += np.asarray(part, dtype=np.float32) * blend
            if np.any(weights <= 0):
                raise ValueError('分块未完整覆盖原图')
            if rgba:
                alpha_weights = accum[:, :, 3:4] / 255.
                np.divide(accum[:, :, :3], alpha_weights, out=accum[:, :, :3], where=alpha_weights > 0)
                hidden_rgb /= weights[:, :, None]
                transparent = alpha_weights[:, :, 0] == 0
                accum[:, :, :3][transparent] = hidden_rgb[transparent]
                accum[:, :, 3] /= weights
            else:
                accum /= weights[:, :, None]
            np.rint(accum, out=accum)
            np.clip(accum, 0, 255, out=accum)
            with Image.fromarray(accum.astype(np.uint8)) as part:
                result.paste(part, (0, top))
            if mask_result is not None:
                mask_accum /= weights
                np.rint(mask_accum, out=mask_accum)
                np.clip(mask_accum, 0, 255, out=mask_accum)
                with Image.fromarray(mask_accum.astype(np.uint8)) as part:
                    mask_result.paste(part, (0, top))
        directory.mkdir(parents=True, exist_ok=True)
        check_stop(stop)
        target = directory / 'merged.png'
        result.save(target)
        item = dict(type='image', path=str(target))
        if mask_result is not None:
            check_stop(stop)
            mask_target = directory / 'merged_mask.png'
            mask_result.save(mask_target)
            item.update(mask_path=str(mask_target), _processed_mask=True, _mask_nonempty=bool(mask_result.getbbox()))
    check_stop(stop)
    return [item]


def _untile(batch, params, directory, stop):
    from . import model
    from .advanced_nodes import check_stop
    images = _members(batch.get('images'), 'image', '图像 Batch')
    bounding_items = _members(batch.get('bounding'), 'bounding', 'Bounding Batch')
    if len(images) != len(bounding_items):
        raise ValueError('图像与 Bounding 数量不一致；请分别将完整列表转为 Batch')
    bounds = [_tile_bounds(value) for value in bounding_items]
    if sum(bound['width'] * bound['height'] for bound in bounds) > _MAX_TILE_PIXELS:
        raise ValueError('此 Batch 的分块总面积超过 1.6 亿像素，请减少输入图像或重叠')
    groups, identities = {}, set()
    unused = set(range(len(images)))
    for bounding_item, bound in zip(bounding_items, bounds):
        check_stop(stop)
        identity = (bound['tile_group'], bound['tile_index'])
        if identity in identities:
            raise ValueError('分块 Bounding 序号重复')
        identities.add(identity)
        matching = []
        for index in sorted(unused):
            try:
                _same_origin(images[index], bounding_item, bound)
            except ValueError:
                continue
            matching.append(index)
        if not matching:
            raise ValueError('找不到来源对应的分块图像；请检查两路 Batch 的来源、完整性和跨轮次结果')
        if len(matching) > 1:
            raise ValueError('同一分块来源对应多张图像，无法唯一配对，请移除重复项')
        index = matching[0]
        unused.remove(index)
        groups.setdefault(bound['tile_group'], []).append((images[index], bounding_item, bound))
    output = []
    for index, members in enumerate(groups.values()):
        check_stop(stop)
        group_images, group_items, group_bounds = map(list, zip(*members))
        results = _untile_group(group_images, group_items, group_bounds, params, directory / str(index), stop)
        # The outer Batch has lost per-source ancestry by design. Restore only
        # ancestry common to this source's tiles, never a particular tile index.
        common = model.lineage(group_images[0])
        for image in group_images[1:]:
            origins = model.lineage(image)
            common = {key: value for key, value in common.items() if origins.get(key) == value}
        for result in results:
            result['_tile_source_lineage'] = dict(common)
        output.extend(results)
    return output


def operation(node, batch, params, directory, stop):
    from .advanced_nodes import check_stop
    check_stop(stop)
    if node['kind'] not in KINDS:
        raise ValueError('不支持的图像分块节点')
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    committed = directory / ('tiles-' + uuid.uuid4().hex)
    try:
        # A failed later source or canceled save must not leave earlier merged
        # outputs looking usable. Only publish the invocation after all succeed.
        with tempfile.TemporaryDirectory(prefix='.tile-work-', dir=directory) as temporary:
            working = Path(temporary)
            result = (_tile(batch, params, working, stop) if node['kind'] == 'image_tile'
                      else _untile(batch, params, working, stop))
            check_stop(stop)
            os.replace(working, committed)
            for item in result:
                for key in ('path', 'mask_path'):
                    if item.get(key):item[key] = str(committed / Path(item[key]).relative_to(working))
            check_stop(stop)
            return result
    except BaseException:
        # This path is unique to this call, never a preexisting output directory.
        if committed.parent == directory and committed.name.startswith('tiles-') and committed.exists():
            shutil.rmtree(committed)
        raise
