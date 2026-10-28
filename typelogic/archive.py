"""Library limit and archive: at most LIMIT decisions are kept; then every decision is compressed into one archive.

The archive goes to the data folder of the project (<project>/data), named after the day it is made (DD-MM-YYYY.rar,
then DD-MM-YYYY-2.rar, ... on the same day). It holds one JSON file per decision (the full stored record with its
integrity check, corrections and audit trail) under decisions/<workspace>/, plus manifest.json with the SHA-256 of
every file. RAR needs WinRAR's command-line tool (Rar.exe, or rar on Linux and macOS: PATH, the usual install folders
or TYPELOGIC_RAR); without it the archive is a .zip with the same content. The decisions are removed from the library
only after the archive has been written and tested.
"""
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

from . import __version__

LIMIT = 200


class LibraryFull(Exception):
    """Raised before an evaluation when the library already holds LIMIT decisions."""


def folder(root):
    return Path(root) / 'data'


def rar_executable():
    """WinRAR's command-line tool, or None."""
    configured = os.environ.get('TYPELOGIC_RAR')
    if configured:
        return configured if Path(configured).is_file() else None
    for name in ('rar', 'Rar.exe', 'rar.exe'):
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == 'win32':
        for base in {os.environ.get(k) for k in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)')} - {None}:
            candidate = Path(base) / 'WinRAR' / 'Rar.exe'
            if candidate.is_file():
                return str(candidate)
    return None


def next_path(target, extension, today=None):
    """<target>/DD-MM-YYYY.<extension>, with -2, -3, ... when that name is taken."""
    base = (today or date.today()).strftime('%d-%m-%Y')
    path, n = Path(target) / f'{base}.{extension}', 2
    while path.exists():
        path, n = Path(target) / f'{base}-{n}.{extension}', n + 1
    return path


def info(store, root):
    """What the library limit and the next archive look like (GET /api/archive)."""
    count = store.count()
    kind = 'rar' if rar_executable() else 'zip'
    path = next_path(folder(root), kind)
    return {'count': count, 'limit': LIMIT, 'full': count >= LIMIT, 'format': kind, 'folder': str(folder(root)),
            'name': path.name, 'note': None if kind == 'rar' else 'WinRAR (Rar.exe) was not found, so the archive will be a .zip file'}


def check_room(store, root):
    if store.count() >= LIMIT:
        found = info(store, root)
        raise LibraryFull(f'The library holds {LIMIT} decisions, the maximum. Archive them to continue: every decision is '
                          f'compressed into {Path(found["folder"]) / found["name"]} and removed from the library.')


def _safe(name):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', name).strip('._') or 'workspace'


def _related(store, ids):
    out = {key: {'feedback': [], 'audit': []} for key in ids}
    for table in ('feedback', 'audit'):
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for row in store.db.execute(f"SELECT * FROM {table} WHERE decision_id IN ({','.join('?' * len(chunk))}) ORDER BY rowid", chunk):
                item = dict(row)
                for name in ('target', 'details'):
                    if isinstance(item.get(name), str):
                        try:
                            item[name] = json.loads(item[name])
                        except ValueError:
                            pass
                out[item['decision_id']][table].append(item)
    return out


def _compress(source, target, kind):
    files = sorted(p for p in source.rglob('*') if p.is_file())
    if kind == 'rar':
        exe = rar_executable()
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        # a: add; -r: recurse; -ep1: paths relative to the source folder; -m5: best compression; -idq: quiet; -y: yes to all
        subprocess.run([exe, 'a', '-r', '-ep1', '-m5', '-idq', '-y', str(target), str(source) + os.sep + '*'],
                       check=True, capture_output=True, timeout=1800, creationflags=flags)
        subprocess.run([exe, 't', '-idq', str(target)], check=True, capture_output=True, timeout=1800, creationflags=flags)
    else:
        with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in files:
                archive.write(path, path.relative_to(source).as_posix())
        with zipfile.ZipFile(target) as archive:
            if archive.testzip() is not None:
                raise RuntimeError('The archive failed its integrity test')


def archive(store, root, today=None):
    """Compress every decision of the library into <project>/data and remove them from the library."""
    target_folder = folder(root)
    target_folder.mkdir(parents=True, exist_ok=True)
    kind = 'rar' if rar_executable() else 'zip'
    with store.lock:
        records = store.decisions(all_rows=True)
        if not records:
            raise ValueError('The library is empty; there is nothing to archive')
        ids = [r['id'] for r in records]
        related = _related(store, ids)
        target = next_path(target_folder, kind, today)
        with tempfile.TemporaryDirectory(prefix='typelogic-archive-', dir=target_folder) as tmp:
            source = Path(tmp) / 'content'
            files = {}
            for r in records:
                path = source / 'decisions' / _safe(r['space']) / f'{r["id"]}.json'
                path.parent.mkdir(parents=True, exist_ok=True)
                data = json.dumps({**r, **related[r['id']]}, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
                path.write_bytes(data)
                files[path.relative_to(source).as_posix()] = hashlib.sha256(data).hexdigest()
            manifest = {'format': 'typelogic-archive-v1', 'app_version': __version__, 'created': datetime.now(timezone.utc).isoformat(),
                        'decisions': len(records), 'workspaces': sorted({r['space'] for r in records}), 'files_sha256': files}
            (source / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            try:
                _compress(source, target, kind)
            except Exception:
                target.unlink(missing_ok=True)
                raise
        with store.db:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ','.join('?' * len(chunk))
                for table, column in (('feedback', 'decision_id'), ('audit', 'decision_id'), ('decisions', 'id')):
                    store.db.execute(f'DELETE FROM {table} WHERE {column} IN ({marks})', chunk)
    return {'path': str(target), 'name': target.name, 'format': kind, 'decisions': len(records), 'external_action_executed': False}
