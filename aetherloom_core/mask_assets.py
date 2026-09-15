"""ComfyUI-style RGBA mask assets, soft MASK values and RH upload images."""
import base64
import hashlib
import io
import os
from pathlib import Path
import uuid
from PIL import Image, ImageOps

MAX_PIXELS = 32_000_000
MAX_BYTES = 32 * 1024 * 1024


def binary(image):
    return image.convert('L').point(lambda value: 255 if value >= 128 else 0, mode='1')


def import_mask(path, size, channel='auto'):
    if Path(path).stat().st_size > MAX_BYTES:raise ValueError('遮罩文件超过 32 MB')
    with Image.open(path) as source:
        if source.width * source.height > MAX_PIXELS:raise ValueError('遮罩超过 3200 万像素')
        has_alpha = 'A' in source.getbands() or 'transparency' in source.info
        image = ImageOps.exif_transpose(source).convert('RGBA')
        alpha = image.getchannel('A')
        if channel == 'alpha' or (channel == 'auto' and has_alpha):
            mask = ImageOps.invert(alpha)
        elif channel in ('red', 'green', 'blue'):
            mask = image.getchannel({'red':'R', 'green':'G', 'blue':'B'}[channel])
        else:mask = image.convert('RGB').convert('L')
        return resize_mask(mask, size)


def resize_mask(image, size):
    if min(size) < 1 or size[0] * size[1] > MAX_PIXELS:raise ValueError('遮罩目标尺寸无效或超过像素限制')
    mask = image.convert('L')
    return mask.resize(size, Image.Resampling.BILINEAR) if mask.size != size else mask


def image_mask(image):
    """MASK is inverse alpha; an opaque image has an empty, same-size mask."""
    return ImageOps.invert(image.convert('RGBA').getchannel('A'))


def draft(image, source, *, rgb=None):
    if rgb is None:
        with Image.open(source) as original:
            if original.width * original.height > MAX_PIXELS:raise ValueError('图像超过 3200 万像素')
            rgb = ImageOps.exif_transpose(original).convert('RGB')
    asset = rgb.convert('RGBA')
    asset.putalpha(ImageOps.invert(resize_mask(image, asset.size)))
    stream = io.BytesIO()
    asset.save(stream, format='PNG')
    data = stream.getvalue()
    if len(data) > MAX_BYTES:raise ValueError('遮罩图像数据超过 32 MB，请先缩小图像')
    return {'version': 2, 'encoding': 'rgba-alpha', 'source': os.path.abspath(source), 'width': asset.width, 'height': asset.height,
            'sha256': hashlib.sha256(data).hexdigest(), 'png': base64.b64encode(data).decode('ascii')}


def matches(config, source):
    return bool(config and os.path.normcase(os.path.abspath(config.get('source', ''))) ==
                os.path.normcase(os.path.abspath(source)))


def read(config, size=None):
    encoded = config.get('png', '')
    if len(encoded) > MAX_BYTES * 2:raise ValueError('遮罩数据过大')
    data = base64.b64decode(encoded, validate=True) if encoded else None
    if data is not None and len(data) > MAX_BYTES:raise ValueError('遮罩数据过大')
    if config.get('path'):
        try:
            path=Path(config['path'])
            if path.stat().st_size<=MAX_BYTES:
                saved=path.read_bytes()
                if not config.get('sha256') or hashlib.sha256(saved).hexdigest()==config['sha256']:data=saved
                elif data is None:raise ValueError('遮罩内容校验失败')
            elif data is None:raise ValueError('遮罩文件超过 32 MB')
        except OSError:pass
    if data is not None and config.get('sha256') and hashlib.sha256(data).hexdigest() != config['sha256']:
        raise ValueError('遮罩内容校验失败')
    if data is None:raise FileNotFoundError('遮罩文件不存在，且没有可恢复的编辑数据')
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > MAX_PIXELS:raise ValueError('遮罩超过 3200 万像素')
        result = image_mask(image) if config.get('encoding') == 'rgba-alpha' else image.convert('L')
    if size and result.size != size:result = resize_mask(result, size)
    return result


