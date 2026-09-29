#!/usr/bin/env python3
"""Check local Markdown link targets in Git's tracked documentation (no network)."""
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT):
    paths = subprocess.check_output(['git', 'ls-files', '-z', '--', '*.md'], cwd=root).decode().split('\0')
    errors = []
    for name in paths:
        path = root / name
        if not name or not path.is_file():
            continue
        source = re.sub(r'```.*?```', '', path.read_text(), flags=re.S)
        for target in re.findall(r'!?\[[^\]]*\]\(([^\s)]+)(?:\s+"[^"]*")?\)', source):
            target = target.strip('<>')
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            destination = (root if parsed.path.startswith('/') else path.parent) / unquote(parsed.path.lstrip('/'))
            if not destination.exists():
                errors.append(f'{name}: missing local target {target}')
    return errors


if __name__ == '__main__':
    errors = check()
    for error in errors:
        print(error)
    print(f'Local documentation link targets: {len(errors)} errors (anchors/external URLs not checked).')
    raise SystemExit(bool(errors))
