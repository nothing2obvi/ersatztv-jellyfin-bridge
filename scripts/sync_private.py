"""Sync public code into the private sibling and rebuild its deployment."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess

FILES = ('bridge.py', 'Dockerfile', 'requirements.txt', 'test_bridge.py',
         'README.md', 'config.example.yml', '.dockerignore', '.gitignore', 'AGENTS.md')


def sync(target):
    source = Path(__file__).resolve().parents[1]
    target = target.resolve()
    if target == source:
        raise ValueError('Private deployment must be a separate directory')
    if not (target / 'config.yml').is_file():
        raise ValueError('Private deployment must already contain config.yml')
    # Preserve the config text exactly, apart from migrating the listening port.
    config = target / 'config.yml'
    original = config.read_text()
    updated, count = re.subn(r'^port:\s*\d+\s*(?:#.*)?$', 'port: 8121', original, flags=re.M)
    if not count:
        updated = original.rstrip() + '\nport: 8121\n'
    if updated != original:
        config.write_text(updated)
    for name in FILES:
        shutil.copy2(source / name, target / name)
    for directory in ('scripts', '.github'):
        shutil.copytree(source / directory, target / directory, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # Preserve an existing explicit container name without importing private config.
    compose_path = target / 'compose.yml'
    previous = compose_path.read_text() if compose_path.exists() else ''
    match = re.search(r'^    container_name:\s*(.+)$', previous, re.M)
    compose = (source / 'compose.yml').read_text()
    # Private deployments build locally; don't overwrite the published version tag.
    compose = re.sub(r'^    image:.*\n', '', compose, flags=re.M)
    if match:
        compose = re.sub(r'^    container_name:.*\n', '', compose, flags=re.M)
        compose = compose.replace('    build: .\n', '    build: .\n    container_name: ' + match[1] + '\n')
    compose_path.write_text(compose)
    subprocess.run(['docker', 'compose', 'up', '-d', '--build', '--remove-orphans'], cwd=target, check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('target', nargs='?', type=Path,
                        default=Path(__file__).resolve().parents[2] / 'ersatztv-jellyfin-bridge-mine')
    sync(parser.parse_args().target)
