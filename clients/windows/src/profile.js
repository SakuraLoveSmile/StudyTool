const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');

function pathEntryExists(candidate) {
  try { fs.lstatSync(candidate); return true; } catch (error) {
    if (error.code === 'ENOENT') return false;
    throw error;
  }
}

function resolveProfile(app, argv = process.argv, env = process.env, tempRoot = os.tmpdir(), exists = pathEntryExists) {
  const smokeArguments = argv.filter(value => value.startsWith('--smoke-test-profile='));
  if (smokeArguments.length > 1) throw new Error('Only one smoke profile is allowed.');
  if (smokeArguments.length) {
    const input = smokeArguments[0].slice('--smoke-test-profile='.length);
    const resolved = path.resolve(input);
    const paths = { requested: input, parent: path.dirname(resolved), nodeTemp: tempRoot, canonicalParent: null, canonicalNodeTemp: null };
    const invalid = reason => new Error(`Smoke profile must be a new studytool-connected-smoke-* directory directly inside the system temp directory. ${reason} Paths: ${JSON.stringify(paths)}`);
    try {
      // Resolve existing parents, never the deliberately absent new child. Windows
      // TEMP may use an 8.3 alias while .NET supplies the same directory's long name.
      paths.canonicalParent = fs.realpathSync.native(paths.parent);
      paths.canonicalNodeTemp = fs.realpathSync.native(path.resolve(tempRoot));
    } catch (error) { throw invalid(`Cannot resolve parent directories: ${error.message}`); }
    const canonicalProfile = path.join(paths.canonicalParent, path.basename(resolved));
    const sameDirectory = (left, right) => process.platform === 'win32'
      ? left.toLowerCase() === right.toLowerCase() : left === right;
    if (!path.isAbsolute(input) || input.split(/[\\/]/).includes('..') ||
      !sameDirectory(paths.canonicalParent, paths.canonicalNodeTemp) ||
      !/^studytool-connected-smoke-[a-z0-9-]+$/.test(path.basename(resolved)) || exists(canonicalProfile)) {
      throw invalid('The requested child is invalid, outside temp, or already exists.');
    }
    return canonicalProfile;
  }
  const developmentProfile = !app.isPackaged && env.STUDYTOOL_CONNECTED_DEV_PROFILE;
  return developmentProfile && path.isAbsolute(developmentProfile)
    ? developmentProfile : path.join(app.getPath('appData'), 'StudyTool Connected');
}

module.exports = { resolveProfile };
