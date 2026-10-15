const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');

function resolveProfile(app, argv = process.argv, env = process.env, tempRoot = os.tmpdir(), exists = fs.existsSync) {
  const smokeArguments = argv.filter(value => value.startsWith('--smoke-test-profile='));
  if (smokeArguments.length > 1) throw new Error('Only one smoke profile is allowed.');
  if (smokeArguments.length) {
    const input = smokeArguments[0].slice('--smoke-test-profile='.length);
    const resolved = path.resolve(input);
    const sameDirectory = (left, right) => process.platform === 'win32'
      ? left.toLowerCase() === right.toLowerCase() : left === right;
    if (!path.isAbsolute(input) || !sameDirectory(path.dirname(resolved), path.resolve(tempRoot)) ||
      !/^studytool-connected-smoke-[a-z0-9-]+$/.test(path.basename(resolved)) || exists(resolved)) {
      throw new Error('Smoke profile must be a new studytool-connected-smoke-* directory directly inside the system temp directory.');
    }
    return resolved;
  }
  const developmentProfile = !app.isPackaged && env.STUDYTOOL_CONNECTED_DEV_PROFILE;
  return developmentProfile && path.isAbsolute(developmentProfile)
    ? developmentProfile : path.join(app.getPath('appData'), 'StudyTool Connected');
}

module.exports = { resolveProfile };
