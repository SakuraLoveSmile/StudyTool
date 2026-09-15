import test from 'node:test';
import assert from 'node:assert';
import crypto from 'crypto';
import fs from 'fs';
import os from 'os';
import path from 'path';
import zlib from 'zlib';
import { buildDiagnosticArchive, writeArchiveAtomically } from '../diagnostic-export';

function tempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'rag-export-'));
}

function unzip(buffer: Buffer): Record<string, string> {
  const eocd = buffer.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  const total = buffer.readUInt16LE(eocd + 10);
  let cursor = buffer.readUInt32LE(eocd + 16);
  const files: Record<string, string> = {};
  for (let i = 0; i < total; i += 1) {
    const method = buffer.readUInt16LE(cursor + 10);
    const compressedSize = buffer.readUInt32LE(cursor + 20);
    const nameLength = buffer.readUInt16LE(cursor + 28);
    const extraLength = buffer.readUInt16LE(cursor + 30);
    const commentLength = buffer.readUInt16LE(cursor + 32);
    const localOffset = buffer.readUInt32LE(cursor + 42);
    const name = buffer.subarray(cursor + 46, cursor + 46 + nameLength).toString('utf8');
    const localNameLength = buffer.readUInt16LE(localOffset + 26);
    const localExtraLength = buffer.readUInt16LE(localOffset + 28);
    const start = localOffset + 30 + localNameLength + localExtraLength;
    const payload = buffer.subarray(start, start + compressedSize);
    files[name] = (method === 8 ? zlib.inflateRawSync(payload) : Buffer.from(payload)).toString('utf8');
    cursor += 46 + nameLength + extraLength + commentLength;
  }
  return files;
}

test('archive carries redacted logs plus version/platform/checksum manifest', () => {
  const dir = tempDir();
  const now = Date.now();
  fs.writeFileSync(
    path.join(dir, 'desktop.jsonl'),
    `${JSON.stringify({
      timestamp: new Date(now - 1000).toISOString(),
      level: 'ERROR',
      source: 'desktop',
      event: 'startup.backend.failed',
      message: '后台启动失败',
      session_id: 'sess-abc',
      app_version: '1.0.1',
    })}\n`
  );
  fs.writeFileSync(
    path.join(dir, 'backend.jsonl'),
    `${JSON.stringify({
      timestamp: new Date(now - 500).toISOString(),
      level: 'INFO',
      source: 'backend',
      event: 'startup.ready',
      message: '后台服务已就绪',
      session_id: 'sess-abc',
    })}\n`
  );

  const archive = buildDiagnosticArchive({
    dir,
    appVersion: '1.0.1',
    sessionId: 'sess-abc',
    from: now - 3600_000,
    to: now,
    platform: 'win32',
    arch: 'x64',
  });

  const files = unzip(archive.buffer);
  assert.deepStrictEqual(
    Object.keys(files).sort(),
    ['CHECKSUMS.txt', 'MANIFEST.json', 'VERSION.txt', 'backend.jsonl', 'desktop.jsonl']
  );

  const manifest = JSON.parse(files['MANIFEST.json']);
  assert.strictEqual(manifest.app_version, '1.0.1');
  assert.strictEqual(manifest.platform, 'win32');
  assert.strictEqual(manifest.arch, 'x64');
  assert.strictEqual(manifest.session_id, 'sess-abc');
  assert.strictEqual(manifest.total_entries, 2);
  assert.deepStrictEqual(manifest.per_source_entries, { desktop: 1, backend: 1 });
  assert.strictEqual(manifest.files.length, 2);
  for (const file of manifest.files) {
    assert.match(file.sha256, /^[0-9a-f]{64}$/, `sha256 recorded for ${file.name}`);
    assert.ok(file.bytes > 0);
  }
  assert.ok(manifest.excludes.includes('数据库'));

  // CHECKSUMS.txt must actually verify: every digest has to match the archived bytes.
  const checksumLines = files['CHECKSUMS.txt'].trim().split('\n');
  assert.strictEqual(checksumLines.length, manifest.files.length + 2, 'one checksum per archived file');
  for (const line of checksumLines) {
    const [digest, name] = line.split(/\s+/);
    assert.match(digest, /^[0-9a-f]{64}$/);
    const expected = crypto.createHash('sha256').update(Buffer.from(files[name], 'utf8')).digest('hex');
    assert.strictEqual(digest, expected, `checksum for ${name} matches the archived bytes`);
  }
  assert.match(files['VERSION.txt'], /platform: win32/);

  assert.match(archive.fileName, /^rag-diagnostics-.*\.zip$/);
  assert.strictEqual(archive.entries, 2);
  assert.strictEqual(archive.truncated, false);
});

