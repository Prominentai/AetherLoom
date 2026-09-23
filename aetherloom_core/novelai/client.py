"""Bounded, cancellable NovelAI client. A POST is attempted exactly once.

No account credentials are read here. The caller passes a persistent API token
only at request time. Vibe encoding happens only inside run_image and is cached.
"""
from __future__ import annotations

import base64
import copy
from collections import OrderedDict
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import tempfile
import time
import zipfile

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

from .catalog import capabilities, validate_options, active_references, director_size, DATASET_MODES
from .prompts import build_prompts, resolve_options
from . import task_records

IMAGE_API = "https://image.novelai.net"
# Persistent image tokens require the image service, including account queries.
ACCOUNT_API = IMAGE_API
MAX_INPUT = 32 * 1024 * 1024
MAX_IMAGE = 64 * 1024 * 1024
MAX_RESPONSE = 128 * 1024 * 1024
MAX_PIXELS = 16_777_216
# Official standalone-upscaler model; live 512x512 -> 1024x1024 verified 2026-09-22.
UPSCALE_MODEL = "nai-diffusion-5-curated"
UPSCALE_FACTOR = 2
MAX_VIBE = 16 * 1024 * 1024
MAX_EVENT = 90 * 1024 * 1024
_VIBE_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_VIBE_ENCODE_LOCK = threading.Lock()


class NovelAIError(RuntimeError):
    """Safe user-facing failure. Requests and credentials are never attached."""

    def __init__(self, message, *, status_code=None, rejection_kind=None):
        super().__init__(message)
        self.status_code = status_code
        # Only _request may mark a response as a confirmed pre-acceptance rejection.
        self.rejection_kind = rejection_kind


class Cancelled(NovelAIError):
    pass


def _cancelled(stop):
    if stop is None:
        return False
    return bool(stop.is_set() if hasattr(stop, "is_set") else stop())


def _check_stop(stop):
    if _cancelled(stop):
        raise Cancelled("已停止接收；服务端已提交的任务可能继续执行并计费，未自动重试")


def _token(token):
    if not isinstance(token, str) or not token.strip():
        raise ValueError("请先设置 NovelAI Persistent API Token")
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or any(not 33 <= ord(c) <= 126 for c in token) or len(token) > 8192:
        raise ValueError("NovelAI Token 格式无效")
    return token


def _safe_message(value, token=""):
    text = str(value)
    if token:
        text = text.replace(token, "[凭据已隐藏]")
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [已隐藏]", text)
    text = re.sub(r"(?i)(?:token|authorization|api[_ -]?key)\s*[:=]\s*[^,\s}]+", "credential=[已隐藏]", text)
    text = re.sub(r"[A-Za-z0-9_+/=-]{96,}", "[长数据已隐藏]", text)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return text[:500]


