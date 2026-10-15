#!/usr/bin/env python3
"""Anonymously pull the published deployment and exercise it with disposable Compose resources."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from smoke_images import EMBEDDING_FIXTURE, PROBE, RESTART_PROBE


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment', type=Path, required=True)
    args = parser.parse_args()
    deployment = args.deployment.resolve()
    manifest = json.loads((deployment / 'GHCR_MANIFEST.json').read_text())
    project = 'studytool-ghcr-smoke-' + uuid.uuid4().hex[:10]
    host = os.environ.get('DOCKER_HOST') or subprocess.check_output(
        ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
    with tempfile.TemporaryDirectory(prefix=project) as temporary:
        root = Path(temporary)
        anonymous = root / 'anonymous-docker'
        anonymous.mkdir()
        # Docker Desktop installs Compose in the user's CLI plugin directory.
        # Preserve plugin discovery without copying auths or a credential helper.
        plugin_directory = Path(os.environ.get('DOCKER_CONFIG', str(Path.home() / '.docker'))) / 'cli-plugins'
        docker_config = {'auths': {}}
        if plugin_directory.is_dir():
            docker_config['cliPluginsExtraDirs'] = [str(plugin_directory.resolve())]
        (anonymous / 'config.json').write_text(json.dumps(docker_config) + '\n')
        docker = ['docker', '--config', str(anonymous), '--host', host]
        data = root / 'data'
        data.mkdir(mode=0o777)
        data.chmod(0o777)  # Disposable synthetic fixture only; production keeps UID 10001.
        values = {}
        for line in (deployment / 'ghcr.env.example').read_text().splitlines():
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key] = value
        values.update(DOMAIN='studytool-smoke', RAG_DATA_DIR=str(data),
                      BACKEND_IMAGE=manifest['images']['backend']['digest'],
                      FRONTEND_IMAGE=manifest['images']['frontend']['digest'],
                      LLM_BASE_URL='http://127.0.0.1:9/v1', LLM_API_KEY='smoke-unused',
                      OCR_MODE='vision_llm', OCR_BASE_URL='http://127.0.0.1:9/v1',
                      OCR_API_KEY='smoke-unused', BAIDU_OCR_TOKEN='',
                      EMBEDDING_MODE='remote', EMBEDDING_BASE_URL='http://model-fixture:9000/v1',
                      EMBEDDING_API_KEY='isolated-fixture')
        envfile = root / '.env'
        envfile.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
        envfile.chmod(0o600)
        environment = {key: value for key, value in os.environ.items() if key not in values}

        def run(*arguments, **kwargs):
            return subprocess.run(arguments, check=True, text=True, env=environment,
                                  capture_output=True, **kwargs)

        compose = docker + ['compose', '-p', project, '--env-file', str(envfile),
                            '-f', str(deployment / 'compose.yaml')]
        config = json.loads(run(*compose, 'config', '--format', 'json').stdout)
        assert set(config['services']) == {'backend', 'frontend'}
        assert all('build' not in service for service in config['services'].values())
        assert not config['services']['backend'].get('ports')
        # Explicit pull still resolves the public registry with an empty auth config,
        # even if the daemon already has matching layers cached.
        print('Pulling production Compose images anonymously...', flush=True)
        try:
            run(*compose, 'pull')
        except subprocess.CalledProcessError as error:
            print(error.stdout or '')
            print(error.stderr or '')
            raise
        for name in ('backend', 'frontend'):
            reference = manifest['images'][name]['digest']
            inspected = json.loads(run(*docker, 'image', 'inspect', reference).stdout)[0]
            assert reference in inspected['RepoDigests']
            assert inspected['Architecture'] == 'amd64'

        caddy = root / 'Caddyfile'
        caddy.write_text((Path(__file__).parent / 'Caddyfile').read_text().replace(
            '{$DOMAIN} {', '{$DOMAIN} {\n    tls internal'))
        config['services']['frontend']['ports'] = []
        config['services']['frontend']['environment']['DOMAIN'] = 'https://studytool-smoke:8443'
        config['services']['frontend']['networks'] = {'rag_private': {'aliases': ['studytool-smoke']}}
        config['services']['frontend']['volumes'].append(
            {'type': 'bind', 'source': str(caddy), 'target': '/etc/caddy/Caddyfile', 'read_only': True})
        config['services']['backend']['environment']['PUBLIC_ORIGIN'] = 'https://studytool-smoke:8443'
        config['services']['model-fixture'] = {
            'image': manifest['images']['backend']['digest'], 'platform': 'linux/amd64',
            'entrypoint': ['python', '-u', '-c', EMBEDDING_FIXTURE],
            'networks': {'rag_private': {'aliases': ['model-fixture']}}}
        config['networks']['rag_private']['internal'] = True
        rendered = root / 'compose.json'
        rendered.write_text(json.dumps(config))
        isolated = docker + ['compose', '-p', project, '-f', str(rendered)]
        try:
            run(*isolated, 'up', '-d', '--wait', '--wait-timeout', '120', '--no-build', '--pull', 'never')
            ca = run(*isolated, 'exec', '-T', 'frontend', 'cat',
                     '/data/caddy/pki/authorities/local/root.crt').stdout
            run(*isolated, 'exec', '-T', 'backend', 'python', '-c',
                'import sys; open("/tmp/smoke-ca.pem", "w").write(sys.stdin.read())', input=ca)
            result = run(*isolated, 'exec', '-T', 'backend', 'python', '-', input=PROBE)
            run(*isolated, 'restart', 'backend')
            restarted = run(*isolated, 'exec', '-T', 'backend', 'python', '-', input=RESTART_PROBE)
            evidence = {'result': 'passed', 'anonymous_pull_verified': True,
                        'source_revision': manifest['source_revision'], 'images': manifest['images'],
                        'initial': json.loads(result.stdout.strip().splitlines()[-1]),
                        'restart': json.loads(restarted.stdout.strip().splitlines()[-1]),
                        'isolation': 'temporary bind/volumes, internal network, no host ports, probe-only CA trust',
                        'production_deployed': False}
            (deployment / 'GHCR_SMOKE.json').write_text(json.dumps(evidence, indent=2) + '\n')
            print(json.dumps(evidence, indent=2))
        except subprocess.CalledProcessError as error:
            print(error.stdout or '')
            print(error.stderr or '')
            logs = subprocess.run(isolated + ['logs', '--tail', '40'], env=environment,
                                  capture_output=True, text=True)
            print(logs.stdout)
            print(logs.stderr)
            raise
        finally:
            subprocess.run(isolated + ['down', '-v', '--remove-orphans', '--timeout', '10'],
                           env=environment, capture_output=True, text=True, check=False)


if __name__ == '__main__':
    main()
