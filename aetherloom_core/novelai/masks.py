"""NovelAI inpainting mask geometry, shared by submission and canvas preflight."""

from PIL import Image


def quantize_mask(mask, target_size):
    """Return the binary 1/8-size *intermediate*, not the HTTP upload image.

    The official image frontend downsamples with nearest-neighbour sampling,
    thresholds brush alpha strictly above 155, then expands to the requested
    width/height before uploading. Our editor stores brush coverage as L.
    """
    width, height = target_size
    if width < 8 or height < 8:
        raise ValueError('蒙版目标尺寸不能小于 8 像素')
    import numpy as np
    with mask.convert('L') as coverage:
        # Use explicit pixel centres, as the website does. Pillow's cumulative
        # resize coordinates can select the preceding pixel at exact boundaries
        # for noninteger scale ratios (e.g. 1280 -> 96).
        xs = np.minimum(coverage.width - 1,
                        ((np.arange(width // 8) + .5) * coverage.width / (width // 8)).astype(np.intp))
        ys = np.minimum(coverage.height - 1,
                        ((np.arange(height // 8) + .5) * coverage.height / (height // 8)).astype(np.intp))
        values = np.asarray(coverage)[ys[:, None], xs]
        binary = Image.fromarray((values > 155).astype(np.uint8) * 255)
    if binary.getextrema()[1] == 0:
        binary.close()
        raise ValueError('蒙版没有可编辑区域')
    return binary


def upload_mask(mask, target_size):
    """Full-size opaque black/white RGBA PNG pixels, matching the website.

    Its transparent brush intermediate is flattened on black by the common
    request builder. Sending that intermediate at 1/8 size skips this step.
    """
    with quantize_mask(mask, target_size) as binary:
        with binary.resize(tuple(target_size), Image.Resampling.NEAREST) as full:
            return full.convert('RGBA')
