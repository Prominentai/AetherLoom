"""Reject unused ML frameworks from AetherLoom's one-file releases.

This module does not import any optional runtime dependency.  Its CLI is run
with the selected build interpreter, where PyInstaller is already available.
Archive names are inspected without importing or executing bundled modules.
"""

import io
import json
import re
import sys
import zipfile
import zlib
from pathlib import Path


BLOCKED_MODULES = (
    'torch', 'torchvision', 'torchaudio', 'torchtext', 'torchgen', 'functorch',
    '_functorch', 'tensorflow', '_tensorflow', 'keras', 'tf_keras',
    'jax', 'jaxlib', 'flax', 'cupy', 'cupyx', 'cupy_backends', 'paddle',
    'mxnet', 'mindspore', 'onnxruntime', 'onnx', 'tensorrt',
    'tensorrt_bindings', 'tensorrt_libs', 'nvidia', 'triton', 'xformers',
    'bitsandbytes', 'transformers', 'diffusers', 'ultralytics',
)
_BLOCKED = frozenset(BLOCKED_MODULES)
MAX_ZIP_BYTES = 64 * 1024 * 1024
MAX_ZIP_DEPTH = 3
MAX_ARCHIVE_ENTRIES = 250_000
_PREFIXES = frozenset(('_internal', 'site-packages', 'dist-packages'))
_ZIP_SUFFIXES = ('.zip', '.whl', '.egg')
_BINARY_PATTERN = re.compile(
    r'^(?:'
    r'(?:lib)?(?:torch|caffe2)(?:[_-][a-z0-9_.-]+)?|'
    r'(?:lib)?c10(?:[_-][a-z0-9_.-]+)?|'
    r'(?:lib)?(?:tensorflow|onnxruntime|paddle|mxnet|mindspore)'
    r'(?:[_-][a-z0-9_.-]+)?|'
    r'_pywrap_tensorflow_internal|'
    r'(?:lib)?(?:cudart|cublas|cudnn|cufft|curand|cusolver|cusparse|'
    r'cufile|nvrtc|nvjitlink|nvfatbinaryloader|nvinfer|nvonnxparser|'
    r'nvtoolsext)[a-z0-9_.-]*|'
    r'nvcuda|nvml|(?:lib)?fbgemm'
    r')\.(?:dll|pyd|dylib|so(?:\.[0-9]+)*)$', re.IGNORECASE,
)


class BundlePolicyError(RuntimeError):
    """The artifact cannot be certified against the release policy."""


def exclusion_options():
    """Arguments shared by subprocess and in-process PyInstaller builds."""
    return ['--exclude-module=' + name for name in BLOCKED_MODULES]


def blocked_module(name):
    """Match a Python package root, never an unrelated compatibility shim."""
    root = str(name).strip().replace('\\', '.').replace('/', '.').split('.', 1)[0]
    return root.casefold() in _BLOCKED


def _blocked_distribution(name):
    # Distribution names differ from their import roots (e.g. cupy-cuda12x).
    name = re.split(r'-(?=[0-9])', name, maxsplit=1)[0]
    name = re.sub(r'[-_.]+', '_', name).casefold()
    if name in _BLOCKED or name in {
        'pytorch', 'tensorflow_cpu', 'tensorflow_gpu', 'tensorflow_intel',
        'tensorflow_macos', 'onnxruntime_gpu', 'onnxruntime_directml',
        'paddlepaddle', 'paddlepaddle_gpu',
    }:
        return True
    return bool(re.fullmatch(r'cupy_cuda[0-9]+x?', name)
                or re.fullmatch(r'tensorrt_cu[0-9]+(?:_bindings|_libs)?', name)
                or name.startswith('nvidia_'))


def _path_reason(name):
    normalized = str(name).replace('\\', '/')
    parts = [part for part in normalized.split('/') if part not in ('', '.')]
    if normalized.startswith('/') or any(part == '..' or ':' in part for part in parts):
        return 'unsafe archive path'
    while parts and parts[0].casefold() in _PREFIXES:
        parts.pop(0)
    if not parts:
        return None
    root = parts[0].casefold()
    if blocked_module(root):
        return 'forbidden package ' + root.split('.', 1)[0]
    for suffix in ('.dist-info', '.egg-info', '.libs'):
        if root.endswith(suffix) and _blocked_distribution(root[:-len(suffix)]):
            return 'forbidden distribution ' + root
    if _BINARY_PATTERN.fullmatch(parts[-1]):
        return 'forbidden ML/GPU binary'
    return None


