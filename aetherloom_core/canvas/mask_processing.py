"""Grayscale mask morphology and canvas-edge feathering; no thresholding."""
from PIL import Image, ImageFilter


def grow(mask, amount, tapered, stop):
    from .advanced_nodes import check_stop
    import cv2
    import numpy as np
    amount = int(amount)
    check_stop(stop)
    if not amount:return mask.copy()
    pixels = np.array(mask, dtype=np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_CROSS if tapered else cv2.MORPH_RECT, (3, 3))
    operation = cv2.dilate if amount > 0 else cv2.erode
    # Small chunks let Stop interrupt a large grow without discarding soft values.
    remaining = abs(amount)
    while remaining:
        check_stop(stop)
        steps = min(4, remaining)
        pixels = operation(pixels, kernel, iterations=steps, borderType=cv2.BORDER_REPLICATE)
        remaining -= steps
    return Image.fromarray(pixels)


def grow_blur(mask, amount, radius, tapered, stop):
    from .advanced_nodes import check_stop
    with grow(mask, amount, tapered, stop) as expanded:
        check_stop(stop)
        return expanded.filter(ImageFilter.GaussianBlur(float(radius)))


def feather_edges(mask, params, stop):
    from .advanced_nodes import check_stop
    import numpy as np
    def weights(length, before, after):
        values = np.ones(length, dtype=np.float32)
        before, after = min(length, int(before)), min(length, int(after))
        if before:values[:before] *= np.arange(1, before + 1, dtype=np.float32) / before
        if after:values[-after:] *= np.arange(after, 0, -1, dtype=np.float32) / after
        return values
    horizontal = weights(mask.width, params['left'], params['right'])
    vertical = weights(mask.height, params['top'], params['bottom'])
    pixels = np.array(mask, dtype=np.uint8)
    for start in range(0, mask.height, 128):
        check_stop(stop)
        block = pixels[start:start + 128].astype(np.float32)
        block *= horizontal[None, :]
        block *= vertical[start:start + 128, None]
        pixels[start:start + 128] = np.rint(block).astype(np.uint8)
    return Image.fromarray(pixels)
