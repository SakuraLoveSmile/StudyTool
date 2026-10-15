const { spawnSync } = require('node:child_process');
const { readdirSync } = require('node:fs');
const { join } = require('node:path');
for (const directory of ['src', 'scripts']) {
  for (const entry of readdirSync(join(__dirname, '..', directory))) {
    if (!entry.endsWith('.js')) continue;
    const result = spawnSync(process.execPath, ['--check', join(__dirname, '..', directory, entry)], { stdio: 'inherit' });
    if (result.status !== 0) process.exit(result.status || 1);
  }
}
