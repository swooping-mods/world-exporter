#!/usr/bin/env python3
"""Rebuild this exported mod using installed tools, without the workshop or game."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parent


def contained(root, name):
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).is_absolute() or '..' in parts or '\\' in name:
        raise ValueError('Unsafe relative file: '+name)
    path = root
    for part in parts:
        path = path/part
        if path.is_symlink():
            raise ValueError('Symlink refused: '+name)
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_inventory():
    result = {}
    if (ROOT/'source').is_symlink():
        raise ValueError('Symlink source root refused')
    for p in sorted((ROOT/'source').rglob('*')):
        if p.is_symlink():
            raise ValueError('Symlink in source')
        if p.is_file():
            result[p.relative_to(ROOT/'source').as_posix()] = digest(p)
    return result


def validate_sources():
    expected = json.loads((ROOT/'provenance.json').read_text())['source_files']
    actual = source_inventory()
    if actual != {name: row['export_sha256'] for name, row in expected.items()}:
        raise ValueError('Source differs from the recorded export; update provenance before building')
    return actual


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, data in sorted(files.items()):
            contained(ROOT, name)
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return output.getvalue()


def build(destination):
    inputs = validate_sources()
    spec = json.loads((ROOT/'build.json').read_text())
    destination = destination.absolute()
    if destination != destination.resolve():
        raise ValueError('Non-canonical build output refused')
    destination.relative_to(ROOT/'.local')
    if destination.is_symlink() or any(p.is_symlink() for p in destination.parents):
        raise ValueError('Symlink build output refused')
    if destination.exists():
        raise ValueError('Build output already exists; choose a fresh .local directory')
    destination.mkdir(parents=True)
    if spec['kind'] == 'native':
        for tool in ('clang', 'llvm-dlltool', 'lld-link'):
            if not shutil.which(tool):
                raise ValueError('Install '+tool+' explicitly; this command downloads nothing')
        subprocess.run(['llvm-dlltool', '-m', 'i386:x86-64', '-d',
                        str(ROOT/'vendored/kernel32.def'), '-l', str(destination/'kernel32.lib')], check=True)
        subprocess.run(['clang', '--target=x86_64-pc-windows-msvc', '-O2', '-Wall', '-Wextra',
                        '-Werror', '-ffreestanding', '-fno-builtin', '-fno-stack-protector',
                        '-funwind-tables', '-c', str(contained(ROOT/'source', spec['entry'])),
                        '-o', str(destination/'mod.obj')], check=True)
        binary = destination/'mod.dll'
        subprocess.run(['lld-link', '/dll', '/noentry', '/nodefaultlib', '/machine:x64',
                        '/export:MoonLab_ModStart', '/timestamp:0', '/out:'+str(binary),
                        str(destination/'mod.obj'), str(destination/'kernel32.lib')], check=True)
        packages = sorted((ROOT/'content').glob('*.msmod'))
        if packages:
            if len(packages) != 1:
                raise ValueError('Expected one package template')
            with zipfile.ZipFile(packages[0]) as z:
                files = {name: z.read(name) for name in z.namelist()}
            manifest = json.loads(files.pop('manifest.json'))
            files[manifest['entry']] = binary.read_bytes()
            manifest['files'] = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
            manifest['verification'] = {'build': 'Locally rebuilt from exported sources; gameplay is unqualified for these bytes.'}
            files['manifest.json'] = (json.dumps(manifest, indent=2)+'\n').encode()
            (destination/packages[0].name).write_bytes(archive(files))
    elif spec['kind'] == 'companion':
        module = spec['id'].replace('-', '_')
        parent = ROOT/'source/mods'/spec['id']
        files = {p.relative_to(parent).as_posix(): p.read_bytes()
                 for p in (parent/module).rglob('*') if p.is_file()}
        files['__main__.py'] = ('from '+module+'.__main__ import main\nraise SystemExit(main())\n').encode()
        (destination/(spec['id']+'.pyz')).write_bytes(archive(files))
    else:
        raise ValueError('Unsupported build kind')
    if source_inventory() != inputs:
        raise ValueError('Sources changed while building')
    result = {'schema': 'moon-independent-build/v1', 'id': spec['id'], 'source_files': inputs,
              'outputs': {p.name: digest(p) for p in destination.iterdir() if p.is_file()},
              'gameplay_qualified': False, 'game_or_mod_executed': False}
    (destination/'receipt.json').write_text(json.dumps(result, indent=2)+'\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Verify exported source identity without compiling')
    parser.add_argument('--output', type=Path, default=ROOT/'.local/build')
    args = parser.parse_args()
    if args.check:
        print(json.dumps({'source_files_checked': len(validate_sources()), 'compiled': False}))
    else:
        print(json.dumps(build(args.output), indent=2))


if __name__ == '__main__':
    main()
