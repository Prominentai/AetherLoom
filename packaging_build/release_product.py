"""Assemble the portable release without copying local application data."""
import hashlib
import html
import json
import shutil
import tempfile
import uuid
from pathlib import Path

MANIFEST = 'product-manifest.json'
PRODUCT_FILES = (
    'autocomplete.txt', 'README.md',
    'assets/readme/canvas-0.2.png', 'assets/readme/app_icon.ico',
)
OPTIONAL_FILES = ('LICENSE',)
# Only accept these in a previous, hash-verified manifest during upgrades.
# They are no longer copied into new products.
LEGACY_PRODUCT_FILES = (
    'Grid_Reversal_Dec_local.py', 'app_icon.ico',
    'aetherloom_core/RH_MODEL_CATALOG_LICENSE.txt', 'THIRD_PARTY_NOTICES.txt',
)
ICON_SUFFIXES = {'.svg', '.png', '.ico', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}


def _regular_file(root, relative):
    path = root / relative
    if not path.is_file():
        raise FileNotFoundError(f'Missing release resource: {path}')
    if path.resolve() != path.absolute():
        raise ValueError(f'Release resources must not be links: {path}')
    return path


def product_resources(project_root):
    """Return validated (source, relative destination) pairs, never user folders."""
    root = Path(project_root).resolve()
    # Required even though notices now travel in the release README and EXE.
    _read_notices(root)
    entries = [(_regular_file(root, name), Path(name)) for name in PRODUCT_FILES]
    for name in OPTIONAL_FILES:
        if (root / name).exists():
            entries.append((_regular_file(root, name), Path(name)))
    return entries


def bundled_icon_resources(project_root):
    """Validate embedded UI icons separately from external README images."""
    root = Path(project_root).resolve()
    icon = 'app_icon.ico'
    entries = [(_regular_file(root, icon), Path(icon))]
    _regular_file(root, 'icons/home_emblem.svg')
    for path in sorted((root / 'icons').rglob('*')):
        if path.is_file() and path.suffix.lower() in ICON_SUFFIXES:
            relative = path.relative_to(root)
            entries.append((_regular_file(root, relative), relative))
    return entries


def _digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _release_name(name):
    path = Path(name)
    return (path.as_posix() == name and not path.is_absolute()
            and '..' not in path.parts and ':' not in name
            and (name in (*PRODUCT_FILES, *OPTIONAL_FILES, *LEGACY_PRODUCT_FILES, 'AetherLoom.exe')
                 or (len(path.parts) > 1 and path.parts[0] == 'icons'
                     and path.suffix.lower() in ICON_SUFFIXES)))


def _read_notices(root):
    return _regular_file(root, 'THIRD_PARTY_NOTICES.txt').read_text(encoding='utf-8')


def _manifest_record(raw):
    record = json.loads(raw.decode('utf-8'))
    files = record['files']
    if record.get('format_version') != 1 or not isinstance(files, dict):
        raise ValueError('Unsupported manifest')
    if not files or not all(isinstance(name, str) and _release_name(name)
                            for name in files):
        raise ValueError('Invalid manifest paths')
    if 'AetherLoom.exe' not in files:
        raise ValueError('Manifest does not describe a complete release')
    if any(not isinstance(value, str) or len(value) != 64
           or any(char not in '0123456789abcdef' for char in value)
           for value in files.values()):
        raise ValueError('Invalid manifest digest')
    return files


def _present(path):
    return path.exists() or path.is_symlink()


def _verify_product(product, files, embedded_manifest=None):
    """Verify against captured metadata, including when checking an old backup."""
    if not product.is_dir() or product.resolve() != product.absolute():
        raise ValueError(f'Invalid product directory: {product}')
    if files is None:
        if any(product.iterdir()):
            raise ValueError('Previously empty product contains new files')
        return
    expected = set(files)
    if embedded_manifest is not None:
        expected.add(MANIFEST)
        if _regular_file(product, MANIFEST).read_bytes() != embedded_manifest:
            raise ValueError('Modified embedded manifest')
    expected_dirs = {parent.as_posix() for name in files
                     for parent in Path(name).parents if parent != Path('.')}
    actual = set()
    for path in product.rglob('*'):
        if path.resolve() != path.absolute():
            raise ValueError('Linked product resource')
        relative = path.relative_to(product).as_posix()
        if path.is_dir():
            if relative not in expected_dirs:
                raise ValueError('Additional directory')
        elif path.is_file():
            actual.add(relative)
        else:
            raise ValueError('Invalid product resource')
    if actual != expected:
        raise ValueError('Additional or missing files')
    if any(_digest(product / name) != digest for name, digest in files.items()):
        raise ValueError('Modified release files')


