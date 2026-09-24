"""Local image preparation and focused inpainting around the public API."""
import copy
import io
import shutil
from pathlib import Path
from PIL import Image, ImageOps, PngImagePlugin
from aetherloom_core import mask_assets


def prepare_editor_input(original, draft_mask, input_dir, temporary):
    """Keep NovelAI's image and mask separate, including the image's own alpha.

    RH transports its mask in image alpha. Reusing that transport here and
    dropping alpha also exposed hidden RGB in transparent input pixels.
    """
    with Image.open(original) as source:
        if source.width * source.height > mask_assets.MAX_PIXELS:
            raise ValueError('输入图像超过 3200 万像素')
        if getattr(source, 'n_frames', 1) != 1:
            raise ValueError('请先导出需要使用的静态图像帧')
        mode = 'RGBA' if 'A' in source.getbands() or 'transparency' in source.info else 'RGB'
        base = ImageOps.exif_transpose(source).convert(mode)
    mask = paint = None
    try:
        orientation = int(draft_mask.get('orientation', 0))
        if not 0 <= orientation <= 7:
            raise ValueError('图像方向设置无效')
        if orientation:
            rotated = base.transpose(Image.Transpose(orientation - 1))
            base.close()
            base = rotated
        mask = mask_assets.read(draft_mask, base.size)
        # The durable editor asset follows the existing inverse-alpha format;
        # it is never used as the image uploaded to NovelAI.
        with base.convert('RGB').convert('RGBA') as asset:
            asset.putalpha(ImageOps.invert(mask))
            mask_asset = mask_assets.store_asset(asset, Path(input_dir) / 'masks')
        persistent = {key: value for key, value in draft_mask.items() if key not in ('png', 'paint_png')}
        persistent.update(version=2, encoding='rgba-alpha', path=mask_asset,
                          sha256=Path(mask_asset).stem, width=base.width, height=base.height)
        paint = mask_assets.read_paint(draft_mask, base.size)
        if paint is not None:
            persistent['paint_path'] = mask_assets.store_asset(paint, Path(input_dir) / 'paintings')
            persistent['paint_sha256'] = Path(persistent['paint_path']).stem
            with base.convert('RGBA') as rgba:
                composited = Image.alpha_composite(rgba, paint).convert(mode)
            base.close()
            base = composited
        image_path = mask_assets.atomic_png(base, Path(temporary) / 'novelai-input.png')
        mask_path = mask_assets.atomic_png(mask, Path(temporary) / 'novelai-mask.png')
        return image_path, mask_path, persistent
    finally:
        base.close()
        if mask is not None:
            mask.close()
        if paint is not None:
            paint.close()


