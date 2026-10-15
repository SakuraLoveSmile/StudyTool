#!/usr/bin/env python3
"""Build and export private-data-free Docker images; never push a registry."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> None:
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=['linux/amd64', 'linux/arm64'], default='linux/amd64')
    parser.add_argument('--tag', default='connected-candidate')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/connected-delivery/server')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}', args.tag):
        parser.error('tag must be a Docker tag, at most 100 characters')
    target = args.output.resolve()
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        parser.error('output must be empty; existing delivery files are never overwritten')
    suffix = args.platform.split('/')[1]
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    tags = {name: f'studytool-{name}:{args.tag}-{suffix}' for name in ('backend', 'frontend')}
    for name, tag in tags.items():
        run('docker', 'buildx', 'build', '--platform', args.platform, '--load',
            '--provenance=false',
            '--label', 'org.opencontainers.image.source=https://github.com/SakuraLoveSmile/StudyTool',
            '--label', f'org.opencontainers.image.revision={revision}',
            '-f', f'{name}/Dockerfile', '-t', tag, '.')
    archive = target / 'studytool-images.tar.gz'
    with gzip.open(archive, 'wb', compresslevel=1) as output:
        process = subprocess.Popen(['docker', 'image', 'save', *tags.values()], stdout=subprocess.PIPE)
        assert process.stdout is not None
        shutil.copyfileobj(process.stdout, output)
        if process.wait() != 0:
            raise RuntimeError('docker image save failed')
    shutil.copy2(ROOT / 'compose.yaml', target / 'compose.yaml')
    deployment = target / 'deploy'
    deployment.mkdir()
    for name in ('README.md', 'Caddyfile', 'backup.sh', 'backup.py', 'restore.sh',
                 'rag-backup.service', 'rag-backup.timer'):
        shutil.copy2(ROOT / 'deploy' / name, deployment / name)
    example = (ROOT / 'deploy/.env.example').read_text()
    example = re.sub(r'^BACKEND_IMAGE=.*$', f'BACKEND_IMAGE={tags["backend"]}', example, flags=re.M)
    example = re.sub(r'^FRONTEND_IMAGE=.*$', f'FRONTEND_IMAGE={tags["frontend"]}', example, flags=re.M)
    (deployment / '.env.example').write_text(example)
    (target / 'README.md').write_text('''# StudyTool 服务端镜像包

先查看 BUILD.json 的 platform，确认与 Linux 服务器匹配。此包不包含密钥、资料或业务源码。

在本目录运行：

```sh
sha256sum -c SHA256SUMS.txt
docker image load -i studytool-images.tar.gz
sudo install -d -m 0750 -o 10001 -g 10001 /srv/rag-data
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
```

编辑 deploy/.env，填写真实 DOMAIN、模型凭据，核对 RAG_DATA_DIR 及镜像标签；域名 DNS 必须指向服务器，开放80/443端口。

```sh
docker compose --env-file deploy/.env config --quiet
docker compose --env-file deploy/.env up -d --no-build --pull never
docker compose --env-file deploy/.env ps
docker compose --env-file deploy/.env exec backend python -m app.manage_users create alice
```

密码交互输入；首次登录需修改。两端客户端填写 https://真实域名。此包使用加载的镜像，不能运行源码 build 命令。
备份、恢复及完整说明见 deploy/README.md。当前尚未完成公网部署或真机验收。
''')
    metadata = {'platform': args.platform, 'images': tags,
                'revision': revision,
                'working_tree_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
                'published': False}
    (target / 'BUILD.json').write_text(json.dumps(metadata, indent=2) + '\n')
    checksums = []
    for path in sorted(target.rglob('*')):
        if path.is_file():
            with path.open('rb') as source:
                digest = hashlib.sha256()
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
                checksum = digest.hexdigest()
            checksums.append(f'{checksum}  {path.relative_to(target).as_posix()}\n')
    (target / 'SHA256SUMS.txt').write_text(''.join(checksums))
    print(f'Bundle ready: {target} ({args.platform}); not published')


if __name__ == '__main__':
    main()
