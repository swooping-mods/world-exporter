"""World Exporter: list, export, inspect and verify native saved-world slots."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile
import zlib
from . import __version__
from .archive import ExportError, export_world, inspect_archive, list_slots, canonical_json


def load_config(path):
    if path is None or not path.exists():
        return dict(schema='moon-companion-config/v1', source_profile=None,
                    output_dir=str(Path.cwd() / 'world-exports'), readonly_roots=[])
    data = json.loads(path.read_text())
    if data.get('schema') != 'moon-companion-config/v1':
        raise ExportError('Unsupported exporter configuration schema')
    return data


def validate_source(data, source):
    source = Path(source).expanduser().resolve()
    policy = data.get('save_policy')
    if policy:
        root = Path(policy['profile_root']).resolve()
        relative = source.relative_to(root).parts if source.is_relative_to(root) else ()
        if len(relative) != 5 or relative[1:] != ('AppData', 'LocalLow', 'DragDropGames', 'MoonSweepers'):
            raise ExportError('Save separation: choose the ' + policy['storefront'] + ' profile from this lane\'s own prefix')
    return source


def configure(path, data, source=None, output=None):
    if path is None:
        raise ExportError('Use --config FILE for persistent configuration')
    data = dict(data)
    if source is not None:
        source = validate_source(data, source)
        if not (source / 'MoonSessions').is_dir():
            raise ExportError('Choose the MoonSweepers profile containing MoonSessions')
        data['source_profile'] = str(source)
    if output is not None:
        data['output_dir'] = str(output.expanduser().resolve())
    destination = Path(data['output_dir'])
    for root in [data.get('source_profile'), *data.get('readonly_roots', [])]:
        if root and destination.is_relative_to(Path(root).resolve()):
            raise ExportError('Output directory must be outside read-only sources')
        if root and path.resolve().is_relative_to(Path(root).resolve()):
            raise ExportError('Configuration must be outside read-only sources')
    if path.is_symlink():
        raise ExportError('Configuration symlink refused')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.world-config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical_json(data));stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--config', type=Path)
    commands = parser.add_subparsers(dest='action', required=True)
    c = commands.add_parser('configure', help='show or change this mod’s persistent source/output settings')
    c.add_argument('--source', type=Path);c.add_argument('--output-dir', type=Path)
    c = commands.add_parser('list', help='list saved slots without loading a world')
    c.add_argument('--source', type=Path)
    c = commands.add_parser('export', help='export a complete last-saved slot')
    c.add_argument('--source', type=Path);c.add_argument('--slot', required=True)
    c.add_argument('--output', type=Path)
    for action in ['inspect', 'verify']:
        commands.add_parser(action).add_argument('archive', type=Path)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.action == 'configure':
            result = configure(args.config, config, args.source, args.output_dir) if args.source or args.output_dir else config
        elif args.action in ('inspect', 'verify'):
            result = inspect_archive(args.archive, verify_payload=args.action == 'verify')
        else:
            source = args.source or config.get('source_profile')
            if source is None:
                raise ExportError('Choose a profile with configure --source DIRECTORY or --source DIRECTORY; candidates: ' + repr(config.get('profile_candidates', [])))
            source = validate_source(config, source)
            if args.action == 'list':
                result = dict(mod='world-exporter', version=__version__, source_profile=str(source), slots=list_slots(source))
            else:
                now = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                target = args.output or Path(config['output_dir']) / f'{args.slot}-{now}.msworld.zip'
                result = export_world(source, args.slot, target, readonly_roots=config.get('readonly_roots', []))
        print(json.dumps(result, indent=2, ensure_ascii=False));return 0
    except (ExportError, OSError, ValueError, zipfile.BadZipFile, RuntimeError, zlib.error, EOFError) as error:
        print(json.dumps(dict(success=False, mod='world-exporter', error=str(error))), file=sys.stderr);return 1
    except KeyboardInterrupt:
        print('Export cancelled.', file=sys.stderr);return 130


if __name__ == '__main__':
    raise SystemExit(main())
