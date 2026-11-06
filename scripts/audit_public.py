"""Audit only Git-indexed files, including embedded 3MF metadata, before publishing.

Run after git add. Reports file/rule names, never matching secrets. This is a
targeted publication check; it is not a substitute for reviewing staged files.
"""
import io
import json
from pathlib import Path
import re
import subprocess
import zipfile

from PIL import Image

RULES = {
    'absolute user directory': re.compile(rb'(?:/home/|/Users/)[A-Za-z0-9_.-]+|[A-Z]:[\\\\/]Users[\\\\/]', re.I),
    'GitHub credential': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    'private key': re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
    'private network address': re.compile(rb'\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b'),
}
PRIVATE_KEYS = {'password', 'token', 'api_key', 'apikey', 'printhost_apikey',
                'printhost_password', 'printhost_user', 'print_host', 'host', 'serial_port'}


def check_json(value, location, failures):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in PRIVATE_KEYS and item not in (None, '', [], {}):
                failures.append((location, 'nonempty connection or credential field: '+key))
            check_json(item, location, failures)
    elif isinstance(value, list):
        for item in value: check_json(item, location, failures)


def check(name, data, failures):
    if name.endswith('.3mf'):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for item in z.namelist(): check(name+'!'+item, z.read(item), failures)
        return
    if name.endswith('.png'):
        with Image.open(io.BytesIO(data)) as image:
            if image.info: failures.append((name, 'PNG contains metadata; review and strip before publication'))
        return
    for rule, pattern in RULES.items():
        if pattern.search(data): failures.append((name, rule))
    if name.endswith('.json'):
        check_json(json.loads(data), name, failures)


def main():
    files = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
    failures, count = [], 0
    forbidden = {'.venv', '__pycache__', 'artifacts', 'research', 'user-data', 'slicer-data', '.env'}
    for name in filter(None, files):
        path = Path(name)
        if set(path.parts) & forbidden or path.suffix in ('.gcode', '.bgcode', '.log', '.pyc') or path.name.startswith('core.'):
            failures.append((name, 'local-only file in index'))
        data = subprocess.check_output(['git', 'show', ':'+name])
        check(name, data, failures)
        count += 1
    if failures:
        for name, rule in failures: print(f'FAIL {name}: {rule}')
        raise SystemExit(1)
    print(f'PASS: {count} indexed files checked, including all embedded 3MF entries and PNG metadata.')


if __name__ == '__main__': main()
