#!/usr/bin/env python3
"""Promote an anonymously verified image and publish deployment files to the public release repo."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

REPOSITORY = 'ghcr.io/sakuralovesmile/studytool'
RELEASE_REPO = 'SakuraLoveSmile/StudyTool-Releases'
START = '<!-- studytool-app:start -->'
END = '<!-- studytool-app:end -->'


def run(*command):
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        print(error.stdout or '')
        print(error.stderr or '')
        raise


def preserve_legacy_deployment(root, release, tag):
    """Keep the old deployment and its checksum set valid when replacing compose.yaml."""
    names = {asset['name'] for asset in release.get('assets', [])}
    if 'GHCR_SHA256SUMS.txt' not in names:
        return
    with tempfile.TemporaryDirectory(prefix='studytool-legacy-') as temporary:
        legacy = Path(temporary)
        compose_name = 'compose.caddy.yaml' if 'compose.caddy.yaml' in names else 'compose.yaml'
        for name in (compose_name, 'ghcr.env.example', 'GHCR.md', 'GHCR_MANIFEST.json',
                     'GHCR_SMOKE.json', 'GHCR_SHA256SUMS.txt'):
            run('gh', 'release', 'download', tag, '--repo', RELEASE_REPO,
                '--pattern', name, '--dir', str(legacy))
        for line in (legacy / 'GHCR_SHA256SUMS.txt').read_text().splitlines():
            expected, name = line.split('  ', 1)
            if name not in names or '/' in name or '\\' in name:
                raise RuntimeError('Unexpected legacy checksum entry')
            source = legacy / compose_name if name in {'compose.yaml', 'compose.caddy.yaml'} else legacy / name
            variants = [source.read_bytes()]
            if name == 'GHCR.md':
                # A previous multi-file upload may have stopped between the doc and checksum.
                text = source.read_text()
                variants += [text.replace('compose.yaml', 'compose.caddy.yaml').encode(),
                             text.replace('compose.caddy.yaml', 'compose.yaml').encode()]
            if not any(hashlib.sha256(data).hexdigest() == expected for data in variants):
                raise RuntimeError('Legacy deployment checksum mismatch: ' + name)
        (root / 'compose.caddy.yaml').write_bytes((legacy / compose_name).read_bytes())
        # Archive first: a partial later upload can always recover the original Compose.
        run('gh', 'release', 'upload', tag, str(root / 'compose.caddy.yaml'),
            '--repo', RELEASE_REPO, '--clobber')
        (root / 'GHCR.md').write_text((legacy / 'GHCR.md').read_text().replace('compose.yaml', 'compose.caddy.yaml'))
        checksums = []
        for line in (legacy / 'GHCR_SHA256SUMS.txt').read_text().splitlines():
            _, name = line.split('  ', 1)
            name = 'compose.caddy.yaml' if name == 'compose.yaml' else name
            source = root / name if (root / name).exists() else legacy / name
            checksums.append(hashlib.sha256(source.read_bytes()).hexdigest() + '  ' + name + '\n')
        (root / 'GHCR_SHA256SUMS.txt').write_text(''.join(checksums))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment', type=Path, required=True)
    args = parser.parse_args()
    root = args.deployment
    manifest = json.loads((root / 'APP_MANIFEST.json').read_text())
    smoke = json.loads((root / 'APP_SMOKE.json').read_text())
    version = manifest['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-z0-9][a-z0-9.-]*)?', version):
        parser.error('invalid release version')
    digest = manifest['image']['digest']
    latest = REPOSITORY + ':latest'
    tag = 'connected-v' + version
    if (manifest['image']['tag'] != REPOSITORY + ':' + version
            or manifest['image']['latest'] != latest or manifest['release_tag'] != tag
            or not re.fullmatch(re.escape(REPOSITORY) + r'@sha256:[a-f0-9]{64}', digest)
            or smoke['result'] != 'passed' or smoke['image'] != digest
            or smoke['version_tag_verified'] != manifest['image']['tag']
            or not smoke['anonymous_pull_verified'] or not smoke['cleanup_verified']):
        parser.error('deployment must pass anonymous version-tag verification first')
    if not os.environ.get('GH_TOKEN'):
        parser.error('GH_TOKEN for the public release repository is required')
    # Fail before changing latest if the cross-repository publishing credential is invalid.
    run('gh', 'api', f'repos/{RELEASE_REPO}')
    run('docker', 'buildx', 'imagetools', 'create', '--prefer-index=false', '--tag', latest, digest)
    with tempfile.TemporaryDirectory(prefix='studytool-latest-') as temporary:
        config = Path(temporary)
        (config / 'config.json').write_text('{"auths": {}}')
        run('docker', '--config', str(config), 'pull', '--platform', 'linux/amd64', latest)
        inspected = json.loads(run('docker', '--config', str(config), 'image', 'inspect', latest).stdout)[0]
        if digest not in inspected['RepoDigests']:
            raise RuntimeError('latest does not resolve to the verified version digest')
    manifest['latest_promoted'] = True
    manifest['latest_anonymous_pull_verified'] = True
    manifest['actions_url'] = (f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
                               f"{os.environ['GITHUB_RUN_ID']}")
    (root / 'APP_MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    url = f'https://github.com/{RELEASE_REPO}/releases/download/{tag}'
    notes = f'''{START}
## Docker Compose 部署（现有 Nginx）

单应用镜像：`{REPOSITORY}:{version}`，同时提供 `{latest}`。容器包含网页和 API，无 Caddy；Linux x64，由现有 Nginx 反代 `127.0.0.1:8088` 并负责 HTTPS。

下载 [compose.yaml]({url}/compose.yaml)、[app.env.example]({url}/app.env.example)（另存为 `.env`），按 [NGINX.md]({url}/NGINX.md) 设置 HTTPS 地址、模型凭据、专属数据目录并创建账号。

```bash
docker compose pull
docker compose up -d
```

默认跟随 `latest`，以后执行相同命令更新。固定版本时在 `.env` 设置 `APP_IMAGE={REPOSITORY}:{version}`；更新时改为所需版本，再执行上述命令。更新前备份数据与配置。

[Actions 验证]({manifest['actions_url']}) 已通过：匿名拉取版本标签、Nginx HTTPS、登录/CSRF、上传与章节、账号隔离、重启持久化及临时资源清理；随后将同一镜像推广至 `latest` 并验证匿名拉取。模型使用隔离协议替身，未调用真实模型，未部署用户服务器。

构建源：`SakuraLoveSmile/StudyTool@{manifest['source_revision']}`。来源、摘要和校验见 `APP_MANIFEST.json`、`APP_SMOKE.json`、`APP_SHA256SUMS.txt`。摘要用于追溯，日常部署按标签更新。历史 APK、Windows 和离线包保留各自原构建来源。
{END}'''
    result = subprocess.run(['gh', 'api', f'repos/{RELEASE_REPO}/releases/tags/{tag}'],
                            capture_output=True, text=True)
    if result.returncode:
        if '404' not in result.stderr:
            raise RuntimeError('Unable to inspect existing release: ' + result.stderr)
        old = None
        body = notes
    else:
        old = json.loads(result.stdout)
        preserve_legacy_deployment(root, old, tag)
        body = old.get('body') or ''
        if START in body:
            body = re.sub(re.escape(START) + r'.*?' + re.escape(END), lambda _: notes,
                          body, count=1, flags=re.S)
        else:
            # Keep client release evidence; demote the superseded deployment section to history.
            if body.startswith('## Docker Compose 直接部署（新增）') and '\n---\n' in body:
                legacy, body = body.split('\n---\n', 1)
                legacy = legacy.replace('compose.yaml', 'compose.caddy.yaml')
                body += '\n\n<details>\n<summary>旧双容器部署记录（已由上方单应用方案替代）</summary>\n\n' + legacy + '\n\n</details>\n'
            body = notes + '\n\n---\n\n' + body
    (root / 'APP_SHA256SUMS.txt').write_text(''.join(
        hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n'
        for p in sorted(root.iterdir()) if p.is_file() and p.name != 'APP_SHA256SUMS.txt'))
    with tempfile.TemporaryDirectory(prefix='studytool-release-') as temporary:
        notes_file = Path(temporary) / 'notes.md'
        notes_file.write_text(body)
        files = [str(p) for p in sorted(root.iterdir()) if p.is_file()]
        if old is None:
            run('gh', 'release', 'create', tag, *files, '--repo', RELEASE_REPO,
                '--title', f'学习助手 联网版 {version}（预览版）', '--notes-file', str(notes_file),
                '--prerelease', '--latest=false')
        else:
            run('gh', 'release', 'upload', tag, *files, '--repo', RELEASE_REPO, '--clobber')
            run('gh', 'release', 'edit', tag, '--repo', RELEASE_REPO, '--notes-file', str(notes_file))
    print(f'Published https://github.com/{RELEASE_REPO}/releases/tag/{tag}; {latest} -> {digest}')


if __name__ == '__main__':
    main()
