#!/usr/bin/env python3
"""Publish a verified single application image and its minimal deployment files."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'ghcr.io/sakuralovesmile/studytool-connected-app'


def run(*command):
    return subprocess.run(command, check=True, text=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--smoke', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-z0-9][a-z0-9.-]*)?', args.version):
        parser.error('invalid release version')
    if run('git', 'status', '--porcelain').stdout.strip():
        parser.error('publishing requires a clean source checkout')
    revision = run('git', 'rev-parse', 'HEAD').stdout.strip()
    smoke = json.loads(args.smoke.read_text())
    if smoke['result'] != 'passed' or smoke['image'] != args.image or not smoke['cleanup_verified']:
        parser.error('image must pass its isolated Nginx/application check first')
    info = json.loads(run('docker', 'image', 'inspect', args.image).stdout)[0]
    if info['Config']['Labels'].get('org.opencontainers.image.revision') != revision:
        parser.error('image source revision mismatch')
    if info['Architecture'] != 'amd64' or info['Config']['User'] != 'app':
        parser.error('expected a Linux x64 non-root application image')
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        parser.error('output must be empty')
    tag = f'{REPOSITORY}:{args.version}'
    sha_tag = f'{REPOSITORY}:sha-{revision}'
    for reference in (tag, sha_tag):
        run('docker', 'tag', args.image, reference)
        run('docker', 'push', reference)
    info = json.loads(run('docker', 'image', 'inspect', tag).stdout)[0]
    digests = [d for d in info['RepoDigests'] if d.startswith(REPOSITORY + '@sha256:')]
    if len(digests) != 1:
        raise RuntimeError('expected one published application digest')
    digest = digests[0]
    for name in ('compose.app.yaml', 'app.env.example'):
        template = (ROOT / 'deploy' / name).read_text()
        old = REPOSITORY + ':0.1.0'
        if template.count(old) != 1:
            raise RuntimeError(f'unexpected image template in {name}')
        (args.output / name).write_text(template.replace(old, digest))
    (args.output / 'NGINX.md').write_bytes((ROOT / 'deploy/NGINX.md').read_bytes())
    (args.output / 'APP_BUILD_SMOKE.json').write_bytes(args.smoke.read_bytes())
    manifest = {'version': args.version, 'source_revision': revision, 'platform': 'linux/amd64',
                'image': {'tag': tag, 'source_tag': sha_tag, 'digest': digest},
                'registry_pushed': True, 'production_deployed': False, 'contains_caddy': False}
    (args.output / 'APP_MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (args.output / 'APP_SHA256SUMS.txt').write_text(''.join(
        hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n'
        for p in sorted(args.output.iterdir()) if p.is_file()))
    print(f'Published {digest}; first publication requires making this package Public.')


if __name__ == '__main__':
    main()
