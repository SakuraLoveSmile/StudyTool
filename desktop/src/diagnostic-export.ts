/**
 * Diagnostic bundle builder: redacted logs + version/platform/checksum manifest.
 *
 * Deliberately contains ONLY logs and metadata. Databases, uploaded materials, model
 * keys and credentials are never read here, so they cannot end up in the archive.
 */

import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import { collectExportEntries, ALL_SOURCES, EXPORT_MAX_RAW_BYTES } from './log-store';
import { createZip } from './zip';

export interface DiagnosticArchiveOptions {
  dir: string;
  appVersion: string;
  sessionId: string;
  from: number;
  to: number;
  sources?: string[];
  memoryFallback?: Record<string, unknown>[];
  /** Merged `getPendingEntries()` snapshots from both loggers (records carry `source`). */
  pendingEntries?: Record<string, unknown>[];
  /** Sum of both loggers' droppedPending counters. */
  droppedPendingEntries?: number;
  platform?: string;
  arch?: string;
  maxRawBytes?: number;
  /** Overrides MAX_SCAN_BYTES — a test hook for the truncation path, never set from IPC. */
  maxScanBytes?: number;
  now?: () => number;
}

export interface DiagnosticArchive {
  buffer: Buffer;
  fileName: string;
  entries: number;
  rawBytes: number;
  truncated: boolean;
  note: string;
  perSource: Record<string, number>;
  /** True when the payload is incomplete or memory-sourced — surfaced through IPC. */
  incomplete: boolean;
}

function sha256(data: Buffer | string): string {
  return crypto.createHash('sha256').update(data).digest('hex');
}

export function buildDiagnosticArchive(options: DiagnosticArchiveOptions): DiagnosticArchive {
  const {
    dir,
    appVersion,
    sessionId,
    from,
    to,
    sources = ALL_SOURCES,
    memoryFallback = [],
    pendingEntries = [],
    platform = process.platform,
    arch = process.arch,
    maxRawBytes = EXPORT_MAX_RAW_BYTES,
  } = options;
  const now = options.now ?? (() => Date.now());

  const selection = collectExportEntries({
    dir,
    sources,
    from,
    to,
    memoryFallback,
    pendingEntries,
    droppedPendingEntries: options.droppedPendingEntries,
    maxRawBytes,
    maxScanBytes: options.maxScanBytes,
  });

  const versionText = [
    `app_version: ${appVersion}`,
    `platform: ${platform}`,
    `arch: ${arch}`,
    `session_id: ${sessionId}`,
    `generated_at: ${new Date(now()).toISOString()}`,
    `time_range: ${new Date(from).toISOString()} ~ ${new Date(to).toISOString()}`,
    `exported_entries: ${selection.totalEntries}`,
    `exported_bytes: ${selection.totalBytes}`,
    `truncated: ${selection.truncated ? 'true' : 'false'}`,
    `incomplete: ${selection.incomplete ? 'true' : 'false'}`,
    `note: ${selection.note}`,
  ].join('\n');

  const fileDigests = selection.files.map((file) => ({
    name: file.name,
    entries: file.entries,
    bytes: file.bytes,
    sha256: sha256(file.content),
  }));

  const manifest = {
    generated_at: new Date(now()).toISOString(),
    app_version: appVersion,
    platform,
    arch,
    session_id: sessionId,
    time_range: { from: new Date(from).toISOString(), to: new Date(to).toISOString() },
    sources,
    per_source_entries: selection.perSource,
    total_entries: selection.totalEntries,
    total_bytes: selection.totalBytes,
    truncated: selection.truncated,
    note: selection.note,
    // Incompleteness disclosure: every reason the payload may be partial is a
    // first-class manifest field, not just a sentence in the note.
    read_errors: selection.read_errors,
    missing_sources: selection.missing_sources,
    skipped_lines: selection.skippedLines,
    read_truncated: selection.read_truncated,
    memory_sources: selection.memory_sources,
    buffered_entries: selection.buffered_entries,
    dropped_pending_entries: selection.dropped_pending_entries,
    incomplete: selection.incomplete,
    files: fileDigests,
    excludes: ['数据库', '上传资料原文', '模型提示词与回答', '任何凭据/密钥'],
  };

  // Checksums must cover the EXACT bytes written into the archive, otherwise the
  // CHECKSUMS.txt "verification list" would not actually verify anything.
  const versionContent = `${versionText}\n`;
  const manifestContent = `${JSON.stringify(manifest, null, 2)}\n`;
  const checksums = [
    ...fileDigests.map((file) => `${file.sha256}  ${file.name}`),
    `${sha256(versionContent)}  VERSION.txt`,
    `${sha256(manifestContent)}  MANIFEST.json`,
  ].join('\n');

  const stamp = new Date(now()).toISOString().replace(/[:.]/g, '-');
  const buffer = createZip(
    [
      { name: 'VERSION.txt', data: versionContent },
      { name: 'MANIFEST.json', data: manifestContent },
      { name: 'CHECKSUMS.txt', data: `${checksums}\n` },
      ...selection.files.map((file) => ({ name: file.name, data: file.content })),
    ],
    new Date(now())
  );

  return {
    buffer,
    fileName: `rag-diagnostics-${stamp}.zip`,
    entries: selection.totalEntries,
    rawBytes: selection.totalBytes,
    truncated: selection.truncated,
    note: selection.note,
    perSource: selection.perSource,
    incomplete: selection.incomplete,
  };
}

/**
 * Writes the archive atomically: a cancelled or failed export must never leave a
 * half-written file behind, so we write to a sibling temp file and rename it.
 */
export function writeArchiveAtomically(targetPath: string, buffer: Buffer): void {
  const dir = path.dirname(targetPath);
  const tmp = path.join(dir, `.${path.basename(targetPath)}.${process.pid}.tmp`);
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(tmp, buffer);
    fs.renameSync(tmp, targetPath);
  } catch (err) {
    try {
      if (fs.existsSync(tmp)) fs.unlinkSync(tmp);
    } catch {
      /* best effort cleanup */
    }
    throw err;
  }
}