test('archive never contains credentials, database or material payloads', () => {
  const dir = tempDir();
  const now = Date.now();
  // Simulate a stale/legacy file that still holds raw secrets and business content.
  fs.writeFileSync(
    path.join(dir, 'desktop.jsonl'),
    `${JSON.stringify({
      timestamp: new Date(now).toISOString(),
      level: 'INFO',
      source: 'desktop',
      event: 'legacy.record',
      message: 'api_key=sk-abcdef1234567890 用户资料正文',
      api_key: 'sk-abcdef1234567890',
      prompt: '完整提示词',
      endpoint: 'https://api.example.com/v1/chat?token=leaked',
      path: 'C:\\Users\\测试用户\\Documents\\真题.pdf',
    })}\n`
  );

  const archive = buildDiagnosticArchive({
    dir,
    appVersion: '1.0.1',
    sessionId: 'sess-abc',
    from: now - 3600_000,
    to: now + 1000,
  });
  const payload = Object.values(unzip(archive.buffer)).join('\n');

  assert.ok(!payload.includes('sk-abcdef1234567890'), 'api key masked');
  assert.ok(!payload.includes('leaked'), 'url token masked');
  assert.ok(!payload.includes('测试用户'), 'user path masked');
  assert.ok(!payload.includes('真题.pdf'), 'document name masked');
  assert.ok(!payload.includes('完整提示词'), 'prompt field dropped');
  assert.ok(!/\.db\b/.test(payload), 'no database content');
});

test('archive export is caps-bounded and reports truncation in the manifest', () => {
  const dir = tempDir();
  const now = Date.now();
  const lines = Array.from({ length: 300 }, (_, i) =>
    JSON.stringify({
      timestamp: new Date(now - (300 - i) * 1000).toISOString(),
      level: 'INFO',
      source: 'desktop',
      event: `evt.${i}`,
      message: 'y'.repeat(300),
    })
  ).join('\n');
  fs.writeFileSync(path.join(dir, 'desktop.jsonl'), `${lines}\n`);

  const archive = buildDiagnosticArchive({
    dir,
    appVersion: '1.0.1',
    sessionId: 'sess-abc',
    from: 0,
    to: now + 1000,
    maxRawBytes: 8192,
  });

  assert.strictEqual(archive.truncated, true);
  assert.ok(archive.rawBytes <= 8192);
  const manifest = JSON.parse(unzip(archive.buffer)['MANIFEST.json']);
  assert.strictEqual(manifest.truncated, true);
  assert.match(manifest.note, /已仅保留最新记录/);
});

test('atomic write leaves no partial or temp file behind', () => {
  const dir = tempDir();
  const target = path.join(dir, 'out.zip');
  writeArchiveAtomically(target, Buffer.from('payload'));
  assert.strictEqual(fs.readFileSync(target, 'utf8'), 'payload');
  assert.deepStrictEqual(
    fs.readdirSync(dir).filter((name) => name.includes('.tmp')),
    [],
    'no temp file survives a successful write'
  );

  // A failing write must not leave a half-written destination or a temp file.
  const badTarget = path.join(dir, 'missing-dir', 'nested', 'out.zip');
  fs.writeFileSync(path.join(dir, 'blocker'), 'file');
  assert.throws(() => writeArchiveAtomically(path.join(dir, 'blocker', 'x.zip'), Buffer.from('x')));
  assert.deepStrictEqual(fs.readdirSync(dir).filter((name) => name.includes('.tmp')), []);
  assert.ok(!fs.existsSync(badTarget));
});