def _bounded_carchive_zip(archive, name, entry):
    """Read only a bounded ZIP blob, including a bound on actual decompression."""
    offset, length, unpacked_length, compressed, _typecode = entry
    if min(offset, length, unpacked_length) < 0:
        raise BundlePolicyError('Invalid archive entry: ' + name)
    if max(length, unpacked_length) > MAX_ZIP_BYTES:
        raise BundlePolicyError('ZIP exceeds 64 MiB audit limit: ' + name)
    # extract() decompresses without a size bound.  Read the documented TOC
    # offsets using the reader's archive start instead, so malformed sizes also
    # cannot cause an unbounded allocation.
    with open(archive._filename, 'rb') as stream:
        stream.seek(archive._start_offset + offset)
        data = stream.read(length)
    if len(data) != length:
        raise BundlePolicyError('Truncated ZIP archive: ' + name)
    if compressed:
        decoder = zlib.decompressobj()
        data = decoder.decompress(data, MAX_ZIP_BYTES + 1)
        if len(data) > MAX_ZIP_BYTES or decoder.unconsumed_tail or not decoder.eof:
            raise BundlePolicyError('Invalid or oversized compressed ZIP: ' + name)
        if decoder.unused_data:
            raise BundlePolicyError('Trailing compressed ZIP data: ' + name)
    if len(data) != unpacked_length:
        raise BundlePolicyError('ZIP length disagrees with archive table: ' + name)
    return data


def audit_bundle(executable):
    """Audit a PyInstaller one-file EXE; return counts or raise on any failure.

    Only one-file artifacts are supported.  Requiring embedded PYZ and the base
    library prevents an onedir EXE from passing while its external files escape
    inspection.  A successful result does not execute or smoke-test the EXE.
    """
    path = Path(executable).resolve()
    if not path.is_file():
        raise BundlePolicyError('Executable does not exist: ' + str(path))
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as exc:
        raise BundlePolicyError('Run the audit with the PyInstaller build interpreter.') from exc

    counts = {'executable': str(path), 'archive_entries': 0, 'python_modules': 0,
              'zip_archives': 0, 'zip_entries': 0, 'zip_bytes': 0}
    violations = []
    total_entries = 0

    def inspect_name(name, origin, module=False):
        nonlocal total_entries
        total_entries += 1
        if total_entries > MAX_ARCHIVE_ENTRIES:
            raise BundlePolicyError('Too many archive entries to audit: ' + origin)
        reason = ('forbidden Python package' if blocked_module(name) else None) if module else _path_reason(name)
        if reason:
            violations.append(origin + ': ' + name + ' (' + reason + ')')
            if len(violations) >= 50:
                raise BundlePolicyError('Forbidden release contents (first 50):\n' + '\n'.join(violations))

    def inspect_zip(data, origin, depth=1):
        if depth > MAX_ZIP_DEPTH:
            raise BundlePolicyError('ZIP nesting exceeds audit limit: ' + origin)
        counts['zip_bytes'] += len(data)
        if counts['zip_bytes'] > MAX_ZIP_BYTES:
            raise BundlePolicyError('Total ZIP data exceeds 64 MiB audit limit: ' + origin)
        counts['zip_archives'] += 1
        with zipfile.ZipFile(io.BytesIO(data)) as zipped:
            for info in zipped.infolist():
                counts['zip_entries'] += 1
                inspect_name(info.filename, origin)
                if info.filename.casefold().endswith(_ZIP_SUFFIXES) and not info.is_dir():
                    remaining = MAX_ZIP_BYTES - counts['zip_bytes']
                    if info.file_size > remaining:
                        raise BundlePolicyError('Nested ZIP exceeds audit size limit: ' + origin + '/' + info.filename)
                    with zipped.open(info) as stream:
                        nested = stream.read(remaining + 1)
                    if len(nested) > remaining or len(nested) != info.file_size:
                        raise BundlePolicyError('Invalid or oversized nested ZIP: ' + origin + '/' + info.filename)
                    inspect_zip(nested, origin + '/' + info.filename, depth + 1)

    try:
        archive = CArchiveReader(str(path))
        pyz_count = 0
        base_zip_found = False
        for name, entry in archive.toc.items():
            counts['archive_entries'] += 1
            inspect_name(name, 'CArchive')
            if entry[-1] == 'z':
                pyz_count += 1
                embedded = archive.open_embedded_archive(name)
                for module_name in embedded.toc:
                    counts['python_modules'] += 1
                    inspect_name(module_name, name, module=True)
            elif entry[-1] == 'Z' or name.casefold().endswith(_ZIP_SUFFIXES):
                if name.replace('\\', '/').rsplit('/', 1)[-1].casefold() == 'base_library.zip':
                    base_zip_found = True
                inspect_zip(_bounded_carchive_zip(archive, name, entry), name)
        if not pyz_count or not base_zip_found:
            raise BundlePolicyError('Unsupported/incomplete bundle: expected one-file EXE with embedded PYZ and base_library.zip.')
    except BundlePolicyError:
        raise
    except Exception as exc:
        raise BundlePolicyError('Cannot audit ' + str(path) + ': ' + str(exc)) from exc
    if violations:
        raise BundlePolicyError('Forbidden release contents:\n' + '\n'.join(violations))
    return counts


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print('Usage: python bundle_policy.py ONEFILE_EXE', file=sys.stderr)
        return 2
    try:
        summary = audit_bundle(arguments[0])
    except BundlePolicyError as exc:
        print('Bundle policy audit failed: ' + str(exc), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
