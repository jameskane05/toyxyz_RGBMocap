"""App-wide imported characters. Projects reference these instead of reimporting."""
import json
import shutil
from pathlib import Path
from .paths import HOME
from .storage import atomic_json

ROOT = HOME / 'character_library'


def _check_id(ident):
    if not isinstance(ident, str) or len(ident) != 32 or any(c not in '0123456789abcdef' for c in ident):
        raise ValueError('Invalid character id')


def asset_dir(ident):
    _check_id(ident)
    return ROOT / ident / 'asset'


def load():
    path = ROOT / 'index.json'
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding='utf8'))
    if not isinstance(data, list):
        raise ValueError('Invalid character library')
    return data


def save(entries):
    ROOT.mkdir(parents=True, exist_ok=True)
    atomic_json(ROOT / 'index.json', entries)


def get(ident):
    _check_id(ident)
    return next((row for row in load() if row.get('id') == ident), None)


def upsert(entry):
    _check_id(entry['id'])
    rows = [row for row in load() if row.get('id') != entry['id']]
    rows.append(entry)
    save(rows)


def is_owned(ident):
    _check_id(ident)
    return (asset_dir(ident) / 'preview.npz').is_file()


def owned_asset(ident):
    """The library's own copy of a character. A project-only reference is copied in once."""
    _check_id(ident)
    dest = asset_dir(ident)
    if (dest / 'preview.npz').is_file():
        return dest
    entry = get(ident)
    external = Path(entry['external']) if entry and entry.get('external') else None
    if external is None or not (external / 'preview.npz').is_file():
        raise ValueError('Library character is missing')
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(external, dest)
    if not (dest / 'preview.npz').is_file():
        raise ValueError('Library character is missing')
    rows = load()
    for row in rows:
        if row.get('id') == ident:
            row.pop('external', None)
    save(rows)
    return dest


def rename(ident, name):
    _check_id(ident)
    name = ' '.join(str(name).split())
    if not name:
        raise ValueError('Character name is empty')
    rows = load()
    found = False
    for row in rows:
        if row.get('id') == ident:
            row['name'] = name
            found = True
            break
    if not found:
        raise ValueError('Character is not in the library')
    save(rows)
    return name


def delete(ident):
    _check_id(ident)
    folder = (ROOT / ident).resolve()
    root = ROOT.resolve()
    if folder.exists():
        if folder == root or not folder.is_relative_to(root):
            raise ValueError('Invalid character location')
        shutil.rmtree(folder)
    save([row for row in load() if row.get('id') != ident])


def set_voice(ident, voice_id, voice_name):
    _check_id(ident)
    rows = load()
    for row in rows:
        if row.get('id') == ident:
            row['voice_id'] = voice_id
            row['voice_name'] = voice_name
            save(rows)
            return
    raise ValueError('Character is not in the library')
