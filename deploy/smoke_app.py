#!/usr/bin/env python3
"""Check the single application image behind a disposable HTTPS Nginx proxy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from smoke_images import EMBEDDING_FIXTURE, PROBE, RESTART_PROBE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--image')
    source.add_argument('--deployment', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.deployment / 'APP_MANIFEST.json').read_text()) if args.deployment else None
    reference = manifest['image']['digest'] if manifest else args.image
    project = 'studytool-app-smoke-' + uuid.uuid4().hex[:10]
    host = os.environ.get('DOCKER_HOST') or subprocess.check_output(
        ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
    with tempfile.TemporaryDirectory(prefix=project) as temporary:
        root = Path(temporary)
        anonymous = root / 'docker'
        anonymous.mkdir()
        config = {'auths': {}}
        plugins = Path(os.environ.get('DOCKER_CONFIG', str(Path.home() / '.docker'))) / 'cli-plugins'
        if plugins.is_dir():
            config['cliPluginsExtraDirs'] = [str(plugins.resolve())]
        (anonymous / 'config.json').write_text(json.dumps(config))
        docker = ['docker', '--config', str(anonymous), '--host', host]

        def run(*command, **kwargs):
            try:
                return subprocess.run(command, text=True, capture_output=True, check=True, **kwargs)
            except subprocess.CalledProcessError as error:
                print(error.stdout or '')
                print(error.stderr or '')
                raise

        if manifest:
            print('Pulling the published application anonymously...', flush=True)
            run(*docker, 'pull', '--platform', 'linux/amd64', reference)
            inspected = json.loads(run(*docker, 'image', 'inspect', reference).stdout)[0]
            assert reference in inspected['RepoDigests'] and inspected['Architecture'] == 'amd64'
            run(*docker, 'pull', '--platform', 'linux/amd64', manifest['image']['tag'])
            tagged = json.loads(run(*docker, 'image', 'inspect', manifest['image']['tag']).stdout)[0]
            assert reference in tagged['RepoDigests'] and tagged['Id'] == inspected['Id']
        # This proxy exists only in the disposable test network, not in the delivered Compose.
        nginx_image = 'nginx:1.28-alpine'
        run(*docker, 'pull', nginx_image)
        run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=studytool-smoke', '-addext', 'subjectAltName=DNS:studytool-smoke',
            '-keyout', str(root / 'key.pem'), '-out', str(root / 'cert.pem'))
        (root / 'nginx.conf').write_text('''events {}
http {
    client_max_body_size 105m;
    server {
        listen 8443 ssl;
        ssl_certificate /fixture/cert.pem;
        ssl_certificate_key /fixture/key.pem;
        location = /healthz { proxy_pass http://app:8000/health; }
        location / {
            proxy_pass http://app:8000;
            proxy_set_header Host $http_host;
            proxy_set_header X-Forwarded-For $remote_addr;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 600s;
            proxy_buffering off;
        }
    }
}
''')
        environment = {'HOST': '0.0.0.0', 'PORT': '8000', 'PUBLIC_MODE': 'true',
                       'PUBLIC_ORIGIN': 'https://studytool-smoke:8443', 'AUTH_COOKIE_SECURE': 'true',
                       'DEMO_MODE': 'false', 'DATA_DIR': '/data', 'LOG_DIR': '/data/logs',
                       'OCR_MODE': 'vision_llm', 'EMBEDDING_MODE': 'remote',
                       'LLM_BASE_URL': 'http://127.0.0.1:9/v1', 'LLM_API_KEY': 'isolated-unused',
                       'OCR_BASE_URL': 'http://127.0.0.1:9/v1', 'OCR_API_KEY': 'isolated-unused',
                       'EMBEDDING_BASE_URL': 'http://model-fixture:9000/v1',
                       'EMBEDDING_API_KEY': 'isolated-fixture'}
        app = {'image': reference, 'platform': 'linux/amd64', 'environment': environment,
               'volumes': ['data:/data']}
        if manifest:
            # Render the exact delivered service, then replace only test data and network boundaries.
            (root / '.env').write_text('PUBLIC_ORIGIN=https://studytool-smoke:8443\nRAG_DATA_DIR=/unused-smoke-data\n')
            (root / 'source.yaml').write_bytes((args.deployment / 'compose.yaml').read_bytes())
            clean_env = {k: v for k, v in os.environ.items()
                         if k not in {'APP_IMAGE', 'PUBLIC_ORIGIN', 'APP_PORT', 'RAG_DATA_DIR'}}
            rendered = json.loads(run(*docker, 'compose', '-f', str(root / 'source.yaml'),
                                     'config', '--format', 'json', env=clean_env).stdout)
            assert set(rendered['services']) == {'app'}
            app = rendered['services']['app']
            assert app['image'] == manifest['image']['latest'] and 'build' not in app
            assert app['ports'][0]['host_ip'] == '127.0.0.1'
            # The default latest alias is promoted only after this version passes.
            # Also render the supported fixed-version override and run that exact tag.
            version_env = dict(clean_env, APP_IMAGE=manifest['image']['tag'])
            fixed = json.loads(run(*docker, 'compose', '-f', str(root / 'source.yaml'),
                                  'config', '--format', 'json', env=version_env).stdout)
            app = fixed['services']['app']
            assert app['image'] == manifest['image']['tag']
            app['environment'] = environment
            app['ports'] = []
            app['volumes'] = ['data:/data']
            app.pop('networks', None)
        services = {
            'app': app,
            'model-fixture': {'image': reference, 'platform': 'linux/amd64',
                              'entrypoint': ['python', '-u', '-c', EMBEDDING_FIXTURE]},
            'nginx': {'image': nginx_image, 'volumes': [f'{root}:/fixture:ro',
                       f'{root}/nginx.conf:/etc/nginx/nginx.conf:ro'],
                      'networks': {'default': {'aliases': ['studytool-smoke']}}},
        }
        (root / 'compose.json').write_text(json.dumps({'services': services, 'volumes': {'data': {}},
                                                       'networks': {'default': {'internal': True}}}))
        compose = docker + ['compose', '-p', project, '-f', str(root / 'compose.json')]
        try:
            run(*compose, 'up', '-d', '--no-build', '--pull', 'never')
            cert = (root / 'cert.pem').read_text()
            run(*compose, 'exec', '-T', 'app', 'python', '-c',
                'import sys; open("/tmp/smoke-ca.pem", "w").write(sys.stdin.read())', input=cert)
            initial = run(*compose, 'exec', '-T', 'app', 'python', '-', input=PROBE)
            static_probe = '''import os, re, shutil, ssl, urllib.request, urllib.error
assert os.geteuid() == 10001 and shutil.which('caddy') is None
origin = 'https://studytool-smoke:8443'
tls = ssl.create_default_context(cafile='/tmp/smoke-ca.pem')
html = urllib.request.urlopen(origin + '/library', context=tls).read().decode()
scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
assert scripts
for path in scripts:
 response = urllib.request.urlopen(origin + path, context=tls)
 assert 'javascript' in response.headers['Content-Type'] and response.read()
for path in ('/docs', '/redoc', '/openapi.json'):
 try: urllib.request.urlopen(origin + path, context=tls)
 except urllib.error.HTTPError as error: assert error.code == 404
 else: raise AssertionError(path + ' was exposed')
print('Non-root, no Caddy binary, anonymous JavaScript assets and hidden API documentation passed.')
'''
            run(*compose, 'exec', '-T', 'app', 'python', '-', input=static_probe)
            run(*compose, 'restart', 'app')
            restarted = run(*compose, 'exec', '-T', 'app', 'python', '-', input=RESTART_PROBE)
            evidence = {'result': 'passed', 'image': reference,
                        'anonymous_pull_verified': bool(manifest),
                        'version_tag_verified': manifest['image']['tag'] if manifest else None,
                        'default_image': manifest['image']['latest'] if manifest else None,
                        'initial': json.loads(initial.stdout.strip().splitlines()[-1]),
                        'restart': json.loads(restarted.stdout.strip().splitlines()[-1]),
                        'production_deployed': False,
                        'extra_checks': ['non-root', 'no-caddy-binary', 'anonymous-js-assets', 'api-docs-hidden'],
                        'isolation': 'temporary named data volume, internal network, no host ports, test Nginx and certificate'}
        except subprocess.CalledProcessError:
            logs = subprocess.run(compose + ['logs', '--tail', '35'], capture_output=True, text=True)
            print(logs.stdout)
            print(logs.stderr)
            raise
        finally:
            run(*compose, 'down', '-v', '--remove-orphans', '--timeout', '10')
        for kind in (['ps', '-aq'], ['volume', 'ls', '-q'], ['network', 'ls', '-q']):
            assert not run(*docker, *kind, '--filter', f'label=com.docker.compose.project={project}').stdout.strip()
        evidence['cleanup_verified'] = True
        output = args.output or args.deployment / 'APP_SMOKE.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, indent=2) + '\n')
        if manifest:
            files = sorted(p for p in args.deployment.iterdir() if p.is_file() and p.name != 'APP_SHA256SUMS.txt')
            (args.deployment / 'APP_SHA256SUMS.txt').write_text(''.join(
                hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in files))
        print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
