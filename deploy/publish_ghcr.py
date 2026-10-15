#!/usr/bin/env python3
"""Publish verified images using an existing Docker login, and prepare pull-only deployment files."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = 'ghcr.io/sakuralovesmile/studytool-connected-'


def run(*args: str, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-z0-9][a-z0-9.-]*)?', args.version):
        parser.error('version must be a release version such as 0.1.0 or 0.2.0-beta.1')
    metadata = json.loads((args.bundle / 'BUILD.json').read_text())
    smoke = json.loads((args.bundle / 'SMOKE.json').read_text())
    revision = metadata['revision']
    if metadata['working_tree_dirty'] or metadata['platform'] != 'linux/amd64':
        parser.error('publishing requires a clean Linux amd64 build')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        parser.error('invalid source revision')
    if smoke['initial']['result'] != 'passed' or smoke['restart']['result'] != 'passed':
        parser.error('both server smoke stages must pass before publishing')
    # Inspect all candidates before the first registry mutation.
    for source in metadata['images'].values():
        inspect = json.loads(run('docker', 'image', 'inspect', source, capture_output=True).stdout)[0]
        if inspect['Config']['Labels'].get('org.opencontainers.image.revision') != revision:
            parser.error('image source label does not match BUILD.json')
    target = args.output.resolve()
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        parser.error('output directory must be empty')
    images = {}
    for name in ('backend', 'frontend'):
        repository = REGISTRY + name
        tagged = f'{repository}:{args.version}'
        source_tag = f'{repository}:sha-{revision}'
        for tag in (tagged, source_tag):
            run('docker', 'tag', metadata['images'][name], tag)
            run('docker', 'push', tag)
        inspect = json.loads(run('docker', 'image', 'inspect', tagged, capture_output=True).stdout)[0]
        digests = [value for value in inspect['RepoDigests'] if value.startswith(repository + '@sha256:')]
        if len(digests) != 1:
            raise RuntimeError(f'Expected one published digest for {repository}')
        images[name] = {'tag': tagged, 'source_tag': source_tag, 'digest': digests[0]}
    compose = (ROOT / 'deploy/compose.ghcr.yaml').read_text()
    example = (ROOT / 'deploy/ghcr.env.example').read_text()
    for name, image in images.items():
        old = REGISTRY + name + ':0.1.0'
        if compose.count(old) != 1 or example.count(old) != 1:
            raise RuntimeError(f'Unexpected deployment template image reference: {old}')
        compose = compose.replace(old, image['digest'])
        example = example.replace(old, image['digest'])
    (target / 'compose.yaml').write_text(compose)
    (target / 'ghcr.env.example').write_text(example)
    (target / 'GHCR.md').write_text((ROOT / 'deploy/GHCR.md').read_text())
    manifest = {'version': args.version, 'source_revision': revision,
                'platform': metadata['platform'], 'registry_pushed': True,
                'images': images, 'server_smoke': smoke}
    (target / 'GHCR_MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    checksums = ''.join(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n'
                        for path in sorted(target.iterdir()) if path.is_file())
    (target / 'GHCR_SHA256SUMS.txt').write_text(checksums)
    print(f'Pushed images and prepared deployment files: {target}')
    print('First publication still requires setting both GitHub packages to Public and verifying anonymous pull.')


if __name__ == '__main__':
    main()