def _check_previous(product):
    """Return verified upgrade metadata; never overwrite untracked user data.

    New releases keep their manifest beside product. A legacy manifest inside
    product is accepted only when no external manifest exists. Capturing both
    the contents and location also protects the transaction against changes
    made while its new files are being copied.
    """
    external = product.parent / MANIFEST
    embedded = product / MANIFEST
    message = (f'{product} contains untracked or modified files, or its build '
               'manifest is missing/invalid. Move the existing product directory '
               'and its build manifest before building again; personal settings, '
               'credentials and outputs will not be removed.')
    try:
        if _present(external) and _present(embedded):
            raise ValueError('Conflicting release manifests')
        external_raw = (_regular_file(product.parent, MANIFEST).read_bytes()
                        if _present(external) else None)
        if not _present(product):
            if external_raw is not None:
                raise ValueError('Manifest exists without its product')
            return {'exists': False, 'files': None, 'external': None, 'embedded': None}
        if not product.is_dir() or product.resolve() != product.absolute():
            raise ValueError('Invalid product directory')
        if not any(product.iterdir()):
            if external_raw is not None:
                raise ValueError('Manifest exists for an empty product')
            return {'exists': True, 'files': None, 'external': None, 'embedded': None}
        embedded_raw = (_regular_file(product, MANIFEST).read_bytes()
                        if _present(embedded) else None)
        raw = external_raw if external_raw is not None else embedded_raw
        if raw is None:
            raise ValueError('Missing release manifest')
        files = _manifest_record(raw)
        _verify_product(product, files, embedded_raw)
        return {'exists': True, 'files': files, 'external': external_raw,
                'embedded': embedded_raw}
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise ValueError(message) from exc


def check_product_destination(project_root):
    """Fail before an expensive build if product contains personal files."""
    pack = Path(project_root).resolve() / 'packaging_build'
    if pack.resolve() != pack.absolute():
        raise ValueError(f'Build directory must not be a link: {pack}')
    product = pack / 'product'
    _check_previous(product)
    return product


def _remove_build_directory(path, parent):
    """Delete only our verified staging/backup directory, never the product."""
    if (path.resolve().parent != parent.resolve()
            or path.resolve() != path.absolute()
            or not path.name.startswith(('.product-stage-', '.product-old-'))):
        raise ValueError(f'Refusing to remove unexpected build directory: {path}')
    shutil.rmtree(path)


def _remove_build_metadata(path, parent):
    """Remove only transaction metadata created directly in packaging_build."""
    if (path.resolve().parent != parent.resolve()
            or path.resolve() != path.absolute()
            or not path.name.startswith(('.product-manifest-stage-', '.product-manifest-old-'))
            or not path.is_file()):
        raise ValueError(f'Refusing to remove unexpected build metadata: {path}')
    path.unlink()


def _release_readme(source, notices):
    readme = source.read_text(encoding='utf-8').replace(
        '(THIRD_PARTY_NOTICES.txt)', '(#third-party-notices)')
    return (readme.rstrip() + '\n\n<details id="third-party-notices">\n'
            '<summary>第三方组件许可</summary>\n\n<pre>'
            + html.escape(notices, quote=False) + '</pre>\n</details>\n')


def assemble_product(project_root, executable):
    """Publish resources and a sibling manifest, rolling back either on failure."""
    root = Path(project_root).resolve()
    resources = product_resources(root)
    notices = _read_notices(root)
    executable = Path(executable)
    if not executable.is_file():
        raise FileNotFoundError(f'Build did not produce {executable}')
    with executable.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError(f'Build output is not a Windows executable: {executable}')
    product = check_product_destination(root)
    pack = product.parent
    pack.mkdir(parents=True, exist_ok=True)
    previous = _check_previous(product)
    stage = Path(tempfile.mkdtemp(prefix='.product-stage-', dir=pack))
    suffix = uuid.uuid4().hex
    backup = pack / ('.product-old-' + suffix)
    metadata = pack / MANIFEST
    metadata_stage = pack / ('.product-manifest-stage-' + suffix + '.json')
    metadata_backup = pack / ('.product-manifest-old-' + suffix + '.json')
    published = False
    try:
        entries = [(executable, Path('AetherLoom.exe')), *resources]
        manifest = {}
        for source, relative in entries:
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if relative.as_posix() == 'README.md':
                destination.write_text(_release_readme(source, notices), encoding='utf-8')
            else:
                shutil.copy2(source, destination)
            manifest[relative.as_posix()] = _digest(destination)
        metadata_raw = (json.dumps({'format_version': 1, 'files': manifest},
                                   ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        # Exclusive creation makes even an unexpected name collision fail closed.
        with metadata_stage.open('xb') as stream:
            stream.write(metadata_raw)
        if _check_previous(product) != previous:
            raise ValueError('Product or manifest changed while preparing the release')
        _verify_product(stage, manifest)
        try:
            if previous['external'] is not None:
                metadata.rename(metadata_backup)
            if previous['exists']:
                product.rename(backup)
            stage.rename(product)
            published = True
            # Do not replace a file that appeared since the old metadata moved.
            metadata_stage.rename(metadata)
        except BaseException:
            # Rollback uses captured records, never a newly published manifest.
            # If another process changed either target, preserve both copies.
            if published:
                _verify_product(product, manifest)
                product.rename(stage)
                published = False
            if backup.exists():
                backup.rename(product)
            if metadata_backup.exists():
                metadata_backup.rename(metadata)
            raise
        if backup.exists():
            try:
                _verify_product(backup, previous['files'], previous['embedded'])
                if metadata_backup.exists() and metadata_backup.read_bytes() != previous['external']:
                    raise ValueError('Previous build manifest changed')
                _remove_build_directory(backup, pack)
                if metadata_backup.exists():
                    _remove_build_metadata(metadata_backup, pack)
            except (OSError, ValueError) as exc:
                print(f'Previous product preserved at {backup}: {exc}')
        return product
    finally:
        if stage.exists():
            _remove_build_directory(stage, pack)
        if metadata_stage.exists():
            _remove_build_metadata(metadata_stage, pack)