def atomic_png(image, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name('.' + uuid.uuid4().hex + '.part')
    try:
        with part.open('xb') as stream:
            image.save(stream, format='PNG');stream.flush();os.fsync(stream.fileno())
        os.replace(part, path)
    finally:part.unlink(missing_ok=True)
    return str(path)


def resize_paint(image,size):
    image=image.convert('RGBA')
    # Resample premultiplied colors so transparent pixels cannot add fringes.
    return image.convert('RGBa').resize(size,Image.Resampling.LANCZOS).convert('RGBA') if image.size!=size else image


def import_paint(path,size):
    if Path(path).stat().st_size>MAX_BYTES:raise ValueError('绘画图层文件超过 32 MB')
    with Image.open(path) as image:
        if image.width*image.height>MAX_PIXELS:raise ValueError('绘画图层超过 3200 万像素')
        if getattr(image,'n_frames',1)>1:raise ValueError('请先导出要使用的静态图像帧')
        return resize_paint(ImageOps.exif_transpose(image),size)


def read_paint(config,size):
    encoded=config.get('paint_png','')
    if len(encoded)>MAX_BYTES*2:raise ValueError('绘画图层数据过大')
    data=base64.b64decode(encoded,validate=True) if encoded else None
    path=config.get('paint_path')
    if path:
        try:
            if Path(path).stat().st_size<=MAX_BYTES:
                saved=Path(path).read_bytes()
                if not config.get('paint_sha256') or hashlib.sha256(saved).hexdigest()==config['paint_sha256']:data=saved
        except OSError:pass
    if data is None:
        if path:raise FileNotFoundError('绘画图层文件不存在：'+str(path))
        return None
    if config.get('paint_sha256') and hashlib.sha256(data).hexdigest()!=config['paint_sha256']:
        raise ValueError('绘画图层内容校验失败')
    with Image.open(io.BytesIO(data)) as image:
        if image.width*image.height>MAX_PIXELS:raise ValueError('绘画图层超过像素限制')
        result=image.convert('RGBA')
    return resize_paint(result,size)


def store_asset(image,directory):
    buffer=io.BytesIO();image.save(buffer,format='PNG')
    identity=hashlib.sha256(buffer.getvalue()).hexdigest()
    path=Path(directory)/(identity+'.png')
    try:unchanged=hashlib.sha256(path.read_bytes()).hexdigest()==identity
    except OSError:unchanged=False
    if not unchanged:atomic_png(image,path)
    return str(path)


def materialize(source, config, input_dir, temporary_dir, *, assets=None):
    """Return RH RGBA upload image and a persistent RGB+inverse-MASK PNG."""
    with Image.open(source) as image:
        if image.width * image.height > MAX_PIXELS:raise ValueError('图像超过 3200 万像素')
        rgb = ImageOps.exif_transpose(image).convert('RGB')
    orientation=int(config.get('orientation',0))
    if not 0<=orientation<=7:raise ValueError('图像方向设置无效')
    if orientation:rgb=rgb.transpose(Image.Transpose(orientation-1))
    mask = read(config, rgb.size)
    base = rgb.convert('RGBA')
    base.putalpha(ImageOps.invert(mask))
    mask_path = store_asset(base, Path(input_dir) / 'masks')
    paint=read_paint(config,rgb.size)
    if paint is not None:
        paint_path=store_asset(paint,Path(input_dir)/'paintings')
        paint_temp_path=store_asset(paint,Path(temporary_dir)/'paintings')
        composite=Image.alpha_composite(rgb.convert('RGBA'),paint)
        composite_path=store_asset(composite,Path(temporary_dir)/'composites')
        rgb=composite.convert('RGB')
        if assets is not None:assets.update(paint_path=paint_path,paint_temp_path=paint_temp_path,composite_path=composite_path)
    rgb.putalpha(ImageOps.invert(mask.convert('L')))
    transport = Path(temporary_dir) / uuid.uuid4().hex[:12] / (Path(source).stem[:80] + '.png')
    return atomic_png(rgb, transport), str(mask_path)


def input_image(source, config, input_dir, temporary_dir):
    """Canvas IMAGE and MASK are separate; only editing assets are durable."""
    assets = {}
    path = source
    if config:
        path, mask_asset = materialize(source, config, input_dir, temporary_dir, assets=assets)
        assets['mask_asset_path'] = mask_asset
    with Image.open(path) as original:
        if original.width * original.height > MAX_PIXELS:raise ValueError('图像超过 3200 万像素')
        if getattr(original, 'n_frames', 1) > 1:
            if config:raise ValueError('请先导出静态图像帧再编辑遮罩')
            # Preserve animated inputs; one same-size empty mask accompanies the file.
            mask = Image.new('L', original.size, 0)
            rgb = None
        else:
            image = ImageOps.exif_transpose(original)
            mask = image_mask(image)
            rgb = image.convert('RGB') if config or 'A' in image.getbands() or 'transparency' in image.info else None
    directory = Path(temporary_dir) / 'image_input'
    if rgb is not None:
        path = atomic_png(rgb, directory / (Path(source).stem[:80] + '.png'))
        assets['source_path'] = source
    mask_path = atomic_png(mask, directory / (Path(source).stem[:80] + '_mask.png'))
    assets.update(mask_path=mask_path, _processed_mask=True, _mask_nonempty=mask.getbbox() is not None)
    return path, assets


def frozen_mask_reference(source, path):
    """A gray runtime MASK can be carried into an immutable RH submission."""
    return {'version': 1, 'source': os.path.abspath(source), 'path': path,
            'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()}


def asset_reference(config, path):
    data = Path(path).read_bytes()
    return dict(config, version=2, encoding='rgba-alpha', path=path,
                sha256=hashlib.sha256(data).hexdigest(), png=base64.b64encode(data).decode('ascii'))