def _error_details(value, token=""):
    """Keep useful validation details without reflecting credentials or image data."""
    sensitive = {"token", "access_token", "refresh_token", "api_key", "api_keys",
                 "authorization", "password", "cookie", "headers", "image", "mask",
                 "reference_image", "reference_image_multiple", "director_reference_images",
                 "input", "prompt", "negative_prompt", "v4_prompt", "v4_negative_prompt"}

    def clean(item, depth=0):
        if depth > 4:
            return "[深层详情已省略]"
        if isinstance(item, dict):
            result = {}
            for key, value in list(item.items())[:20]:
                label = str(key)
                hidden = label.lower().replace("-", "_") in sensitive
                result[label[:80]] = "[已隐藏]" if hidden else clean(value, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            return [clean(entry, depth + 1) for entry in item[:20]]
        if isinstance(item, str):
            return _safe_message(item, token)
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return _safe_message(item, token)

    if value is None or value == "" or value == [] or value == {}:
        return ""
    cleaned = clean(value)
    text = cleaned if isinstance(cleaned, str) else json.dumps(cleaned, ensure_ascii=False)
    return _safe_message(text, token)


def _progress(callback, phase, message, progress=None):
    if callback:
        value = dict(phase=phase, message=message)
        if progress is not None:
            value["progress"] = progress
        callback(value)


def _payload_size(value):
    if isinstance(value, str):
        return len(value.encode("utf-8")) + 2
    if isinstance(value, dict):
        return sum(_payload_size(k) + _payload_size(v) + 2 for k, v in value.items()) + 2
    if isinstance(value, (list, tuple)):
        return sum(_payload_size(v) + 1 for v in value) + 2
    return 32


def _request(session, method, url, token, *, payload=None, params=None,
             accept="application/json", timeout=(15, 90), stop=None):
    _check_stop(stop)
    if payload is not None and _payload_size(payload) > MAX_RESPONSE:
        raise ValueError("请求数据超过 128 MiB，请减少参考图或输入尺寸")
    correlation = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(6))
    try:
        # redirects are not followed: a paid POST must never be replayed.
        response = session.request(method, url, json=payload, params=params, stream=True,
                                   headers={"Authorization": "Bearer " + token,
                                            "Accept": accept, "x-correlation-id": correlation},
                                   timeout=timeout, allow_redirects=False)
    except requests.RequestException:
        _check_stop(stop)
        suffix = "结果可能已生成并计费，未自动重试" if method == "POST" else "未自动重试"
        raise NovelAIError(f"NovelAI 网络请求失败或超时（请求 {correlation}）；{suffix}") from None
    if not 200 <= response.status_code < 300:
        try:
            raw = _read_bytes(response, 64 * 1024, stop)
            try:
                error = json.loads(raw)
                message = error.get("message", error.get("error", "")) if isinstance(error, dict) else ""
                details = error.get("details") if isinstance(error, dict) else None
            except (ValueError, UnicodeError):
                message, details = "", None
            safe = _error_details(message, token)
            detail_text = _error_details(details, token)
            if detail_text:
                safe += "；详情：" + detail_text
            names = {401: "Token 无效或已失效", 402: "Anlas 余额不足", 403: "账户无权执行此操作",
                     429: "请求受限", 504: "服务端超时，任务可能已计费"}
            detail = names.get(response.status_code, "服务返回错误")
            # Never infer replay safety from status alone, an SSE event,
            # a proxy 5xx, or the separate (possibly billed) Vibe encoding call.
            generation_urls = {IMAGE_API + path for path in (
                "/ai/generate-image", "/ai/generate-image-stream",
                "/ai/upscale", "/ai/augment-image")}
            rejection = None
            if method == "POST" and url in generation_urls:
                if response.status_code in (401, 402, 403):
                    rejection = "credential"
                elif (response.status_code == 429 and isinstance(message, str)
                      and " ".join(message.casefold().split()) == "concurrent generation is locked"):
                    rejection = "concurrency"
            raise NovelAIError(f"NovelAI HTTP {response.status_code}：{detail}；{safe}（请求 {correlation}）",
                               status_code=response.status_code, rejection_kind=rejection)
        finally:
            response.close()
    return response


def _read_bytes(response, limit, stop=None):
    try:
        length = int(response.headers.get("Content-Length", 0))
    except (TypeError, ValueError):
        length = 0
    if length > limit:
        raise NovelAIError("服务响应超过本地安全大小限制")
    data = bytearray()
    for chunk in response.iter_content(chunk_size=65536):
        _check_stop(stop)
        if chunk:
            if len(data) + len(chunk) > limit:
                raise NovelAIError("服务响应超过本地安全大小限制")
            data.extend(chunk)
    _check_stop(stop)
    return bytes(data)


def _open_image(path):
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > MAX_INPUT:
            raise ValueError("输入图像不存在或超过 32 MiB")
        with Image.open(p) as image:
            if image.width * image.height > MAX_PIXELS or getattr(image, "n_frames", 1) != 1:
                raise ValueError("输入图像须为单帧且不超过 16777216 像素")
            if image.format not in ("PNG", "JPEG", "WEBP", "BMP"):
                raise ValueError("输入图像格式只支持 PNG、JPEG、WebP、BMP")
            image.load()
            return ImageOps.exif_transpose(image).copy()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise ValueError("无法读取输入图像，请选择有效的本地图像文件") from None


def _png(image):
    out = BytesIO()
    image.save(out, format="PNG")
    data = out.getvalue()
    if len(data) > MAX_INPUT:
        raise ValueError("编码后的输入图像超过 32 MiB")
    return base64.b64encode(data).decode("ascii")


def _rgba_or_rgb(image, alpha, background="white"):
    rgba = image.convert("RGBA")
    if alpha:
        return rgba
    canvas = Image.new("RGBA", image.size, background)
    canvas.alpha_composite(rgba)
    return canvas.convert("RGB")


def prepare_image(path, size=None, *, alpha=False):
    image = _open_image(path)
    image = _rgba_or_rgb(image, alpha)
    if size and image.size != tuple(size):
        image = image.resize(tuple(size), Image.Resampling.LANCZOS)
    return _png(image), image.size


