import test from 'node:test';
import assert from 'node:assert';
import zlib from 'zlib';
import { createZip, crc32 } from '../zip';

interface ParsedEntry {
  name: string;
  method: number;
  crc: number;
  data: Buffer;
}

/** Independent ZIP reader: walks the central directory, so a writer bug cannot hide. */
function parseZip(buffer: Buffer): ParsedEntry[] {
  const eocd = buffer.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  assert.ok(eocd >= 0, 'end of central directory record present');
  const total = buffer.readUInt16LE(eocd + 10);
  let cursor = buffer.readUInt32LE(eocd + 16);
  const entries: ParsedEntry[] = [];

  for (let i = 0; i < total; i += 1) {
    assert.strictEqual(buffer.readUInt32LE(cursor), 0x02014b50, 'central header signature');
    const method = buffer.readUInt16LE(cursor + 10);
    const crc = buffer.readUInt32LE(cursor + 16);
    const compressedSize = buffer.readUInt32LE(cursor + 20);
    const uncompressedSize = buffer.readUInt32LE(cursor + 24);
    const nameLength = buffer.readUInt16LE(cursor + 28);
    const extraLength = buffer.readUInt16LE(cursor + 30);
    const commentLength = buffer.readUInt16LE(cursor + 32);
    const localOffset = buffer.readUInt32LE(cursor + 42);
    const name = buffer.subarray(cursor + 46, cursor + 46 + nameLength).toString('utf8');

    assert.strictEqual(buffer.readUInt32LE(localOffset), 0x04034b50, 'local header signature');
    const localNameLength = buffer.readUInt16LE(localOffset + 26);
    const localExtraLength = buffer.readUInt16LE(localOffset + 28);
    const dataStart = localOffset + 30 + localNameLength + localExtraLength;
    const payload = buffer.subarray(dataStart, dataStart + compressedSize);
    const data = method === 8 ? zlib.inflateRawSync(payload) : Buffer.from(payload);

    assert.strictEqual(data.length, uncompressedSize, `size matches for ${name}`);
    assert.strictEqual(crc32(data), crc, `crc matches for ${name}`);
    entries.push({ name, method, crc, data });
    cursor += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

test('zip round-trip preserves names and content, verified by an independent reader', () => {
  const compressible = 'repeated diagnostic line for compression\n'.repeat(200);
  const archive = createZip([
    { name: 'VERSION.txt', data: 'app_version: 1.0.1\n' },
    { name: 'desktop.jsonl', data: '{"event":"a"}\n{"event":"b"}\n' },
    { name: 'nested/dir/note.txt', data: '中文内容测试' },
    { name: 'big.jsonl', data: compressible },
  ]);

  const entries = parseZip(archive);
  assert.deepStrictEqual(
    entries.map((e) => e.name).sort(),
    ['VERSION.txt', 'big.jsonl', 'desktop.jsonl', 'nested/dir/note.txt']
  );
  const desktop = entries.find((e) => e.name === 'desktop.jsonl')!;
  assert.strictEqual(desktop.data.toString('utf8'), '{"event":"a"}\n{"event":"b"}\n');
  const nested = entries.find((e) => e.name === 'nested/dir/note.txt')!;
  assert.strictEqual(nested.data.toString('utf8'), '中文内容测试');
  // Compressible payloads are deflated; tiny ones may legitimately be stored when
  // deflating would make them bigger — the round-trip above proves correctness either way.
  const big = entries.find((e) => e.name === 'big.jsonl')!;
  assert.strictEqual(big.method, 8, 'a large compressible entry is deflated');
  assert.strictEqual(big.data.toString('utf8'), compressible);
});

test('zip handles incompressible data and empty files', () => {
  const random = Buffer.from(Array.from({ length: 4096 }, (_, i) => (i * 37) % 256));
  const archive = createZip([
    { name: 'blob.bin', data: random },
    { name: 'empty.txt', data: '' },
  ]);
  const entries = parseZip(archive);
  const blob = entries.find((e) => e.name === 'blob.bin')!;
  assert.ok(blob.data.equals(random), 'content survives regardless of store/deflate choice');
  assert.ok(entries.find((e) => e.name === 'empty.txt')!.data.length === 0);
});

test('zip refuses path traversal and absolute names', () => {
  assert.throws(() => createZip([{ name: '../escape.txt', data: 'x' }]), /非法归档条目名称/);
  assert.throws(() => createZip([{ name: '/etc/passwd', data: 'x' }]), /非法归档条目名称/);
  assert.throws(() => createZip([{ name: 'a\\b.txt', data: 'x' }]), /非法归档条目名称/);
  assert.throws(() => createZip([{ name: '', data: 'x' }]), /非法归档条目名称/);
});
