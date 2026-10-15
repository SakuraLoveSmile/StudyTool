const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { readOrigin, saveOrigin } = require('../src/config');

test('address configuration persists only a validated origin and invalid files return a visible error', async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'studytool-connected-'));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const filename = path.join(dir, 'service.json');
  assert.deepEqual(await readOrigin(filename), { origin: '', error: '' });
  await saveOrigin(filename, 'https://Learn.Example.com/');
  assert.deepEqual(await readOrigin(filename), { origin: 'https://learn.example.com', error: '' });
  await assert.rejects(() => saveOrigin(filename, 'http://bad.example.com'));
  assert.deepEqual(JSON.parse(await fs.readFile(filename)), { origin: 'https://learn.example.com' });
  await fs.writeFile(filename, '{"origin":"javascript:alert(1)"}');
  assert.match((await readOrigin(filename)).error, /重新填写/);
  await fs.writeFile(filename, 'corrupted');
  assert.match((await readOrigin(filename)).error, /重新填写/);
});