def prepare_mask(path, original_size, target_size):
    """Input: L PNG, white=edit. API: binary latent-resolution L PNG."""
    mask = _open_image(path)
    if mask.size != tuple(original_size):
        raise ValueError("蒙版尺寸必须与原输入图像一致")
    if mask.mode not in ("1", "L", "RGB"):
        raise ValueError("蒙版须为灰度图（白色编辑、黑色保留），请重新保存蒙版")
    mask = mask.convert("L")
    mask = mask.resize((target_size[0] // 8, target_size[1] // 8), Image.Resampling.NEAREST)
    mask = mask.point(lambda p: 255 if p >= 155 else 0)
    if mask.getextrema()[1] == 0:
        raise ValueError("蒙版没有可编辑区域")
    return _png(mask)


def _precise_image(path):
    image = _rgba_or_rgb(_open_image(path), False, "black")
    ratio = image.width / image.height
    size = min(((1024, 1536), (1536, 1024), (1472, 1472)), key=lambda s: abs(s[0] / s[1] - ratio))
    # Match the official resize-and-pad preprocessing, including upscaling.
    factor = min(size[0] / image.width, size[1] / image.height)
    contained = image.resize((max(1, round(image.width * factor)), max(1, round(image.height * factor))), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, "black")
    canvas.paste(contained, ((size[0] - contained.width) // 2, (size[1] - contained.height) // 2))
    return _png(canvas)


def _vibe(session, token, model, reference, stop, cache_dir, progress, timeout):
    # Encoding may charge Anlas. Concurrent tasks must recheck the shared cache
    # after the first encoding completes rather than paying for the same image twice.
    while not _VIBE_ENCODE_LOCK.acquire(timeout=0.1):
        _check_stop(stop)
    try:
        _check_stop(stop)
        return _vibe_locked(session, token, model, reference, stop, cache_dir, progress, timeout)
    finally:
        _VIBE_ENCODE_LOCK.release()


def _vibe_locked(session, token, model, reference, stop, cache_dir, progress, timeout):
    image, _ = prepare_image(reference["path"], alpha=False)
    info = reference["information_extracted"]
    key = hashlib.sha256((model + "\0" + str(info) + "\0" + image).encode("ascii")).hexdigest()
    cached_path = Path(cache_dir) / (key + ".naivibe") if cache_dir else None
    with _CACHE_LOCK:
        cached = _VIBE_CACHE.get(key)
        if cached is not None:
            _VIBE_CACHE.move_to_end(key)
            return cached
    if cached_path and cached_path.is_file() and 0 < cached_path.stat().st_size <= MAX_VIBE:
        cached = cached_path.read_bytes()
        return base64.b64encode(cached).decode("ascii")
    _progress(progress, "encoding", "正在编码 Vibe 参考图（此操作可能消耗 Anlas，完成后缓存）")
    response = _request(session, "POST", IMAGE_API + "/ai/encode-vibe", token,
                        payload=dict(image=image, model=model, information_extracted=info),
                        accept="application/octet-stream", stop=stop, timeout=(15, timeout))
    try:
        data = _read_bytes(response, MAX_VIBE, stop)
    finally:
        response.close()
    if not data or response.headers.get("Content-Type", "").lower().startswith(("application/json", "text/")):
        raise NovelAIError("Vibe 编码端点没有返回有效的二进制编码")
    encoded = base64.b64encode(data).decode("ascii")
    with _CACHE_LOCK:
        _VIBE_CACHE[key] = encoded
        while len(_VIBE_CACHE) > 16 or sum(len(v) for v in _VIBE_CACHE.values()) > MAX_RESPONSE:
            _VIBE_CACHE.popitem(last=False)
    if cached_path:
        tmp = cached_path.with_suffix("." + secrets.token_hex(4) + ".tmp")
        try:
            cached_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, cached_path)
        except OSError:
            _progress(progress, "encoding", "Vibe 已编码，磁盘缓存写入失败；本次仍可继续")
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return encoded


def _task_vibe(session, token, model, reference, stop, cache_dir, progress, timeout,
               state, task_cache_dir=None):
    """Pin a successful encoding for this task; a shared LRU is insufficient.

    Bytes live in the frozen task directory, never in each waiting task's RAM.
    A missing/corrupt pin is terminal: it must not become another paid encode.
    """
    image, unused = prepare_image(reference['path'], alpha=False)
    info = reference['information_extracted']
    key = hashlib.sha256((model + "\0" + str(info) + "\0" + image).encode('ascii')).hexdigest()
    pins = state.setdefault('vibe_assets', {})
    if key in pins:
        item = pins[key]
        try:
            if not item.get('ready'):
                raise ValueError('previous encoding was not safely cached')
            with open(item['path'], 'rb') as handle:
                data = handle.read(MAX_VIBE + 1)
            if (not 0 < len(data) <= MAX_VIBE or len(data) != item['size']
                    or hashlib.sha256(data).hexdigest() != item['sha256']):
                raise ValueError('cached encoding changed')
        except (OSError, ValueError, TypeError, KeyError):
            raise NovelAIError('任务的 Vibe 编码缓存丢失或损坏；已停止，未自动重新收费编码。') from None
        return base64.b64encode(data).decode('ascii')
    # Mark before invoking the encoder. Unknown outcomes must never be replayed
    # even if a caller accidentally reuses this state outside QueueService.
    pins[key] = {'ready': False}
    encoded = _vibe(session, token, model, reference, stop, cache_dir, progress, timeout)
    try:
        data = base64.b64decode(encoded, validate=True)
        if not 0 < len(data) <= MAX_VIBE:
            raise ValueError('invalid encoded data')
        if task_cache_dir is None:
            # One-shot callers also work; the caller-owned state retains the
            # TemporaryDirectory only when it explicitly spans attempts.
            temporary = state.get('_vibe_temporary')
            if temporary is None:
                temporary = state['_vibe_temporary'] = tempfile.TemporaryDirectory(prefix='aetherloom-nai-vibe-')
            directory = Path(temporary.name)
        else:
            directory = Path(task_cache_dir)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / (key + '.naivibe')
        part = destination.with_suffix('.' + secrets.token_hex(4) + '.part')
        try:
            with part.open('xb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(part, destination)
        finally:
            part.unlink(missing_ok=True)
        pins[key] = {'ready': True, 'path': str(destination), 'size': len(data),
                     'sha256': hashlib.sha256(data).hexdigest()}
    except (OSError, ValueError, TypeError):
        raise NovelAIError('Vibe 已编码，但任务缓存保存失败；未提交生成请求，未自动重新收费编码。') from None
    return encoded


def build_request(options, *, seed=None):
    """Pure payload builder; file/reference preparation is performed by run_image."""
    o = validate_options(options)
    if o["action"] not in ("generate", "img2img", "infill"):
        raise ValueError("此请求构造器仅用于图像生成；Director 和超分使用各自接口")
    chosen = secrets.randbelow(2**32) if o["seed"] == -1 else o["seed"]
    chosen = chosen if seed is None else seed
    prompt = build_prompts(resolve_options(o, chosen))
    params = {key: o[key] for key in ("width", "height", "steps", "scale", "sampler", "noise_schedule",
                                    "n_samples", "cfg_rescale")}
    params.update(prompt)
    params.update(params_version=4, seed=chosen, image_format="png", legacy=False,
                  dynamic_thresholding=False,
                  tag_hint_qt={"none": 0, "standard": 1, "light": 3}[o["quality_preset"]],
                  tag_hint_uc_preset={"none": 0, "heavy": 2, "light": 3,
                                      "humanFocus": 4, "furryFocus": 5}[o["uc_preset"]])
    if o["sampler"] == "k_euler_ancestral":
        params.update(deliberate_euler_ancestral_bug=False, prefer_brownian=True)
    if capabilities(o["model"])["transparency"]:
        params.update(straight_alpha=o["straight_alpha"], tag_hint_transparent_background=o["transparent_background"])
    if o["variety_boost"]:
        params["skip_cfg_above_sigma"] = 58 * math.sqrt(o["width"] * o["height"] / (832 * 1216))
    if o["action"] in ("img2img", "infill"):
        params["extra_noise_seed"] = (chosen - 1) % (2**32)
    if o["action"] == "img2img":
        params.update(strength=o["strength"], noise=o["noise"], color_correct=False)
        if o["upscaled_enhance"]:
            params["upscaled_enhance"] = True
    model = capabilities(o["model"])["inpaint_model"] if o["action"] == "infill" else o["model"]
    return dict(model=model, action=o["action"], input=prompt["prompt"], parameters=params)


def _image_item(data, index, seed=None):
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE:
        raise NovelAIError("返回图像为空或超过大小限制")
    metadata = {}
    try:
        with Image.open(BytesIO(data)) as im:
            if im.format not in ("PNG", "WEBP", "JPEG") or im.width * im.height > MAX_PIXELS:
                raise NovelAIError("返回图像的格式或尺寸超出限制")
            mime = Image.MIME[im.format]
            try:
                metadata = json.loads(im.info.get("Comment", "{}"))
                if not isinstance(metadata, dict):
                    metadata = {}
                if seed is None:
                    seed = metadata.get("seed")
            except (TypeError, ValueError):
                metadata = {}
            im.verify()
        with Image.open(BytesIO(data)) as decoded:
            decoded.load()
        if isinstance(seed, bool) or (seed is not None and (not isinstance(seed, int) or not 0 <= seed < 2**32)):
            raise NovelAIError("响应种子格式无效")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise NovelAIError("服务返回了损坏的图像") from None
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 16:
        raise NovelAIError("响应图像序号无效")
    result = dict(bytes=data, mime=mime, seed=seed, index=index)
    actual = metadata.get("actual_prompts", {})
    if isinstance(actual, dict):
        for key, field in (("prompt", "resolved_prompt"), ("negative_prompt", "resolved_negative_prompt")):
            caption = actual.get(key, {})
            if isinstance(caption, dict) and isinstance(caption.get("base_caption"), str):
                result[field] = caption["base_caption"][:100000]
        caption = actual.get("prompt", {})
        if isinstance(caption, dict) and isinstance(caption.get("char_captions"), list):
            result["resolved_characters"] = caption["char_captions"][:32]
        negative_caption = actual.get("negative_prompt", {})
        if isinstance(negative_caption, dict) and isinstance(negative_caption.get("char_captions"), list):
            result["resolved_negative_characters"] = negative_caption["char_captions"][:32]
    if "resolved_negative_characters" not in result:
        negative = metadata.get("v4_negative_prompt", {})
        caption = negative.get("caption", {}) if isinstance(negative, dict) else {}
        if isinstance(caption, dict) and isinstance(caption.get("char_captions"), list):
            result["resolved_negative_characters"] = caption["char_captions"][:32]
    return result


def _decode64(value):
    if not isinstance(value, str) or len(value) > (MAX_IMAGE * 4 // 3 + 8):
        raise NovelAIError("返回图像编码无效或过大")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise NovelAIError("返回图像 Base64 编码无效") from None


def _parse_images(raw, content_type):
    if raw.startswith(b"PK\x03\x04") or "zip" in content_type:
        try:
            with zipfile.ZipFile(BytesIO(raw)) as z:
                entries = z.infolist()
                if len(entries) > 32 or sum(e.file_size for e in entries) > MAX_RESPONSE:
                    raise NovelAIError("ZIP 内容超过本地安全限制")
                images = [e for e in entries if not e.is_dir() and Path(e.filename).suffix.lower() in (".png", ".webp", ".jpg", ".jpeg")]
                if not images or len(images) > 16:
                    raise NovelAIError("ZIP 没有有效图像或图像数量过多")
                def order(e):
                    digits = re.findall(r"\d+", Path(e.filename).stem)
                    return (int(digits[-1]) if digits else 0, e.filename)
                images.sort(key=order)
                matches = [re.fullmatch(r"image_?(\d+)", Path(e.filename).stem) for e in images]
                indices = [int(m[1]) for m in matches] if all(matches) else list(range(len(images)))
                if len(set(indices)) != len(indices):
                    raise NovelAIError("ZIP 包含重复的图像序号")
                out = []
                for index, entry in zip(indices, images):
                    if entry.file_size > MAX_IMAGE or entry.flag_bits & 1:
                        raise NovelAIError("ZIP 图像过大或被加密")
                    with z.open(entry) as handle:
                        data = handle.read(MAX_IMAGE + 1)
                    out.append(_image_item(data, index))
                return out
        except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError):
            raise NovelAIError("服务返回的 ZIP 无法读取") from None
    if "image/" in content_type:
        return [_image_item(raw, 0)]
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise NovelAIError("服务响应不是有效的 JSON 或图像 ZIP") from None
    items = payload.get("images") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 16:
        raise NovelAIError("响应没有有效的 images 列表")
    output = []
    for item in items:
        if not isinstance(item, dict):
            raise NovelAIError("响应图像条目格式无效")
        output.append(_image_item(_decode64(item.get("image")), item.get("index"), item.get("seed")))
    if len({i["index"] for i in output}) != len(output):
        raise NovelAIError("响应包含重复的图像序号")
    return sorted(output, key=lambda i: i["index"])


def _sse_lines(response, stop):
    """Decode bounded SSE lines across arbitrary HTTP chunk boundaries.

    SSE permits LF, CRLF, or CR and an initial UTF-8 BOM. In particular, a
    CRLF split between chunks must not dispatch an extra empty event.
    """
    buffer = bytearray()
    total = 0
    skip_lf = False
    first = True

    def decode(raw):
        nonlocal first
        if len(raw) > MAX_EVENT:
            raise NovelAIError("图像流单个事件过大")
        encoding = "utf-8-sig" if first else "utf-8"
        first = False
        try:
            return raw.decode(encoding)
        except UnicodeError:
            raise NovelAIError("图像流包含无效文本编码") from None

    for chunk in response.iter_content(chunk_size=16384):
        _check_stop(stop)
        if not chunk:
            continue
        total += len(chunk)
        # Preview streams can exceed the final archive limit but remain bounded.
        if total > MAX_RESPONSE * 4:
            raise NovelAIError("图像流超过本地安全大小限制")
        if skip_lf:
            if chunk.startswith(b"\n"):
                chunk = chunk[1:]
            skip_lf = False
        position = 0
        for match in re.finditer(br"\r\n|\r|\n", chunk):
            buffer.extend(chunk[position:match.start()])
            yield decode(buffer)
            buffer.clear()
            position = match.end()
            skip_lf = match.group() == b"\r" and position == len(chunk)
        buffer.extend(chunk[position:])
        if len(buffer) > MAX_EVENT:
            raise NovelAIError("图像流单个事件过大")
    if buffer:
        yield decode(buffer)


def _sse(response, expected, stop, preview, progress, token):
    finals = {}
    event, lines = "", []
    event_size = 0

    def dispatch():
        nonlocal event, lines, event_size
        if not lines:
            event = ""
            return
        data = "\n".join(lines)
        lines, event_size = [], 0
        current, event = event, ""
        if data == "[DONE]":
            return
        try:
            item = json.loads(data)
        except ValueError:
            raise NovelAIError("图像流包含无效 JSON 事件") from None
        if not isinstance(item, dict):
            raise NovelAIError("图像流事件格式无效")
        kind = item.get("event_type", item.get("type", current))
        if kind in ("error", "StreamingEventTypeError"):
            raise NovelAIError("NovelAI 图像流错误：" + _safe_message(item.get("message", "生成失败"), token))
        if kind not in ("intermediate", "final", "StreamingEventTypeIntermediate", "StreamingEventTypeFinal"):
            return
        ix = item.get("samp_ix", item.get("index"))
        if not isinstance(ix, int) or isinstance(ix, bool) or not 0 <= ix < expected:
            raise NovelAIError("图像流序号超出请求范围")
        image = _image_item(_decode64(item.get("image")), ix, item.get("seed"))
        if kind in ("final", "StreamingEventTypeFinal"):
            if ix in finals:
                raise NovelAIError("图像流返回重复的最终图像")
            if sum(len(v["bytes"]) for v in finals.values()) + len(image["bytes"]) > MAX_RESPONSE:
                raise NovelAIError("图像流最终图像超过本地内存限制")
            finals[ix] = image
            _progress(progress, "receiving", f"已收到 {len(finals)}/{expected} 张图像", len(finals) / expected)
        elif preview:
            preview(image)

    for line in _sse_lines(response, stop):
        if not line:
            dispatch()
            if len(finals) == expected:
                return [finals[i] for i in range(expected)]
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            event_size += len(value.encode("utf-8")) + 1
            if event_size > MAX_EVENT:
                raise NovelAIError("图像流单个事件过大")
            lines.append(value)
    dispatch()
    _check_stop(stop)
    if len(finals) != expected:
        raise NovelAIError(f"图像流提前结束，仅收到 {len(finals)}/{expected} 张最终图像；未自动重试")
    return [finals[i] for i in range(expected)]


def run_image(snapshot, token, *, stop=None, on_preview=None, on_progress=None, cache_dir=None, request_state=None, task_cache_dir=None, on_request=None):
    o = validate_options(snapshot)
    token = _token(token)
    _check_stop(stop)
    response = None
    with requests.Session() as session:
        try:
            _progress(on_progress, "preparing", "正在校验并准备图像")
            if o["action"] == "upscale" and o["model"] != UPSCALE_MODEL:
                raise ValueError("独立超分当前仅支持官网 V5 Curated 2×；请显式选择 V5 Curated 模型")
            generation = o["action"] in ("generate", "img2img", "infill")
            # Rejected submissions retain the same random seed and randomized
            # prompt. Large encoded image payloads are rebuilt from frozen files.
            state = request_state if request_state is not None else {}
            if generation and "base_request" not in state:
                state["base_request"] = build_request(o)
            request = copy.deepcopy(state["base_request"]) if generation else {}
            params = request.get("parameters", {})
            # Director has its own request shape. Resolve only once and retain
            # exactly that draw; do not build a discarded generation payload.
            if o["action"] == "augment" and "resolved_tool" not in state:
                uses_prompt = o["augment_method"] in ("colorize", "emotion")
                tool = dict(prompt=o["tool_prompt"] if uses_prompt else "",
                            negative_prompt="", characters=[],
                            chunks=o.get("chunks", {}) if uses_prompt else {})
                state["resolved_tool"] = resolve_options(tool)
            resolved_tool = state.get("resolved_tool") if o["action"] == "augment" else None
            if o["action"] != "generate":
                original = _open_image(o["image_path"])
                original_size = original.size
                original.close()
                if o["action"] == "upscale" and original_size[0] * original_size[1] * UPSCALE_FACTOR ** 2 > MAX_PIXELS:
                    raise ValueError("2×超分结果将超过本地 16777216 像素限制，请使用不超过 4194304 像素的输入图像")
                if o["action"] in ("img2img", "infill"):
                    params["image"], _ = prepare_image(o["image_path"], (o["width"], o["height"]),
                                                       alpha=capabilities(o["model"])["transparency"])
                if o["action"] == "infill":
                    params["mask"] = prepare_mask(o["mask_path"], original_size, (o["width"], o["height"]))
                    params["add_original_image"] = o["add_original_image"]
                    if o["inpaint_strength"] != 1:
                        params["img2img"] = dict(strength=o["inpaint_strength"], color_correct=True)
            # Validate *every* local reference before a possibly paid Vibe encode.
            input_total = 0
            refs = active_references(o) if generation else []
            for reference in refs:
                _open_image(reference["path"]).close()
                input_total += Path(reference["path"]).stat().st_size
                if input_total > MAX_RESPONSE:
                    raise ValueError("参考图总大小超过 128 MiB")
            if refs and refs[0]["kind"] == "vibe":
                # A valid small JPEG can expand beyond MAX_INPUT when converted
                # to PNG. Discover every such local failure before any paid
                # encoding, without retaining all full-size images in memory.
                for reference in refs:
                    prepare_image(reference["path"], alpha=False)
            if o["action"] in ("generate", "img2img", "infill"):
                if refs and refs[0]["kind"] == "vibe":
                    params["reference_image_multiple"] = [_task_vibe(session, token, o["model"], ref, stop, cache_dir, on_progress, o["timeout"], state, task_cache_dir) for ref in refs]
                    strengths = [r["strength"] for r in refs]
                    total_strength = sum(abs(v) for v in strengths)
                    if o["normalize_reference_strength_multiple"] and len(strengths) > 1 and total_strength > 1:
                        strengths = [v / total_strength for v in strengths]
                    params["reference_strength_multiple"] = strengths
                elif refs:
                    params["director_reference_images"] = [_precise_image(r["path"]) for r in refs]
                    params["director_reference_descriptions"] = [dict(caption=dict(base_caption=r["kind"], char_captions=[]), legacy_uc=False) for r in refs]
                    params["director_reference_information_extracted"] = [r["information_extracted"] for r in refs]
                    params["director_reference_strength_values"] = [r["strength"] for r in refs]
                    params["director_reference_secondary_strength_values"] = [1 - r["fidelity"] for r in refs]
                endpoint = "/ai/generate-image-stream" if o["stream"] else "/ai/generate-image"
                if o["stream"]:
                    params["stream"] = "sse"
                accept = "text/event-stream" if o["stream"] else "application/json"
            else:
                target_size = director_size(*original_size) if o["action"] == "augment" else None
                image, size = prepare_image(o["image_path"], target_size, alpha=True)
                if o["action"] == "upscale":
                    endpoint = "/ai/upscale"
                    request = dict(image=image, model=o["model"], declared_blur_sigma=o["declared_blur_sigma"])
                else:
                    endpoint = "/ai/augment-image"
                    prompt = resolved_tool["prompt"]
                    if o["augment_method"] == "emotion":
                        prompt = o["emotion"] + ";;" + prompt
                    request = dict(image=image, width=size[0], height=size[1], req_type=o["augment_method"])
                    if o["augment_method"] in ("colorize", "emotion"):
                        request.update(prompt=prompt, defry=o["defry"])
                accept = "application/json" if o["action"] == "upscale" else "application/zip"
            _check_stop(stop)
            if on_request:
                metadata = task_records.request_metadata(IMAGE_API + endpoint, request)
                if resolved_tool is not None:
                    metadata['resolved_tool'] = {'tool_prompt': resolved_tool['prompt']}
                on_request(metadata)
            _progress(on_progress, "submitted", "正在向 NovelAI 提交一次请求，请勿重复点击")
            response = _request(session, "POST", IMAGE_API + endpoint, token, payload=request, accept=accept, stop=stop, timeout=(15, o["timeout"]))
            _progress(on_progress, "accepted", "NovelAI 已接受请求，正在接收结果")
            if o["stream"] and o["action"] in ("generate", "img2img", "infill"):
                if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                    raise NovelAIError("流式端点没有返回 SSE；未改用其他端点或重试")
                result = _sse(response, o["n_samples"], stop, on_preview, on_progress, token)
            else:
                raw = _read_bytes(response, MAX_RESPONSE, stop)
                result = _parse_images(raw, response.headers.get("Content-Type", "").lower())
            if o["action"] in ("generate", "img2img", "infill"):
                if [item["index"] for item in result] != list(range(o["n_samples"])):
                    raise NovelAIError("服务返回的最终图像数量或序号与请求不符；未自动重试")
                for item in result:
                    item["request_seed"] = params["seed"]
                    item.setdefault("resolved_prompt", params["prompt"])
                    item.setdefault("resolved_negative_prompt", params["negative_prompt"])
                    item.setdefault("resolved_characters", params["v4_prompt"]["caption"]["char_captions"])
                    item.setdefault("resolved_negative_characters", params["v4_negative_prompt"]["caption"]["char_captions"])
            elif resolved_tool is not None:
                for item in result:
                    item.setdefault("resolved_prompt", resolved_tool["prompt"])
                    item.setdefault("resolved_negative_prompt", resolved_tool["negative_prompt"])
                    for key, field in (("prompt", "resolved_characters"),
                                       ("negative_prompt", "resolved_negative_characters")):
                        item.setdefault(field, [{"char_caption": ch[key],
                                                 "centers": [{"x": ch["x"], "y": ch["y"]}]}
                                                for ch in resolved_tool["characters"]])
            _progress(on_progress, "done", f"已收到 {len(result)} 张图像", 1.0)
            return result
        except requests.RequestException:
            _check_stop(stop)
            raise NovelAIError("NovelAI 接收响应失败或超时；任务可能已计费，未自动重试") from None
        finally:
            if response is not None:
                response.close()


def account(token, timeout=20):
    token = _token(token)
    with requests.Session() as session:
        response = _request(session, "GET", ACCOUNT_API + "/user/subscription", token, timeout=timeout)
        try:
            value = json.loads(_read_bytes(response, 1024 * 1024))
            if not isinstance(value, dict):
                raise NovelAIError("账户信息格式无效")
            return value
        except requests.RequestException:
            raise NovelAIError("读取 NovelAI 账户信息失败或超时") from None
        except (ValueError, UnicodeError):
            raise NovelAIError("账户信息不是有效 JSON") from None
        finally:
            response.close()


def suggest_tags(token, model, prompt, timeout=20, *, dataset_mode="anime"):
    token = _token(token)
    capabilities(model)
    if dataset_mode not in DATASET_MODES:
        raise ValueError("数据集模式必须是 Anime 或 Furry")
    if not isinstance(prompt, str) or len(prompt) > 1000:
        raise ValueError("标签查询过长")
    # Public web build 36c939a, chunk 266: V5 selects its tag dataset by type;
    # V4.5 Furry reuses V3's furry tag index without changing generation models.
    query = dict(model=model, prompt=prompt, lang="en")
    if model.startswith("nai-diffusion-5-"):
        query["type"] = "furryv5" if dataset_mode == "furry" else "animev5"
    elif dataset_mode == "furry":
        query["model"] = "nai-diffusion-furry-3"
    with requests.Session() as session:
        response = _request(session, "GET", IMAGE_API + "/ai/generate-image/suggest-tags", token,
                            params=query, timeout=timeout)
        try:
            value = json.loads(_read_bytes(response, 2 * 1024 * 1024))
            tags = value.get("tags", []) if isinstance(value, dict) else value
            if isinstance(tags, dict):
                tags = [tags]
            if not isinstance(tags, list):
                raise NovelAIError("标签建议格式无效")
            return [t for t in tags[:100] if isinstance(t, dict) and isinstance(t.get("tag"), str)]
        except requests.RequestException:
            raise NovelAIError("读取 NovelAI 标签建议失败或超时") from None
        except (ValueError, UnicodeError):
            raise NovelAIError("标签建议不是有效 JSON") from None
        finally:
            response.close()
