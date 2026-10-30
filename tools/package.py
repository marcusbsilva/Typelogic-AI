"""Build the TypeLogic release on Windows (development only; Python standard library).

    python tools/package.py [--skip-build]

1. `cargo build --release` (unless --skip-build).
2. release/TypeLogic-<version>-windows-x64.zip, one folder `TypeLogic/`:
     TypeLogic.exe, config/models.lock.json, runtime/llama/ (llama.cpp of models.lock.json with the patched llama.dll),
     README.md, LICENSE, NOTICE, licenses/
   The runtime is taken from dist/runtime/llama (installed by `TypeLogic setup`) after its files are checked: the
   marker names the pinned release and llama.dll has the SHA-256 of the patched build in models.lock.json.
3. The release assets beside it: the specialist GGUF and llama.dll (both checked against models.lock.json), and
   SHA256SUMS.txt for every file of release/.
Models are not in the archive: on the first start TypeLogic.exe offers to download them (setup).
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'release'
EXE = ROOT / 'target' / 'release' / 'TypeLogic.exe'
RUNTIME = ROOT / 'dist' / 'runtime' / 'llama'


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def version():
    for line in (ROOT / 'Cargo.toml').read_text(encoding='utf-8').splitlines():
        if line.startswith('version = '):
            return line.split('"')[1]
    raise SystemExit('no version in Cargo.toml')


def cargo():
    found = shutil.which('cargo') or str(Path.home() / '.cargo' / 'bin' / 'cargo.exe')
    if not Path(found).is_file():
        raise SystemExit('cargo not found: install Rust (rustup)')
    return found


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--skip-build', action='store_true')
    p.add_argument('--models', type=Path, default=ROOT / 'models', help='folder with the specialist GGUF')
    args = p.parse_args()
    if os.name != 'nt':
        raise SystemExit('the release is built on Windows (TypeLogic.exe and the Vulkan runtime)')
    ver = version()
    lock = json.loads((ROOT / 'config' / 'models.lock.json').read_text(encoding='utf-8'))
    if not args.skip_build:
        subprocess.run([cargo(), 'build', '--release'], cwd=ROOT, check=True)
    out = subprocess.run([str(EXE), 'version'], capture_output=True, text=True, check=True).stdout
    if f'TypeLogic {ver}' not in out:
        raise SystemExit(f'{EXE} reports {out.strip()!r}, expected {ver}')

    # the runtime: the pinned release with the patched llama.dll
    marker = json.loads((RUNTIME / 'llama-runtime.json').read_text(encoding='utf-8'))
    pinned = lock['runtime']['llama.cpp']
    if marker.get('sha256') != pinned['sha256']:
        raise SystemExit(f'{RUNTIME} is not the llama.cpp release of models.lock.json; run TypeLogic setup in dist/')
    patched = lock['runtime']['llama-dll-patched']
    if sha256(RUNTIME / 'llama.dll') != patched['sha256']:
        raise SystemExit(f'{RUNTIME / "llama.dll"} is not the patched build of models.lock.json')

    OUT.mkdir(exist_ok=True)
    name = f'TypeLogic-{ver}-windows-x64'
    archive = OUT / f'{name}.zip'
    files = [(EXE, 'TypeLogic.exe'), (ROOT / 'config' / 'models.lock.json', 'config/models.lock.json')]
    for f in ('README.md', 'LICENSE', 'NOTICE'):
        files.append((ROOT / f, f))
    files += [(f, f'licenses/{f.name}') for f in sorted((ROOT / 'licenses').iterdir()) if f.is_file()]
    files += [(f, f'runtime/llama/{f.name}') for f in sorted(RUNTIME.iterdir()) if f.is_file() and not f.name.endswith('.release')]
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for src, dest in files:
            z.write(src, f'TypeLogic/{dest}')
    with zipfile.ZipFile(archive) as z:
        bad = z.testzip()
        if bad:
            raise SystemExit(f'{archive}: {bad} is damaged')
        listed = z.namelist()
    print(f'{archive.name}: {len(listed)} files, {archive.stat().st_size / 2**20:.1f} MiB')

    # release assets: the files models.lock.json points to
    spec = lock['built']['spec-q8']
    gguf = args.models / spec['file']
    if not gguf.is_file() or gguf.stat().st_size != spec['bytes'] or sha256(gguf) != spec['sha256']:
        raise SystemExit(f'{gguf} is missing or differs from models.lock.json')
    for src in (gguf, RUNTIME / 'llama.dll'):
        dest = OUT / src.name
        if dest.exists():
            dest.unlink()
        try:
            os.link(src, dest)
        except OSError:
            shutil.copy2(src, dest)
    sums = [f'{sha256(f)}  {f.name}' for f in sorted(OUT.iterdir()) if f.is_file() and f.name != 'SHA256SUMS.txt']
    (OUT / 'SHA256SUMS.txt').write_text('\n'.join(sums) + '\n', encoding='utf-8')
    print('\n'.join(sums))


if __name__ == '__main__':
    sys.exit(main())
