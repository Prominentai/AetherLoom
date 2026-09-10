"""Validate an untrusted drag payload before creating a durable input file."""
import io
import os
from pathlib import Path
import re
import subprocess
import uuid
from PIL import Image
from .mask_assets import MAX_PIXELS

IMAGE_FORMATS={'PNG':'.png','JPEG':'.jpg','WEBP':'.webp','BMP':'.bmp','TIFF':'.tiff',
               'GIF':'.gif','AVIF':'.avif','HEIF':'.heic'}


def validate(data,kind='image',name=''):
    from .canvas.model import MEDIA_SUFFIXES
    allowed=set(MEDIA_SUFFIXES) if kind in ('any','file') else {kind}
    if 'image' in allowed:
        try:
            with Image.open(io.BytesIO(data)) as image:
                suffix=IMAGE_FORMATS.get(image.format)
                if not suffix:raise ValueError('图像格式不在此输入支持的范围内')
                if image.width*image.height>MAX_PIXELS:raise ValueError('图像超过 3200 万像素')
                image.verify()
            with Image.open(io.BytesIO(data)) as image:image.load()
            return suffix
        except (OSError,SyntaxError):
            if allowed=={'image'}:raise ValueError('拖入内容不是有效的受支持图像')
    if not allowed & {'video','audio'}:raise ValueError('文件格式不符合此输入要求')
    # Probe from memory: rejected input never appears in the configured input dir.
    import imageio_ffmpeg
    process=subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-i','pipe:0',
        '-t','0','-f','null','-'],input=data,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
        timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    report=process.stderr.decode('utf8','replace')
    header=report.split('Output #',1)[0]
    if process.returncode!=0:raise ValueError('无法读取拖入的媒体文件')
    video=bool(re.search(r'Stream #\S+.*Video:',header));audio=bool(re.search(r'Stream #\S+.*Audio:',header))
    actual='video' if video else 'audio' if audio else ''
    if actual not in allowed:raise ValueError('拖入媒体类型不符合此输入要求')
    detected=re.search(r'Input #0, (.*?), from',header)
    fmt=detected.group(1) if detected else ''
    candidates=(['.mp4','.mov','.m4v'] if 'mp4' in fmt else ['.mkv','.webm'] if 'matroska' in fmt else
                ['.avi'] if fmt=='avi' else ['.wav'] if fmt=='wav' else ['.mp3'] if fmt=='mp3' else
                ['.flac'] if fmt=='flac' else ['.ogg','.opus'] if fmt=='ogg' else ['.aac'] if fmt=='aac' else [])
    if actual=='audio' and 'mp4' in fmt:candidates=['.m4a']
    candidates=[suffix for suffix in candidates if suffix in MEDIA_SUFFIXES[actual]]
    if not candidates:raise ValueError('媒体封装格式不在此输入支持的范围内')
    suffix=Path(name).suffix.lower()
    return suffix if suffix in candidates else candidates[0]


def save(data,directory,kind='image',name=''):
    suffix=validate(data,kind,name)
    path=Path(directory)/(uuid.uuid4().hex+suffix)
    path.parent.mkdir(parents=True,exist_ok=True)
    part=path.with_suffix('.part')
    try:
        with part.open('xb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(part,path)
    finally:part.unlink(missing_ok=True)
    return str(path)
