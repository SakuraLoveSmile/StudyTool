/**
 * Safe directory reading patch for electron-builder 25.1.8 NSIS multiUser.nsh
 * Upstream fix: a356198ec7c54c7795659342bff36d9a5162cd93
 *
 * Prevents potential buffer/pointer issues when reading user program files directory
 * by using KERNEL32::lstrcpynW and ensuring OLE32::CoTaskMemFree is called safely.
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const EXPECTED_VERSION = '25.1.8';
const ORIGINAL_SHA256 = 'afa9046492317e72e79e022cdb1f42edba4687ea605300bfc01c002f3106201c';
const PATCHED_SHA256 = '519a23385d3b3da4ce3925f9b9c34e2b8d2b98a1bcc8a510f721f56cffe910ee';

const OLD_SNIPPET = [
  '      StrCpy $0 "$LocalAppData\\Programs"',
  '      System::Store S',
  '      # Win7 has a per-user programfiles known folder and this can be a non-default location',
  '      System::Call \'SHELL32::SHGetKnownFolderPath(g "${FOLDERID_UserProgramFiles}", i ${KF_FLAG_CREATE}, p 0, *p .r2)i.r1\'',
  '      ${If} $1 == 0',
  '        System::Call \'*$2(&w${NSIS_MAX_STRLEN} .s)\'',
  '        StrCpy $0 $1',
  '        System::Call \'OLE32::CoTaskMemFree(p r2)\'',
  '      ${endif}',
  '      System::Store L',
  '      StrCpy $INSTDIR "$0\\${APP_FILENAME}"'
].join('\n');

const NEW_SNIPPET = [
  '      StrCpy $0 "$LocalAppData\\Programs"',
  '',
  '      Push $1',
  '      Push $2',
  '      # UserProgramFiles is the per-user install root and can be a non-default location',
  '      StrCpy $2 0',
  '      System::Call \'SHELL32::SHGetKnownFolderPath(g "${FOLDERID_UserProgramFiles}", i ${KF_FLAG_CREATE}, p 0, *p .r2)i.r1\'',
  '      ${If} $1 == 0',
  '        System::Call \'KERNEL32::lstrcpynW(w .r0, p r2, i ${NSIS_MAX_STRLEN})p\'',
  '      ${endif}',
  '      # SHGetKnownFolderPath may return allocated memory even on failure',
  '      ${If} $2 != 0',
  '        System::Call \'OLE32::CoTaskMemFree(p r2)\'',
  '      ${endif}',
  '      Pop $2',
  '      Pop $1',
  '',
  '      StrCpy $INSTDIR "$0\\${APP_FILENAME}"'
].join('\n');

function computeSha256(content) {
  return crypto.createHash('sha256').update(content).digest('hex');
}

function resolveTarget(targetOrOptions) {
  let appBuilderLibDir;
  let packageJsonPath;
  let templatePath;

  if (typeof targetOrOptions === 'string') {
    const resolved = path.resolve(targetOrOptions);
    if (fs.existsSync(resolved) && fs.statSync(resolved).isFile()) {
      templatePath = resolved;
      const dir = path.dirname(resolved);
      const directPkg = path.join(dir, 'package.json');
      const upperPkg = path.resolve(dir, '../../package.json');
      packageJsonPath = fs.existsSync(directPkg) ? directPkg : upperPkg;
      appBuilderLibDir = path.dirname(packageJsonPath);
    } else {
      appBuilderLibDir = resolved;
      packageJsonPath = path.join(appBuilderLibDir, 'package.json');
      templatePath = path.join(appBuilderLibDir, 'templates', 'nsis', 'multiUser.nsh');
    }
  } else if (targetOrOptions && typeof targetOrOptions === 'object') {
    if (targetOrOptions.appBuilderLibDir) {
      appBuilderLibDir = path.resolve(targetOrOptions.appBuilderLibDir);
      packageJsonPath = targetOrOptions.packageJsonPath || path.join(appBuilderLibDir, 'package.json');
      templatePath = targetOrOptions.templatePath || path.join(appBuilderLibDir, 'templates', 'nsis', 'multiUser.nsh');
    } else if (targetOrOptions.templatePath || targetOrOptions.packageJsonPath) {
      templatePath = targetOrOptions.templatePath;
      packageJsonPath = targetOrOptions.packageJsonPath;
      appBuilderLibDir = templatePath
        ? path.dirname(path.dirname(path.dirname(templatePath)))
        : (packageJsonPath ? path.dirname(packageJsonPath) : process.cwd());
    } else if (targetOrOptions.packager) {
      // electron-builder context
      const projectDir = targetOrOptions.packager.projectDir || targetOrOptions.packager.info?.projectDir;
      if (projectDir) {
        appBuilderLibDir = path.resolve(projectDir, 'node_modules/app-builder-lib');
      } else {
        appBuilderLibDir = path.resolve(__dirname, '../node_modules/app-builder-lib');
      }
      packageJsonPath = path.join(appBuilderLibDir, 'package.json');
      templatePath = path.join(appBuilderLibDir, 'templates', 'nsis', 'multiUser.nsh');
    } else {
      appBuilderLibDir = path.resolve(__dirname, '../node_modules/app-builder-lib');
      packageJsonPath = path.join(appBuilderLibDir, 'package.json');
      templatePath = path.join(appBuilderLibDir, 'templates', 'nsis', 'multiUser.nsh');
    }
  } else {
    appBuilderLibDir = path.resolve(__dirname, '../node_modules/app-builder-lib');
    packageJsonPath = path.join(appBuilderLibDir, 'package.json');
    templatePath = path.join(appBuilderLibDir, 'templates', 'nsis', 'multiUser.nsh');
  }

  return { appBuilderLibDir, packageJsonPath, templatePath };
}

async function patchNsisTemplate(targetOrOptions) {
  const { appBuilderLibDir, packageJsonPath, templatePath } = resolveTarget(targetOrOptions);

  // 1. Verify app-builder-lib package.json
  if (!fs.existsSync(packageJsonPath)) {
    throw new Error(`app-builder-lib package.json not found at: ${packageJsonPath}`);
  }

  let pkg;
  try {
    pkg = JSON.parse(fs.readFileSync(packageJsonPath, 'utf8'));
  } catch (err) {
    throw new Error(`Failed to parse app-builder-lib package.json at ${packageJsonPath}: ${err.message}`);
  }

  if (pkg.version !== EXPECTED_VERSION) {
    throw new Error(
      `app-builder-lib version mismatch: expected ${EXPECTED_VERSION}, but found ${pkg.version} (at ${packageJsonPath})`
    );
  }

  // 2. Verify and patch template multiUser.nsh
  if (!fs.existsSync(templatePath)) {
    throw new Error(`multiUser.nsh template not found at: ${templatePath}`);
  }

  const currentContent = fs.readFileSync(templatePath, 'utf8');
  const currentHash = computeSha256(currentContent);

  // Idempotent: already patched
  if (currentHash === PATCHED_SHA256) {
    return {
      status: 'already_patched',
      hash: currentHash,
      templatePath,
      appBuilderLibDir
    };
  }

  // Original: needs patch
  if (currentHash === ORIGINAL_SHA256) {
    if (!currentContent.includes(OLD_SNIPPET)) {
      throw new Error(
        `multiUser.nsh has original SHA256 (${ORIGINAL_SHA256}) but target snippet was not found. Aborting patch.`
      );
    }

    const patchedContent = currentContent.replace(OLD_SNIPPET, NEW_SNIPPET);
    fs.writeFileSync(templatePath, patchedContent, 'utf8');

    // Re-verify written file
    const writtenContent = fs.readFileSync(templatePath, 'utf8');
    const writtenHash = computeSha256(writtenContent);

    if (writtenHash !== PATCHED_SHA256) {
      throw new Error(
        `Post-patch SHA256 mismatch: expected ${PATCHED_SHA256}, but got ${writtenHash} after writing ${templatePath}`
      );
    }

    return {
      status: 'patched',
      hash: writtenHash,
      templatePath,
      appBuilderLibDir
    };
  }

  // Unknown template drift: neither original nor patched
  throw new Error(
    `Unknown multiUser.nsh template drift! SHA256: ${currentHash} does not match expected original (${ORIGINAL_SHA256}) or patched (${PATCHED_SHA256}). Path: ${templatePath}`
  );
}

async function beforePack(context) {
  return await patchNsisTemplate(context);
}

module.exports = beforePack;
module.exports.beforePack = beforePack;
module.exports.patchNsisTemplate = patchNsisTemplate;
module.exports.EXPECTED_VERSION = EXPECTED_VERSION;
module.exports.ORIGINAL_SHA256 = ORIGINAL_SHA256;
module.exports.PATCHED_SHA256 = PATCHED_SHA256;
module.exports.OLD_SNIPPET = OLD_SNIPPET;
module.exports.NEW_SNIPPET = NEW_SNIPPET;

if (require.main === module) {
  patchNsisTemplate()
    .then((res) => {
      console.log(`[patch-nsis-template] Success (${res.status}): ${res.templatePath}`);
      console.log(`[patch-nsis-template] Current SHA256: ${res.hash}`);
    })
    .catch((err) => {
      console.error(`[patch-nsis-template] Failed: ${err.message}`);
      process.exit(1);
    });
}
