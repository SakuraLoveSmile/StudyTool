/**
 * Minimal, dependency-free ZIP writer.
 *
 * Why hand-rolled: the diagnostic export must work in a packaged Windows build with no
 * backend running (startup-failure export) and must not add a runtime npm dependency
 * whose packaging we cannot verify here. Only `zlib` and `Buffer` are used.
 *
 * Supports: UTF-8 file names (bit 11), stored + deflate entries, no zip64 (>4 GiB is
 * refused explicitly rather than silently corrupted).
 */

import zlib from 'zlib';

const LOCAL_HEADER_SIG = 0x04034b50;
const CENTRAL_HEADER_SIG = 0x02014b50;
const EOCD_SIG = 0x06054b50;
const VERSION_NEEDED = 20; // 2.0 — deflate support
const FLAG_UTF8 = 0x0800;
const METHOD_STORE = 0;
const METHOD_DEFLATE = 8;
const MAX_UINT32 = 0xffffffff;

const CRC_TABLE: number[] = (() => {
  const table: number[] = new Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    }
    table[n] = c >>> 0;
  }
  return table;
})();

export function crc32(buffer: Buffer): number {
  let crc = 0xffffffff;
  for (let i = 0; i < buffer.length; i += 1) {
    crc = CRC_TABLE[(crc ^ buffer[i]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

/** DOS date/time encoding used by the ZIP headers. */
function dosDateTime(date: Date): { time: number; date: number } {
  const year = Math.max(1980, date.getFullYear());
  const time =
    ((date.getHours() & 0x1f) << 11) | ((date.getMinutes() & 0x3f) << 5) | ((date.getSeconds() / 2) & 0x1f);
  const dosDate = (((year - 1980) & 0x7f) << 9) | (((date.getMonth() + 1) & 0x0f) << 5) | (date.getDate() & 0x1f);
  return { time, date: dosDate };
}

export interface ZipEntryInput {
  /** Archive-relative path, forward slashes. Directory traversal is rejected. */
  name: string;
  data: Buffer | string;
}

interface PreparedEntry {
  nameBytes: Buffer;
  method: number;
  crc: number;
  compressed: Buffer;
  uncompressedSize: number;
  offset: number;
}

/**
 * Builds a ZIP archive in memory. Throws on invalid names or entries too large for
 * the non-zip64 format we emit (callers cap the payload well below that).
 */
export function createZip(entries: ZipEntryInput[], now: Date = new Date()): Buffer {
  const prepared: PreparedEntry[] = [];
  const chunks: Buffer[] = [];
  let offset = 0;
  const { time, date } = dosDateTime(now);

  for (const entry of entries) {
    const name = String(entry.name || '');
    if (!name || name.startsWith('/') || /(^|\/)\.\.(\/|$)/.test(name) || name.includes('\\')) {
      throw new Error(`非法归档条目名称: ${name}`);
    }
    const raw = Buffer.isBuffer(entry.data) ? entry.data : Buffer.from(String(entry.data), 'utf8');
    if (raw.length > MAX_UINT32) throw new Error(`归档条目过大: ${name}`);

    const deflated = zlib.deflateRawSync(raw, { level: 9 });
    // Store when compression does not actually help (tiny/JSONL-incompressible blobs).
    const useDeflate = deflated.length < raw.length;
    const payload = useDeflate ? deflated : raw;
    const method = useDeflate ? METHOD_DEFLATE : METHOD_STORE;
    const nameBytes = Buffer.from(name, 'utf8');

    const header = Buffer.alloc(30);
    header.writeUInt32LE(LOCAL_HEADER_SIG, 0);
    header.writeUInt16LE(VERSION_NEEDED, 4);
    header.writeUInt16LE(FLAG_UTF8, 6);
    header.writeUInt16LE(method, 8);
    header.writeUInt16LE(time, 10);
    header.writeUInt16LE(date, 12);
    header.writeUInt32LE(crc32(raw), 14);
    header.writeUInt32LE(payload.length, 18);
    header.writeUInt32LE(raw.length, 22);
    header.writeUInt16LE(nameBytes.length, 26);
    header.writeUInt16LE(0, 28);

    prepared.push({
      nameBytes,
      method,
      crc: crc32(raw),
      compressed: payload,
      uncompressedSize: raw.length,
      offset,
    });
    chunks.push(header, nameBytes, payload);
    offset += header.length + nameBytes.length + payload.length;
  }

  const centralStart = offset;
  let centralSize = 0;

  for (const item of prepared) {
    const central = Buffer.alloc(46);
    central.writeUInt32LE(CENTRAL_HEADER_SIG, 0);
    central.writeUInt16LE(VERSION_NEEDED, 4);
    central.writeUInt16LE(VERSION_NEEDED, 6);
    central.writeUInt16LE(FLAG_UTF8, 8);
    central.writeUInt16LE(item.method, 10);
    central.writeUInt16LE(time, 12);
    central.writeUInt16LE(date, 14);
    central.writeUInt32LE(item.crc, 16);
    central.writeUInt32LE(item.compressed.length, 20);
    central.writeUInt32LE(item.uncompressedSize, 24);
    central.writeUInt16LE(item.nameBytes.length, 28);
    central.writeUInt16LE(0, 30); // extra
    central.writeUInt16LE(0, 32); // comment
    central.writeUInt16LE(0, 34); // disk number
    central.writeUInt16LE(0, 36); // internal attrs
    central.writeUInt32LE(0, 38); // external attrs
    central.writeUInt32LE(item.offset, 42);
    chunks.push(central, item.nameBytes);
    centralSize += central.length + item.nameBytes.length;
  }

  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(EOCD_SIG, 0);
  eocd.writeUInt16LE(0, 4);
  eocd.writeUInt16LE(0, 6);
  eocd.writeUInt16LE(prepared.length, 8);
  eocd.writeUInt16LE(prepared.length, 10);
  eocd.writeUInt32LE(centralSize, 12);
  eocd.writeUInt32LE(centralStart, 16);
  eocd.writeUInt16LE(0, 20);
  chunks.push(eocd);

  return Buffer.concat(chunks);
}
