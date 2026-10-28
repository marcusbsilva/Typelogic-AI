"""Decision templates: an example case, suggested tags, an evidence schema and decisions.

Templates are JSON files (format typelogic-template-v1) read from three places, later ones replacing earlier ones with
the same id:
  templates/builtin/   shipped with TypeLogic (read-only for the app)
  templates/custom/    saved from the app into the project folder, to share them (for example through git)
  <data>/templates/    the same saved templates in the user's data folder (~/.typelogic-ai/templates)
Saving writes both the project copy and the data-folder copy.
"""
import json
import os
from pathlib import Path
import re

from . import casefmt

FORMAT = 'typelogic-template-v1'
ID = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
ROOT = Path(__file__).resolve().parents[1]
FIELDS = ('id', 'title', 'description', 'case', 'tags', 'evidence_schema', 'evidence', 'decisions', 'threshold',
          'permutation_check', 'example')
LEGACY = ('rules', 'exact_checks')     # earlier template keys, ignored when a template is read
LEGACY_DECISION = ('auto_review', 'verdict')   # earlier per-decision review rules, dropped when a template is read


def folders(root=ROOT, data_dir=None):
    out = [('builtin', Path(root) / 'templates' / 'builtin'), ('project', Path(root) / 'templates' / 'custom')]
    if data_dir is not None:
        out.append(('local', Path(data_dir) / 'templates'))
    return out


def to_request(template, include_values=True):
    """A v2 request built from a template (values are marked as coming from the template)."""
    case = template.get('case') or ''
    request = {'template': template['id'], 'case': (case if isinstance(case, str) else casefmt.case_text(case)) if case else '',
               'evidence_schema': template.get('evidence_schema', []), 'decisions': template['decisions'],
               'threshold': template.get('threshold', 0.85), 'permutation_check': template.get('permutation_check', True)}
    if template.get('tags'):
        request['tags'] = list(template['tags'])
    if not include_values:
        request['case'] = ''
    request['evidence'] = {k: {'value': v, 'source': 'template', 'status': 'confirmed'}
                           for k, v in (template.get('evidence') or {}).items()} if include_values else {}
    return request


def _upgrade_decision(d):
    """Earlier templates: drop the automatic review rules (auto_review, verdict)."""
    if not isinstance(d, dict):
        return d
    return {k: v for k, v in d.items() if k not in LEGACY_DECISION}


def validate(template):
    """Checked copy of a template; raises ValueError with the first problem."""
    if not isinstance(template, dict):
        raise ValueError('A template must be a JSON object')
    unknown = set(template) - set(FIELDS) - set(LEGACY) - {'format'}
    if unknown:
        raise ValueError('Unknown template keys: ' + ', '.join(sorted(unknown)))
    tid = template.get('id')
    if not isinstance(tid, str) or not ID.match(tid):
        raise ValueError('Template id: lowercase letters, digits and -, up to 64 characters')
    title = template.get('title')
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError('Template title: 1-200 characters')
    description = template.get('description') or ''
    if not isinstance(description, str) or len(description) > 1000:
        raise ValueError('Template description: up to 1,000 characters')
    out = {'format': FORMAT, 'id': tid, 'title': title.strip(), 'description': description.strip()}
    for key in ('case', 'tags', 'evidence_schema', 'evidence', 'decisions', 'threshold', 'permutation_check', 'example'):
        if key in template:
            out[key] = template[key]
    if 'decisions' not in out:
        raise ValueError('A template needs at least one decision')
    if out.get('case') and not isinstance(out['case'], str):
        out['case'] = casefmt.case_text(out['case'])     # earlier {"summary", "details"} layout
    if isinstance(out['decisions'], list):
        out['decisions'] = [_upgrade_decision(d) for d in out['decisions']]
    if 'tags' in out:
        out['tags'] = casefmt.validate_tags(out['tags'])
    request = to_request(out)
    request['case'] = request['case'] or out['title']
    casefmt.normalize(request)              # same checks as a real request
    return out


def load_all(root=ROOT, data_dir=None):
    """[{id, title, description, origin, path, template}] in id order; broken files are reported, not raised."""
    found, errors = {}, []
    for origin, folder in folders(root, data_dir):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob('*.json')):
            try:
                t = validate(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, ValueError) as exc:
                errors.append({'path': str(path), 'error': str(exc)[:300]})
                continue
            found[t['id']] = {'id': t['id'], 'title': t['title'], 'description': t['description'], 'origin': origin,
                              'path': str(path), 'template': t}
    return {'templates': sorted(found.values(), key=lambda r: (r['origin'] != 'builtin', r['title'].lower())), 'errors': errors}


def get(template_id, root=ROOT, data_dir=None):
    for row in load_all(root, data_dir)['templates']:
        if row['id'] == template_id:
            return row['template']
    raise ValueError(f'Template not found: {template_id}')


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def save(template, root=ROOT, data_dir=None, overwrite=False):
    """Save a user template to templates/custom/ (project) and <data>/templates/. Built-in ids cannot be replaced."""
    t = validate(template)
    if (Path(root) / 'templates' / 'builtin' / f'{t["id"]}.json').exists():
        raise ValueError(f'"{t["id"]}" is a built-in template; save it under another id')
    targets = [Path(root) / 'templates' / 'custom' / f'{t["id"]}.json']
    if data_dir is not None:
        targets.append(Path(data_dir) / 'templates' / f'{t["id"]}.json')
    if not overwrite and any(p.exists() for p in targets):
        raise ValueError(f'A template with id "{t["id"]}" already exists; choose another id or confirm the overwrite')
    for path in targets:
        _write(path, t)
    return {'id': t['id'], 'saved': [str(p) for p in targets]}
