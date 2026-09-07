import os
import sys
import struct
import time
import numpy as np
from PIL import Image
import moviepy.editor as mpe

# 基本参数
CATEGORY = "SSTool"
WATERMARK_SKIP_W_RATIO = 0.40
WATERMARK_SKIP_H_RATIO = 0.08
TRY_K = (2, 6, 8)
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".webm", ".mkv", ".gif")
DEFAULT_PASSWORD = ""  # 如需密码解密，在此填写或运行时通过环境变量 DUCK_DEC_PASSWORD 提供


def _extract_payload_with_k(arr: np.ndarray, k: int) -> bytes:
    h, w, c = arr.shape
    skip_w = int(w * WATERMARK_SKIP_W_RATIO)
    skip_h = int(h * WATERMARK_SKIP_H_RATIO)
    mask2d = np.ones((h, w), dtype=bool)
    if skip_w > 0 and skip_h > 0:
        mask2d[:skip_h, :skip_w] = False
    mask3d = np.repeat(mask2d[:, :, None], c, axis=2)
    flat = arr.reshape(-1)
    idxs = np.flatnonzero(mask3d.reshape(-1))
    vals = (flat[idxs] & ((1 << k) - 1)).astype(np.uint8)
    ub = np.unpackbits(vals, bitorder="big").reshape(-1, 8)[:, -k:]
    bits = ub.reshape(-1)
    if len(bits) < 32:
        raise ValueError("Insufficient image data")
    len_bits = bits[:32]
    length_bytes = np.packbits(len_bits, bitorder="big").tobytes()
    header_len = struct.unpack(">I", length_bytes)[0]
    total_bits = 32 + header_len * 8
    if header_len <= 0 or total_bits > len(bits):
        raise ValueError("Payload length invalid")
    payload_bits = bits[32:32 + header_len * 8]
    return np.packbits(payload_bits, bitorder="big").tobytes()


def _generate_key_stream(password: str, salt: bytes, length: int) -> bytes:
    import hashlib
    key_material = (password + salt.hex()).encode("utf-8")
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hashlib.sha256(key_material + str(counter).encode("utf-8")).digest())
        counter += 1
    return bytes(out[:length])


def _parse_header(header: bytes, password: str):
    idx = 0
    if len(header) < 1:
        raise ValueError("Header corrupted")
    has_pwd = header[0] == 1
    idx += 1
    pwd_hash = b""
    salt = b""
    if has_pwd:
        if len(header) < idx + 32 + 16:
            raise ValueError("Header corrupted")
        pwd_hash = header[idx:idx + 32]
        idx += 32
        salt = header[idx:idx + 16]
        idx += 16
    if len(header) < idx + 1:
        raise ValueError("Header corrupted")
    ext_len = header[idx]
    idx += 1
    if len(header) < idx + ext_len + 4:
        raise ValueError("Header corrupted")
    ext = header[idx:idx + ext_len].decode("utf-8", errors="ignore")
    idx += ext_len
    data_len = struct.unpack(">I", header[idx:idx + 4])[0]
    idx += 4
    data = header[idx:]
    if len(data) != data_len:
        raise ValueError("Data length mismatch")
    if not has_pwd:
        return data, ext
    if not password:
        raise ValueError("Password required")
    import hashlib
    check_hash = hashlib.sha256((password + salt.hex()).encode("utf-8")).digest()
    if check_hash != pwd_hash:
        raise ValueError("Wrong password")
    ks = _generate_key_stream(password, salt, len(data))
    plain = bytes(a ^ b for a, b in zip(data, ks))
    return plain, ext


def binpng_bytes_to_mp4_bytes(p: str) -> bytes:
    img = Image.open(p).convert("RGB")
    arr = np.array(img).astype(np.uint8)
    flat = arr.reshape(-1, 3).reshape(-1)
    return flat.tobytes().rstrip(b"\x00")


def _decode_array(arr: np.ndarray, password: str):
    for k in TRY_K:
        try:
            header = _extract_payload_with_k(arr, k)
            raw, ext = _parse_header(header, password)
            return raw, ext
        except Exception:
            continue
    raise RuntimeError("解析失败: 无法从图像提取载荷")


def _save_payload(raw: bytes, ext: str, out_base: str):
    final_ext = ext
    final_path = ""
    if ext.endswith(".binpng"):
        tmp_png = out_base + ".binpng"
        with open(tmp_png, "wb") as f:
            f.write(raw)
        mp4_bytes = binpng_bytes_to_mp4_bytes(tmp_png)
        os.unlink(tmp_png)
        final_path = out_base + ".mp4"
        with open(final_path, "wb") as f:
            f.write(mp4_bytes)
        final_ext = "mp4"
    else:
        if ext.startswith("."):
            final_path = out_base + ext
        else:
            final_path = out_base + "." + ext
        with open(final_path, "wb") as f:
            f.write(raw)
    return final_path, final_ext


def _load_image_array(path: str) -> np.ndarray:
    _, ext = os.path.splitext(path)
    ext = ext.lower()
    if ext in IMAGE_EXTS:
        img = Image.open(path).convert("RGB")
        return np.array(img).astype(np.uint8)
    if ext in VIDEO_EXTS:
        clip = mpe.VideoFileClip(path)
        frame = clip.get_frame(0)
        clip.close()
        return frame.astype(np.uint8)
    raise ValueError("不支持的输入类型")


def process_file(input_path: str, output_dir: str, password: str):
    name, _ = os.path.splitext(os.path.basename(input_path))
    out_base = os.path.join(output_dir, f"{name}_recovered")
    try:
        arr = _load_image_array(input_path)
        raw, ext = _decode_array(arr, password)
        final_path, final_ext = _save_payload(raw, ext, out_base)
        print(f"✓ 解码成功 {os.path.basename(input_path)} -> {os.path.basename(final_path)} (ext={final_ext})")
    except Exception as e:
        print(f"✗ 解码失败 {os.path.basename(input_path)}: {e}")


def main():
    print("=" * 50)
    print("Duck Decoder Local")
    print("=" * 50)

    password = os.environ.get("DUCK_DEC_PASSWORD", DEFAULT_PASSWORD)

    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    input_dir = os.path.join(base_dir, "解码前")
    output_dir = os.path.join(base_dir, "解码后")

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    print(f"扫描目录: {input_dir}")

    if not os.path.exists(input_dir):
        os.makedirs(input_dir)
        print(f"输入文件夹不存在，已创建: {input_dir}")
        print("请将待解码文件放入该文件夹后重新运行。")
        time.sleep(5)
        return

    input_files = os.listdir(input_dir)
    if not input_files:
        print("输入文件夹为空")
        time.sleep(5)
        return

    supported = IMAGE_EXTS + VIDEO_EXTS
    start_all = time.time()
    processed = 0
    for filename in input_files:
        filepath = os.path.join(input_dir, filename)
        if not os.path.isfile(filepath):
            continue
        if not filename.lower().endswith(supported):
            continue
        processed += 1
        t0 = time.time()
        process_file(filepath, output_dir, password)
        t1 = time.time()
        print(f"    用时: {t1 - t0:.2f} 秒\n")

    elapsed = time.time() - start_all
    print("=" * 50)
    print(f"完成，处理文件数: {processed}, 总耗时: {elapsed:.2f} 秒")
    print("5秒后退出...")
    time.sleep(5)


if __name__ == "__main__":
    main()
