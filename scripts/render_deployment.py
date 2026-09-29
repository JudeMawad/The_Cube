#!/usr/bin/env python3
"""Render service templates into a new staging directory. Never install or restart."""
import argparse
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def render(component, user, home, repo, uid, output):
    if component not in {'client', 'server'}:
        raise ValueError('Unknown deployment component.')
    if user == 'root' or not re.fullmatch(r'[a-z_][a-z0-9_-]*', user) or not 1 <= uid < 2**32:
        raise ValueError('Use a non-root service account and its numeric UID.')
    for value in (home, repo):
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', value) or '..' in Path(value).parts:
            raise ValueError('Deployment paths must be absolute, without whitespace or special characters.')
    values = {'CUBE_USER': user, 'CUBE_HOME': home, 'CUBE_REPO': repo, 'CUBE_UID': str(uid)}
    source = ROOT / component / 'deploy'
    prepared = []
    for path in sorted(source.rglob('*')):
        if path.suffix not in {'.service', '.timer', '.rules', '.conf'}:
            continue
        text = path.read_text()
        for name, value in values.items():
            text = text.replace('@' + name + '@', value)
        if re.search(r'@CUBE_[A-Z_]+@', text):
            raise ValueError('Unresolved template placeholder.')
        prepared.append((path.relative_to(source), text))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    for relative, text in prepared:
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
    return len(prepared)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', choices=('client', 'server'))
    parser.add_argument('--user', required=True)
    parser.add_argument('--home', required=True)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--uid', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        count = render(**vars(args))
    except (ValueError, OSError) as error:
        parser.exit(1, f'Template rendering failed: {error}\n')
    print(f'Rendered {count} files into {args.output}. Review before manual installation.')


if __name__ == '__main__':
    main()
