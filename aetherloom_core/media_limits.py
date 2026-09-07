"""Bound decoded frames used by local thumbnails and quick previews."""

from pathlib import Path

from PIL import Image, ImageOps


MAX_DECODE_PIXELS = 32_000_000
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.webm', '.m4v', '.wmv', '.flv'}


class MediaTooLargeError(ValueError):
    """The source would require an oversized in-memory preview frame."""


def check_dimensions(width, height, *, max_pixels=MAX_DECODE_PIXELS):
    width, height = int(width), int(height)
    if width <= 0 or height <= 0:
        raise ValueError('无法读取媒体尺寸，可尝试系统打开。')
    if width * height > max_pixels:
        raise MediaTooLargeError(
            f'此文件解码尺寸较大（{width} × {height}），已跳过预览以节省内存。可使用「系统打开」。')
    return width, height


def load_media_frame(path, target_size, *, max_pixels=MAX_DECODE_PIXELS):
    """Return (owned RGB PIL image, source size, video flag), bounded before decode.

    JPEG's native draft reduction runs before checking its actual decode size.
    PNG/GIF and other formats without that reduction are checked at full size.
    Callers must close the returned image after converting or encoding it.
    """
    target = check_dimensions(*target_size, max_pixels=max_pixels)
    if Path(path).suffix.lower() in VIDEO_EXTENSIONS:
        import cv2
        capture = cv2.VideoCapture(str(path))
        try:
            original = check_dimensions(capture.get(cv2.CAP_PROP_FRAME_WIDTH),
                                        capture.get(cv2.CAP_PROP_FRAME_HEIGHT), max_pixels=max_pixels)
            success, array = capture.read()
            if not success or array is None:
                raise ValueError('无法读取视频首帧，可尝试系统打开。')
            height, width = array.shape[:2]
            check_dimensions(width, height, max_pixels=max_pixels)
            scale = min(1.0, target[0] / width, target[1] / height)
            if scale < 1.0:
                array = cv2.resize(array, (max(1, int(width * scale)), max(1, int(height * scale))),
                                   interpolation=cv2.INTER_AREA)
            return Image.fromarray(cv2.cvtColor(array, cv2.COLOR_BGR2RGB)), original, True
        finally:
            capture.release()
    try:
        with Image.open(path) as source:
            original = source.size
            source.draft('RGB', target)
            check_dimensions(*source.size, max_pixels=max_pixels)
            source.thumbnail(target, Image.Resampling.LANCZOS)
            # Orientation is applied after reduction, so transposition cannot
            # introduce a second full-resolution allocation.
            with ImageOps.exif_transpose(source) as oriented:
                oriented.thumbnail(target, Image.Resampling.LANCZOS)
                return oriented.convert('RGB'), original, False
    except Image.DecompressionBombError as exc:
        raise MediaTooLargeError('此图片尺寸过大，已跳过预览以节省内存。可使用「系统打开」。') from exc
