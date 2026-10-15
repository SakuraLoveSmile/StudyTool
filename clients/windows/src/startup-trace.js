const fs = require('node:fs');
const { resolveProfile } = require('./profile');

// Enabled only by the bounded CI smoke profile argument. It does not create that
// profile or change Electron's default exception handling.
function createStartupTrace(app, options = {}) {
  const argv = options.argv || process.argv;
  const processLike = options.processLike || process;
  const noop = () => {};
  if (!argv.some(value => value.startsWith('--smoke-test-profile='))) return noop;
  let filename;
  try {
    // Validate location/name even when diagnosing an unexpectedly existing profile.
    const profile = resolveProfile(app, argv, {}, options.tempRoot, () => false);
    filename = `${profile}.startup.jsonl`;
    fs.closeSync(fs.openSync(filename, 'ax', 0o600));
  } catch (error) {
    processLike.stderr.write(`Smoke startup trace unavailable: ${error.message}\n`);
    return noop;
  }
  const record = (event, details = {}) => {
    try {
      fs.appendFileSync(filename, `${JSON.stringify({ at: new Date().toISOString(), event, ...details })}\n`);
    } catch (error) { processLike.stderr.write(`Smoke startup trace write failed: ${error.message}\n`); }
  };
  // Monitor observes errors without consuming uncaughtException or replacing the
  // default native error dialog. stderr is also captured by the smoke runner.
  processLike.on('uncaughtExceptionMonitor', error => {
    record('uncaught-main-exception', { message: error.message, stack: error.stack });
    processLike.stderr.write(`${error.stack || error.message}\n`);
  });
  record.file = filename;
  return record;
}

module.exports = { createStartupTrace };
