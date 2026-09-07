"""Local decoder loading and the importable multiprocessing entry point."""
import importlib.util
import os
import shutil
import sys
from aetherloom_core.paths import current_dir

LOCAL_GRC_MODULE = 'Grid_Reversal_Dec_local'
LOCAL_GRC_FILENAME = f'{LOCAL_GRC_MODULE}.py'
LOCAL_GRC_PATH = os.path.join(current_dir, LOCAL_GRC_FILENAME)

def _ensure_local_grc_copy():
    """Ensure a local copy of the *local* GRC module exists by copying
    it from the PyInstaller _MEIPASS bundled files if available.
    Do NOT fall back to any parent-directory Grid_Reversal_Dec file.
    """
    try:
        if os.path.exists(LOCAL_GRC_PATH):
            return
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            bundled_path = os.path.join(meipass, LOCAL_GRC_FILENAME)
            if os.path.exists(bundled_path):
                shutil.copy2(bundled_path, LOCAL_GRC_PATH)
                return
    except Exception:
        pass


def _load_grc_module():
    """Load only the local Grid_Reversal_Dec_local module.
    Do not attempt to import or read a sibling parent 'Grid_Reversal_Dec.py'.
    """
    try:
        return importlib.import_module(LOCAL_GRC_MODULE)
    except Exception:
        pass

    # Try to ensure a bundled copy exists and load from the local file path.
    _ensure_local_grc_copy()
    if not os.path.exists(LOCAL_GRC_PATH):
        raise ImportError(f'无法找到 {LOCAL_GRC_MODULE} 模块，请确保 {LOCAL_GRC_FILENAME} 存在于应用目录或打包资源中。')
    spec = importlib.util.spec_from_file_location(LOCAL_GRC_MODULE, LOCAL_GRC_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


grc = _load_grc_module()
try:
    grc.grid_cols = 32
    grc.grid_rows = int(grc.grid_cols) + 2
except Exception:
    pass

def _decode_sst_local(src_path, out_path, password=''):
    """Decode SSTool (Duck) payload. Returns final output path on success or '' on failure."""
    try:
        import numpy as _np
        from PIL import Image as _Img
        import moviepy.editor as _mpe
        import struct as _struct
    except Exception:
        return False

    WATERMARK_SKIP_W_RATIO = 0.40
    WATERMARK_SKIP_H_RATIO = 0.08
    TRY_K = (2, 6, 8)
    IMAGE_EXTS_LOCAL = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp')
    VIDEO_EXTS_LOCAL = ('.mp4', '.mov', '.avi', '.webm', '.mkv', '.gif')

    def _load_image_array(path):
        _, ext = os.path.splitext(path)
        ext = ext.lower()
        if ext in IMAGE_EXTS_LOCAL:
            img = _Img.open(path).convert('RGB')
            try:
                arr = _np.array(img).astype(_np.uint8)
            finally:
                try:
                    img.close()
                except Exception:
                    pass
            return arr
        if ext in VIDEO_EXTS_LOCAL:
            clip = _mpe.VideoFileClip(path)
            try:
                frame = clip.get_frame(0)
            finally:
                try:
                    clip.close()
                except Exception:
                    pass
            return frame.astype(_np.uint8)
        raise ValueError('unsupported input type')

    def _extract_payload_with_k(arr, k):
        h, w, c = arr.shape
        skip_w = int(w * WATERMARK_SKIP_W_RATIO)
        skip_h = int(h * WATERMARK_SKIP_H_RATIO)
        mask2d = _np.ones((h, w), dtype=bool)
        if skip_w > 0 and skip_h > 0:
            mask2d[:skip_h, :skip_w] = False
        mask3d = _np.repeat(mask2d[:, :, None], c, axis=2)
        flat = arr.reshape(-1)
        idxs = _np.flatnonzero(mask3d.reshape(-1))
        vals = (flat[idxs] & ((1 << k) - 1)).astype(_np.uint8)
        ub = _np.unpackbits(vals, bitorder='big').reshape(-1, 8)[:, -k:]
        bits = ub.reshape(-1)
        if len(bits) < 32:
            raise ValueError('Insufficient image data')
        len_bits = bits[:32]
        length_bytes = _np.packbits(len_bits, bitorder='big').tobytes()
        header_len = _struct.unpack('>I', length_bytes)[0]
        total_bits = 32 + header_len * 8
        if header_len <= 0 or total_bits > len(bits):
            raise ValueError('Payload length invalid')
        payload_bits = bits[32:32 + header_len * 8]
        return _np.packbits(payload_bits, bitorder='big').tobytes()

    def _generate_key_stream(password_local, salt, length):
        import hashlib as _hashlib
        key_material = (password_local + salt.hex()).encode('utf-8')
        out = bytearray()
        counter = 0
        while len(out) < length:
            out.extend(_hashlib.sha256(key_material + str(counter).encode('utf-8')).digest())
            counter += 1
        return bytes(out[:length])

    def _parse_header(header, password_local):
        idx = 0
        if len(header) < 1:
            raise ValueError('Header corrupted')
        has_pwd = header[0] == 1
        idx += 1
        pwd_hash = b''
        salt = b''
        if has_pwd:
            if len(header) < idx + 32 + 16:
                raise ValueError('Header corrupted')
            pwd_hash = header[idx:idx + 32]
            idx += 32
            salt = header[idx:idx + 16]
            idx += 16
        if len(header) < idx + 1:
            raise ValueError('Header corrupted')
        ext_len = header[idx]
        idx += 1
        if len(header) < idx + ext_len + 4:
            raise ValueError('Header corrupted')
        ext = header[idx:idx + ext_len].decode('utf-8', errors='ignore')
        idx += ext_len
        data_len = _struct.unpack('>I', header[idx:idx + 4])[0]
        idx += 4
        data = header[idx:]
        if len(data) != data_len:
            raise ValueError('Data length mismatch')
        if not has_pwd:
            return data, ext
        if not password_local:
            raise ValueError('Password required')
        import hashlib as _hashlib
        check_hash = _hashlib.sha256((password_local + salt.hex()).encode('utf-8')).digest()
        if check_hash != pwd_hash:
            raise ValueError('Wrong password')
        ks = _generate_key_stream(password_local, salt, len(data))
        plain = bytes(a ^ b for a, b in zip(data, ks))
        return plain, ext

    def _decode_array(arr, password_local):
        for k in TRY_K:
            try:
                header = _extract_payload_with_k(arr, k)
                raw, ext = _parse_header(header, password_local)
                return raw, ext
            except Exception:
                continue
        raise RuntimeError('解析失败: 无法从图像提取载荷')

    def _save_payload(raw, ext, out_base):
        final_ext = ext
        if ext.endswith('.binpng'):
            tmp_png = out_base + '.binpng'
            with open(tmp_png, 'wb') as f:
                f.write(raw)
            try:
                img = _Img.open(tmp_png).convert('RGB')
                arr = _np.array(img).astype(_np.uint8)
            finally:
                try:
                    img.close()
                except Exception:
                    pass
                try:
                    os.unlink(tmp_png)
                except Exception:
                    pass
            mp4_bytes = arr.reshape(-1, 3).reshape(-1).tobytes().rstrip(b'\x00')
            final_path = out_base + '.mp4'
            with open(final_path, 'wb') as f:
                f.write(mp4_bytes)
            final_ext = 'mp4'
        else:
            if ext.startswith('.'):  # keep leading dot
                final_path = out_base + ext
            else:
                final_path = out_base + '.' + ext
            with open(final_path, 'wb') as f:
                f.write(raw)
        return final_path, final_ext

    try:
        arr = _load_image_array(src_path)
        raw, ext = _decode_array(arr, password or '')
        out_base, _ = os.path.splitext(out_path)
        final_path, _ = _save_payload(raw, ext, out_base)
        return final_path if final_path and os.path.exists(final_path) else ''
    except Exception:
        return ''


def _file_process_worker(queue, src, dst, is_image, keep_audio, decode_mode, grid_cols, grid_rows, password=''):
    """Run file processing in a separate process so it can be terminated on cancel."""
    try:
        ok = False
        out_path_final = dst
        if decode_mode == 'sst':
            out_path_final = _decode_sst_local(src, dst, password)
            ok = bool(out_path_final)
        else:
            # import inside child process to ensure fresh interpreter state
            grc_child = _load_grc_module()
            # choose decoder
            grc_child.grid_cols = int(grid_cols)
            grc_child.grid_rows = int(grid_rows)
            dec = grc_child
            if is_image:
                ok = dec.reverse_image_grid(src, dst)
            else:
                ok = dec.restore_video_cv2(src, dst)
        queue.put(('OK' if ok else 'FAIL', out_path_final if ok else ''))
    except Exception as e:
        try:
            queue.put(('ERROR', str(e)))
        except Exception:
            pass


def _mode_for_label(lbl, grc_label, sst_label=None):
    try:
        if sst_label is not None and lbl is sst_label:
            return 'sst'
    except Exception:
        pass
    return 'grc'
