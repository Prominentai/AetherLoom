"""Minimal vision model caller: send image + text, get text back."""
import base64
from pathlib import Path
from typing import Optional
import io
from .provider_client import ProviderAPIError, complete_text

# Pillow is optional; if unavailable we fall back to raw bytes read
try:
    from PIL import Image
except Exception:
    Image = None


def _encode_image_data(path):
    try:
        if Image is not None:
            with Image.open(path) as source:
                width, height = source.size
                scale = min(1.0, (1_000_000 / float(width * height)) ** 0.5)
                with source.convert('RGB') as converted:
                    converted.thumbnail((max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS)
                    buffer = io.BytesIO()
                    converted.save(buffer, format='PNG')
                    return 'image/png', base64.b64encode(buffer.getvalue()).decode('ascii')
        data = Path(path).read_bytes()
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            mime = 'image/png'
        elif data.startswith(b'\xff\xd8\xff'):
            mime = 'image/jpeg'
        elif data.startswith((b'GIF87a', b'GIF89a')):
            mime = 'image/gif'
        elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
            mime = 'image/webp'
        else:
            raise ValueError
        return mime, base64.b64encode(data).decode('ascii')
    except Exception:
        raise ProviderAPIError('图片无法读取或格式不受支持。', status='invalid_image') from None


def _encode_image(path: str) -> str:
    return _encode_image_data(path)[1]


def call_vision(
    api_url: str,
    api_key: str,
    model: str,
    image_path: str,
    user_text: str,
    timeout: int = 60,
    provider=None,
    max_tokens=None,
) -> str:
    return complete_text(api_url, api_key, model, '', user_text, timeout=timeout,
                         provider=provider, max_tokens=max_tokens, image=_encode_image_data(image_path))
