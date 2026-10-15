const fs = require('node:fs/promises');
const { normalizeOrigin } = require('./security');

async function readOrigin(filename) {
  try {
    const value = JSON.parse(await fs.readFile(filename, 'utf8'));
    return { origin: normalizeOrigin(value.origin), error: '' };
  } catch (error) {
    if (error.code === 'ENOENT') return { origin: '', error: '' };
    return { origin: '', error: '原服务地址无法读取，请重新填写。' };
  }
}

async function saveOrigin(filename, origin) {
  const normalized = normalizeOrigin(origin);
  const temp = `${filename}.tmp`;
  await fs.writeFile(temp, `${JSON.stringify({ origin: normalized }, null, 2)}\n`, { mode: 0o600 });
  await fs.rename(temp, filename);
}

module.exports = { readOrigin, saveOrigin };