def prepare(snapshot, draft_mask, input_dir, temporary):
    options = copy.deepcopy(snapshot)
    action = options.get('action', 'generate')
    needs_image = action in ('img2img', 'infill', 'augment', 'upscale')
    original = options.get('image_path', '') if needs_image else ''
    persistent_mask = None
    if original:
        if draft_mask and mask_assets.matches(draft_mask, original):
            options['image_path'], options['mask_path'], persistent_mask = prepare_editor_input(
                original, draft_mask, input_dir, temporary)
        elif options.get('action') == 'infill' and not options.get('mask_path'):
            with Image.open(original) as source:
                if source.width * source.height > mask_assets.MAX_PIXELS:
                    raise ValueError('输入图像超过 3200 万像素')
                if getattr(source, 'n_frames', 1) != 1:
                    raise ValueError('请先导出需要使用的静态图像帧')
                with ImageOps.exif_transpose(source) as image, mask_assets.image_mask(image) as mask:
                    if not mask.getbbox():
                        raise ValueError('请先绘制或导入遮罩，再进行局部重绘。')
                    options['mask_path'] = mask_assets.atomic_png(mask, Path(temporary) / 'novelai-mask.png')
            # Infer an edit mask from transparency without erasing the original
            # transparency or exposing RGB hidden below it in the upload image.
    # Freeze actual file contents as well as Qt settings before any paid request.
    paths = [(options, 'image_path')] if needs_image else []
    if action == 'infill':
        paths.append((options, 'mask_path'))
    if action in ('generate', 'img2img', 'infill'):
        paths.extend((reference, 'path') for reference in options.get('references', [])
                     if reference.get('enabled', True))
    for index, (record, key) in enumerate(paths):
        source = record.get(key)
        if not source:
            continue
        source = Path(source)
        if source.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('输入图像超过 32 MB')
        with Image.open(source) as image:
            if image.width * image.height > mask_assets.MAX_PIXELS:
                raise ValueError('输入图像超过 3200 万像素')
            if getattr(image, 'n_frames', 1) != 1:
                raise ValueError('请先导出需要使用的静态图像帧')
            image.verify()
        destination = Path(temporary) / f'input-{index}{source.suffix}'
        shutil.copyfile(source, destination)
        record[key] = str(destination)
    focused = None
    if options.get('focused') and options.get('action') == 'infill':
        # Validate the full inputs before cropping: cropping both to one box
        # would otherwise hide a mismatched mask from the API preflight.
        from .client import _open_image
        with _open_image(options['image_path']) as image:
            base = image.convert('RGBA' if 'A' in image.getbands() or 'transparency' in image.info else 'RGB')
        mask = None
        try:
            with _open_image(options['mask_path']) as image:
                if image.size != base.size:
                    raise ValueError('蒙版尺寸必须与原输入图像一致')
                if image.mode not in ('1', 'L', 'RGB'):
                    raise ValueError('蒙版须为灰度图（白色编辑、黑色保留），请重新保存蒙版')
                mask = image.convert('L')
            box = mask.getbbox()
            if not box:
                raise ValueError('遮罩为空，无法进行局部放大重绘。')
            padding = int(options.get('focus_padding', 64))
            if not 0 <= padding <= 2048:
                raise ValueError('聚焦重绘边距必须为 0 到 2048 像素')
            box = (max(0, box[0] - padding), max(0, box[1] - padding),
                   min(base.width, box[2] + padding), min(base.height, box[3] + padding))
            with base.crop(box) as crop:
                options['image_path'] = mask_assets.atomic_png(crop, Path(temporary) / 'focus-base.png')
            with mask.crop(box) as crop:
                options['mask_path'] = mask_assets.atomic_png(crop, Path(temporary) / 'focus-mask.png')
            focused = (base, mask, box)
        except BaseException:
            base.close()
            if mask is not None:
                mask.close()
            raise
    return options, persistent_mask, focused


def _compose_focused_patch(patch, original, mask):
    if patch.mode != 'RGBA' or (patch.getextrema()[3] == (255, 255)
                                and original.getextrema()[3] == (255, 255)):
        return Image.composite(patch, original, mask)
    import numpy as np
    # Interpolate premultiplied colors, then restore straight RGBA. Keeping
    # integer alpha weights avoids quantizing low-alpha colors to 8-bit RGBa.
    composed = Image.new('RGBA', patch.size)
    rows = max(1, min(256, 262144 // patch.width))
    for top in range(0, patch.height, rows):
        region = (0, top, patch.width, min(top + rows, patch.height))
        with patch.crop(region) as part, original.crop(region) as old, mask.crop(region) as selection:
            incoming = np.asarray(part, dtype=np.uint32)
            previous = np.asarray(old, dtype=np.uint32)
            weight = np.asarray(selection, dtype=np.uint32)[..., None]
        incoming_alpha = incoming[..., 3:] * weight
        previous_alpha = previous[..., 3:] * (255 - weight)
        alpha = incoming_alpha + previous_alpha
        colors = incoming[..., :3] * incoming_alpha + previous[..., :3] * previous_alpha
        colors = (colors + alpha // 2) // np.maximum(alpha, 1)
        rgba = np.concatenate((colors, (alpha + 127) // 255), axis=2).astype(np.uint8)
        rgba[rgba[..., 3] == 0, :3] = 0
        with Image.fromarray(rgba) as part:
            composed.paste(part, (0, top))
    return composed


def compose_focused(results, context):
    if context is None:
        return results
    base, mask, box = context
    restored = []
    for result in results:
        with Image.open(io.BytesIO(result['bytes'])) as image:
            info = dict(image.info)
            patch = image.convert(base.mode).resize((box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS)
        image = base.copy()
        original = base.crop(box)
        with mask.crop(box) as selection, _compose_focused_patch(patch, original, selection) as composed:
            image.paste(composed, box[:2])
        metadata = PngImagePlugin.PngInfo()
        for key, value in info.items():
            if isinstance(value, str) and len(value) <= 1024 * 1024:
                metadata.add_text(key, value)
        stream = io.BytesIO()
        image.save(stream, format='PNG', pnginfo=metadata)
        restored.append(dict(result, bytes=stream.getvalue(), mime='image/png'))
    return restored
