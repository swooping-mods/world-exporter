"""Lossless saved-slot snapshots with bounded verification and no source writes."""
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import time
import zipfile
from . import __version__

SCHEMA = 'moon-sweepers-world/v1'
MAX_FILES = 20000
MAX_TOTAL = 8 * 1024 ** 3
MAX_FILE = 4 * 1024 ** 3
MAX_MANIFEST = 8 * 1024 ** 2
CHUNK = 1024 ** 2
NOFOLLOW_SOURCE = (os.open in os.supports_dir_fd and os.scandir in os.supports_fd
                   and hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'))


class ExportError(ValueError):
    pass


class SourceChanged(ExportError):
    pass


def canonical_json(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


def unique_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ExportError('Duplicate JSON member: ' + key)
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or path.as_posix() != name or '\\' in name:
        raise ExportError('Noncanonical payload path: ' + name)
    for part in path.parts:
        if part in ('.', '..') or any(ord(c) < 32 or c in '<>:"|?*' for c in part) or part.endswith((' ', '.')):
            raise ExportError('Nonportable payload path: ' + name)
        stem = part.split('.')[0].upper()
        if stem in {'CON', 'PRN', 'AUX', 'NUL'} or re.fullmatch(r'(COM|LPT)[1-9]', stem):
            raise ExportError('Reserved payload name: ' + name)
    return name


def slot_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'slot-[0-9]+', name):
        raise ExportError('Choose a slot such as slot-1')
    return name


def signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class Snapshot:
    files: dict
    directories: tuple
    total_bytes: int


@dataclass(frozen=True)
class ProfileSource:
    path: Path
    descriptor: int

    def check(self):
        # Keep the validated name bound to the pinned directory. Neither an
        # alias swap nor a real replacement directory may adopt another source.
        with source_handle(self.path, directory=True) as current:
            actual, expected = os.fstat(current), os.fstat(self.descriptor)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise SourceChanged('Validated profile directory was replaced')


@dataclass(frozen=True)
class SlotSource:
    path: Path
    profile: ProfileSource

    def __fspath__(self):
        return os.fspath(self.path)


@contextmanager
def open_profile(path, policy=None):
    """Canonicalize once, enforce the frozen policy path, then pin the source."""
    if isinstance(path, ProfileSource):
        path.check()
        yield path
        return
    canonical = Path(path).expanduser().resolve(strict=True)
    if policy is not None:
        # Managed settings record an already canonical prefix. Resolving this
        # name again would trust a newly installed cross-storefront symlink.
        root = Path(policy['profile_root'])
        if not root.is_absolute() or '..' in root.parts:
            raise ExportError('Save separation policy requires a canonical absolute profile prefix')
        relative = canonical.relative_to(root).parts if canonical.is_relative_to(root) else ()
        if len(relative) != 5 or relative[1:] != ('AppData', 'LocalLow', 'DragDropGames', 'MoonSweepers'):
            raise ExportError('Save separation: choose the ' + policy['storefront'] + ' profile from this lane\'s own prefix')
    with source_handle(canonical, directory=True) as descriptor:
        profile = ProfileSource(canonical, descriptor)
        profile.check()
        yield profile


@contextmanager
def source_handle(path, *, directory=False, profile=None):
    # Anchor every component, not only the leaf: O_NOFOLLOW on a save file
    # alone still follows a directory replaced by a symlink during a save.
    if not NOFOLLOW_SOURCE:
        raise ExportError('Safe source traversal requires directory handles and no-follow support on this platform')
    path = Path(path).absolute()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | getattr(os, 'O_CLOEXEC', 0)
    if profile is not None:
        profile.check()
        try:
            parts = path.relative_to(profile.path).parts
        except ValueError as error:
            raise ExportError('Source traversal escaped the validated profile') from error
        descriptor = os.dup(profile.descriptor)
    else:
        parts = path.parts[1:]
        descriptor = os.open(path.anchor, flags)
    try:
        for index, part in enumerate(parts):
            if part in ('.', '..'):
                raise ExportError('Noncanonical source traversal component')
            leaf = index == len(parts) - 1
            mode = flags if directory or not leaf else (
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))
            next_descriptor = os.open(part, mode, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    finally:
        os.close(descriptor)


def scan(root):
    profile = root.profile if isinstance(root, SlotSource) else None
    if profile is not None:
        profile.check()
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ExportError('Saved slot must be a regular directory')
    files = {}
    directories = []
    folded = set()
    total = 0
    pending = [(root, 0)]
    while pending:
        folder, depth = pending.pop()
        if depth > 64:
            raise ExportError('Save directory nesting exceeds the supported bound')
        with source_handle(folder, directory=True, profile=profile) as descriptor, os.scandir(descriptor) as entries:
            for entry in entries:
                path = folder / entry.name
                name = safe_name(path.relative_to(root).as_posix())
                if name.casefold() in folded:
                    raise ExportError('Case-insensitive path collision: ' + name)
                folded.add(name.casefold())
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ExportError('Symlinks are not saved-world payloads: ' + name)
                if stat.S_ISDIR(info.st_mode):
                    directories.append(name)
                    pending.append((path, depth + 1))
                elif stat.S_ISREG(info.st_mode):
                    if info.st_size > MAX_FILE:
                        raise ExportError('Save file exceeds the supported bound: ' + name)
                    files[name] = signature(info)
                    total += info.st_size
                else:
                    raise ExportError('Special file refused: ' + name)
                if len(folded) > MAX_FILES or total > MAX_TOTAL:
                    raise ExportError('Saved slot exceeds the export size/file bound')
    return Snapshot(files=dict(sorted(files.items())), directories=tuple(sorted(directories)), total_bytes=total)


def locate_slot(profile, name):
    pinned = profile if isinstance(profile, ProfileSource) else None
    profile = pinned.path if pinned is not None else Path(profile).expanduser().resolve(strict=True)
    if pinned is not None:
        pinned.check()
    sessions = profile / 'MoonSessions'
    slot = sessions / slot_name(name)
    if sessions.is_symlink() or slot.is_symlink():
        raise ExportError('MoonSessions and slot directories must not be symlinks')
    return profile, SlotSource(slot, pinned) if pinned is not None else slot


def require_world(snapshot):
    if 'slot.json' not in snapshot.files or not any(PurePosixPath(p).name == 'session.moon' for p in snapshot.files):
        raise ExportError('A complete saved world needs slot.json and at least one session.moon')


def list_slots(profile):
    with open_profile(profile) as pinned:
        sessions = pinned.path / 'MoonSessions'
        try:
            with source_handle(sessions, directory=True, profile=pinned) as descriptor, os.scandir(descriptor) as entries:
                names = sorted(entry.name for entry in entries if re.fullmatch(r'slot-[0-9]+', entry.name))
        except FileNotFoundError:
            pinned.check()
            return []
        result = []
        for name in names:
            try:
                snapshot = scan(SlotSource(sessions / name, pinned))
                require_world(snapshot)
                result.append(dict(slot=name, files=len(snapshot.files), bytes=snapshot.total_bytes, state='saved-world'))
            except (ExportError, OSError) as error:
                result.append(dict(slot=name, state='unavailable', error=str(error)))
        pinned.check()
        return result


def output_path(profile, target, readonly_roots=()):
    target = Path(target).expanduser()
    # Resolve the parent separately so an existing output symlink cannot be
    # silently interpreted as permission to overwrite its destination.
    if target.is_symlink():
        raise ExportError('Output symlink refused')
    target = target.parent.resolve() / target.name
    for source in [profile, *readonly_roots]:
        if target.is_relative_to(Path(source).expanduser().resolve()):
            raise ExportError('Exports must be outside read-only source roots')
    if target.exists() or Path(str(target) + '.receipt.json').exists():
        raise ExportError('Output or receipt already exists; choose a new filename')
    return target


def info(name, directory=False):
    entry = zipfile.ZipInfo(name + ('/' if directory else ''), date_time=(1980, 1, 1, 0, 0, 0))
    entry.create_system = 3
    entry.external_attr = ((stat.S_IFDIR | 0o700) if directory else (stat.S_IFREG | 0o600)) << 16
    entry.compress_type = zipfile.ZIP_STORED if directory else zipfile.ZIP_DEFLATED
    return entry


def payload_identity(files, directories):
    return hashlib.sha256(canonical_json(dict(files=files, directories=list(directories)))).hexdigest()


def write_snapshot(slot, snapshot, archive):
    profile = slot.profile if isinstance(slot, SlotSource) else None
    slot_path = Path(slot)
    rows = {}
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as output:
        for name in snapshot.directories:
            output.writestr(info('payload/' + name, directory=True), b'')
        for name, expected in snapshot.files.items():
            path = slot_path / name
            with source_handle(path, profile=profile) as descriptor, os.fdopen(descriptor, 'rb', closefd=False) as source:
                if signature(os.fstat(source.fileno())) != expected:
                    raise SourceChanged('Save changed before copying: ' + name)
                hasher = hashlib.sha256()
                count = 0
                with output.open(info('payload/' + name), 'w', force_zip64=True) as target:
                    while chunk := source.read(CHUNK):
                        target.write(chunk)
                        hasher.update(chunk)
                        count += len(chunk)
                        if count > expected[3]:
                            raise SourceChanged('Save grew while copying: ' + name)
                if signature(os.fstat(source.fileno())) != expected or count != expected[3]:
                    raise SourceChanged('Save changed while copying: ' + name)
            rows[name] = dict(bytes=count, sha256=hasher.hexdigest())
        if scan(slot) != snapshot:
            raise SourceChanged('Saved slot changed during export')
        data = dict(schema=SCHEMA, game='Moon Sweepers', steam_app_id=5335510,
                    exporter=dict(id='world-exporter', version=__version__),
                    source_slot=slot_path.name, content='complete-native-saved-slot',
                    files=rows, directories=list(snapshot.directories),
                    payload_sha256=payload_identity(rows, snapshot.directories))
        encoded = canonical_json(data)
        if len(encoded) > MAX_MANIFEST:
            raise ExportError('Manifest exceeds the supported bound')
        output.writestr(info('manifest.json'), encoded)
    return data


def validate_manifest(data):
    if not isinstance(data, dict) or data.get('schema') != SCHEMA or data.get('steam_app_id') != 5335510:
        raise ExportError('Unsupported world archive schema/game')
    slot_name(data.get('source_slot', ''))
    files, directories = data.get('files'), data.get('directories')
    if not isinstance(files, dict) or not isinstance(directories, list) or len(files) + len(directories) > MAX_FILES:
        raise ExportError('Invalid world payload index')
    names = list(files) + directories
    if not all(isinstance(n, str) for n in names) or len({n.casefold() for n in names}) != len(names):
        raise ExportError('Duplicate or colliding payload path')
    for name in names:
        safe_name(name)
        for parent in PurePosixPath(name).parents:
            if parent.as_posix() != '.' and parent.as_posix() not in directories:
                raise ExportError('Missing declared payload directory')
    total = 0
    for name, row in files.items():
        if not isinstance(row, dict) or type(row.get('bytes')) is not int or not 0 <= row['bytes'] <= MAX_FILE:
            raise ExportError('Invalid file size: ' + name)
        if not isinstance(row.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', row['sha256']):
            raise ExportError('Invalid file checksum: ' + name)
        total += row['bytes']
    if total > MAX_TOTAL or data.get('payload_sha256') != payload_identity(files, directories):
        raise ExportError('Payload identity or size mismatch')
    if 'slot.json' not in files or not any(PurePosixPath(n).name == 'session.moon' for n in files):
        raise ExportError('Archive is missing its native saved-world files')
    return files, directories


def inspect_archive(path, verify_payload=False):
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_FILES + 1:
            raise ExportError('Archive has too many entries')
        names = [entry.filename for entry in members]
        if len(names) != len(set(names)) or names.count('manifest.json') != 1:
            raise ExportError('Duplicate archive entry or missing manifest')
        manifest = archive.getinfo('manifest.json')
        if manifest.file_size > MAX_MANIFEST:
            raise ExportError('Manifest exceeds the supported bound')
        data = unique_json(archive.read(manifest))
        files, directories = validate_manifest(data)
        expected = {'manifest.json'} | {'payload/' + n for n in files} | {'payload/' + n + '/' for n in directories}
        if set(names) != expected:
            raise ExportError('Archive contents differ from the manifest')
        for member in members:
            kind = stat.S_IFMT(member.external_attr >> 16)
            if kind not in (0, stat.S_IFDIR if member.is_dir() else stat.S_IFREG) or member.flag_bits & 1:
                raise ExportError('Special/encrypted archive entry refused')
            if member.is_dir() and member.file_size:
                raise ExportError('Directory entry contains data')
        if verify_payload:
            for name, row in files.items():
                member = archive.getinfo('payload/' + name)
                if member.file_size != row['bytes']:
                    raise ExportError('Archive size mismatch: ' + name)
                count = 0
                hasher = hashlib.sha256()
                with archive.open(member) as stream:
                    while chunk := stream.read(CHUNK):
                        count += len(chunk)
                        if count > row['bytes']:
                            raise ExportError('Archive payload exceeds its declared size')
                        hasher.update(chunk)
                if count != row['bytes'] or hasher.hexdigest() != row['sha256']:
                    raise ExportError('Archive checksum mismatch: ' + name)
            if files['slot.json']['bytes'] > 256 * 1024:
                raise ExportError('Native slot metadata exceeds the supported bound')
            if not isinstance(unique_json(archive.read('payload/slot.json')), dict):
                raise ExportError('Native slot metadata must be a JSON object')
    return dict(schema=data['schema'], source_slot=data['source_slot'], files=len(files),
                bytes=sum(row['bytes'] for row in files.values()), payload_sha256=data['payload_sha256'],
                exporter=data.get('exporter'), payload_verified=verify_payload)


def publish_file(temporary, destination):
    # Both paths are in the same directory. Linking publishes complete bytes
    # atomically and refuses a racing writer instead of replacing their file.
    os.link(temporary, destination)


def export_world(profile, slot, destination, *, readonly_roots=(), attempts=3, settle_ms=250):
    with open_profile(profile) as pinned:
        return _export_world(pinned, slot, destination, readonly_roots=readonly_roots,
                             attempts=attempts, settle_ms=settle_ms)


def _export_world(pinned, slot, destination, *, readonly_roots=(), attempts=3, settle_ms=250):
    profile, source = locate_slot(pinned, slot)
    target = output_path(profile, destination, readonly_roots)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not 1 <= attempts <= 5 or not 0 <= settle_ms <= 5000:
        raise ExportError('Invalid bounded snapshot retry settings')
    receipt_path = Path(str(target) + '.receipt.json')
    error = None
    for attempt in range(1, attempts + 1):
        descriptor, temporary = tempfile.mkstemp(prefix='.moon-world-', dir=target.parent)
        os.close(descriptor)
        temporary = Path(temporary)
        published = False
        try:
            before = scan(source)
            require_world(before)
            time.sleep(settle_ms / 1000)
            if scan(source) != before:
                raise SourceChanged('Saved slot changed during the settle interval')
            write_snapshot(source, before, temporary)
            verified = inspect_archive(temporary, verify_payload=True)
            if scan(source) != before:
                raise SourceChanged('Saved slot changed before publication')
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            archive_sha = sha(temporary)
            result = dict(verified, schema='moon-world-export-receipt/v1', success=True,
                          archive=str(target), archive_sha256=archive_sha,
                          source_profile=str(profile), source_slot=slot,
                          created_utc=datetime.now(timezone.utc).isoformat(), attempt=attempt,
                          source_stable_during_snapshot=True, source_writes=False)
            # Two-file publication is recoverable, not a filesystem transaction.
            # A caught sidecar failure removes only the exact archive we own.
            pinned.check()
            publish_file(temporary, target)
            published = True
            fd, sidecar = tempfile.mkstemp(prefix='.moon-receipt-', dir=target.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(canonical_json(result));stream.flush();os.fsync(stream.fileno())
                publish_file(sidecar, receipt_path)
            finally:
                Path(sidecar).unlink(missing_ok=True)
            return result
        except SourceChanged as changed:
            error = changed
        except BaseException:
            if published:
                try:
                    current, owned = target.lstat(), temporary.stat()
                    if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                        target.unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            temporary.unlink(missing_ok=True)
    raise SourceChanged(f'No stable saved-world snapshot after {attempts} attempts: {error}')
