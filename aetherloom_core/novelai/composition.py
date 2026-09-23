"""Local image preparation and focused inpainting around the public API."""
import copy
import io
import shutil
from pathlib import Path
from PIL import Image, PngImagePlugin
from aetherloom_core import mask_assets


def prepare(snapshot, draft_mask, input_dir, temporary):
    options = copy.deepcopy(snapshot)
    action = options.get('action', 'generate')
    needs_image = action in ('img2img', 'infill', 'augment', 'upscale')
    original = options.get('image_path', '') if needs_image else ''
    persistent_mask = None
    if original:
        if draft_mask and mask_assets.matches(draft_mask, original):
            path, assets = mask_assets.input_image(original, draft_mask, input_dir, temporary)
            options['image_path'] = path
            options['mask_path'] = assets['mask_path']
            persistent_mask = {key: value for key, value in draft_mask.items() if key not in ('png', 'paint_png')}
            persistent_mask['path'] = assets.get('mask_asset_path', persistent_mask.get('path', ''))
            if assets.get('paint_path'):
                persistent_mask['paint_path'] = assets['paint_path']
        elif options.get('action') == 'infill' and not options.get('mask_path'):
            path, assets = mask_assets.input_image(original, None, input_dir, temporary)
            if not assets.get('_mask_nonempty'):
                raise ValueError('请先绘制或导入遮罩，再进行局部重绘。')
            options['image_path'], options['mask_path'] = path, assets['mask_path']
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
        image.paste(Image.composite(patch, original, mask.crop(box)), box[:2])
        metadata = PngImagePlugin.PngInfo()
        for key, value in info.items():
            if isinstance(value, str) and len(value) <= 1024 * 1024:
                metadata.add_text(key, value)
        stream = io.BytesIO()
        image.save(stream, format='PNG', pnginfo=metadata)
        restored.append(dict(result, bytes=stream.getvalue(), mime='image/png'))
    return restored
