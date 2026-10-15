const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');

const {
  patchNsisTemplate,
  beforePack,
  EXPECTED_VERSION,
  ORIGINAL_SHA256,
  PATCHED_SHA256
} = require('../scripts/patch-nsis-template');

// Exact 25.1.8 original multiUser.nsh content
const ORIGINAL_TEMPLATE_CONTENT =
  '!include FileFunc.nsh\n' +
  '!include UAC.nsh\n\n' +
  '!define FOLDERID_UserProgramFiles {5CD7AEE2-2219-4A67-B85D-6C9CE15660CB}\n' +
  '!define KF_FLAG_CREATE 0x00008000\n\n' +
  '# allow user to define own custom\n' +
  '!define /ifndef INSTALL_REGISTRY_KEY "Software\\${APP_GUID}"\n' +
  '!define /ifndef UNINSTALL_REGISTRY_KEY "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${UNINSTALL_APP_KEY}"\n\n' +
  '# current Install Mode ("all" or "CurrentUser")\n' +
  'Var installMode\n\n' +
  '!ifndef INSTALL_MODE_PER_ALL_USERS\n' +
  '  !ifndef ONE_CLICK\n' +
  '    Var hasPerUserInstallation\n' +
  '    Var hasPerMachineInstallation\n' +
  '  !endif\n' +
  '  Var PerUserInstallationFolder\n\n' +
  '  !macro setInstallModePerUser\n' +
  '    StrCpy $installMode CurrentUser\n' +
  '    SetShellVarContext current\n\n' +
  '    # сhecks registry for previous installation path\n' +
  '    ReadRegStr $perUserInstallationFolder HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation\n' +
  '    ${if} $perUserInstallationFolder != ""\n' +
  '      StrCpy $INSTDIR $perUserInstallationFolder\n' +
  '    ${else}\n' +
  '      StrCpy $0 "$LocalAppData\\Programs"\n' +
  '      System::Store S\n' +
  '      # Win7 has a per-user programfiles known folder and this can be a non-default location\n' +
  '      System::Call \'SHELL32::SHGetKnownFolderPath(g "${FOLDERID_UserProgramFiles}", i ${KF_FLAG_CREATE}, p 0, *p .r2)i.r1\'\n' +
  '      ${If} $1 == 0\n' +
  '        System::Call \'*$2(&w${NSIS_MAX_STRLEN} .s)\'\n' +
  '        StrCpy $0 $1\n' +
  '        System::Call \'OLE32::CoTaskMemFree(p r2)\'\n' +
  '      ${endif}\n' +
  '      System::Store L\n' +
  '      StrCpy $INSTDIR "$0\\${APP_FILENAME}"\n' +
  '    ${endif}\n\n' +
  '    # allow /D switch to override installation path https://github.com/electron-userland/electron-builder/issues/1551\n' +
  '    ${StdUtils.GetParameter} $R0 "D" ""\n' +
  '    ${If} $R0 != ""\n' +
  '      StrCpy $INSTDIR $R0\n' +
  '    ${endif}\n\n' +
  '  !macroend\n' +
  '!endif\n\n' +
  '!ifdef INSTALL_MODE_PER_ALL_USERS_REQUIRED\n' +
  '  Var perMachineInstallationFolder\n\n' +
  '  !macro setInstallModePerAllUsers\n' +
  '    StrCpy $installMode all\n' +
  '    SetShellVarContext all\n\n' +
  '    !ifdef BUILD_UNINSTALLER\n' +
  '      ${IfNot} ${UAC_IsAdmin}\n' +
  '        ShowWindow $HWNDPARENT ${SW_HIDE}\n' +
  '        !insertmacro UAC_RunElevated\n' +
  '        Quit\n' +
  '      ${endif}\n' +
  '    !endif\n\n' +
  '    # сheck registry for previous installation path\n' +
  '    ReadRegStr $perMachineInstallationFolder HKLM "${INSTALL_REGISTRY_KEY}" InstallLocation\n' +
  '    ${if} $perMachineInstallationFolder != ""\n' +
  '      StrCpy $INSTDIR $perMachineInstallationFolder\n' +
  '    ${else}\n' +
  '      StrCpy $0 "$PROGRAMFILES"\n' +
  '      !ifdef APP_64\n' +
  '        ${if} ${RunningX64}\n' +
  '          StrCpy $0 "$PROGRAMFILES64"\n' +
  '        ${endif}\n' +
  '      !endif\n\n' +
  '      !ifdef MENU_FILENAME\n' +
  '        StrCpy $0 "$0\\${MENU_FILENAME}"\n' +
  '      !endif\n\n' +
  '      StrCpy $INSTDIR "$0\\${APP_FILENAME}"\n' +
  '    ${endif}\n\n' +
  '    # allow /D switch to override installation path https://github.com/electron-userland/electron-builder/issues/1551\n' +
  '    ${StdUtils.GetParameter} $R0 "D" ""\n' +
  '    ${If} $R0 != ""\n' +
  '      StrCpy $INSTDIR $R0\n' +
  '    ${endif}\n\n' +
  '  !macroend\n' +
  '!endif\n';

function createMockEnvironment(options = {}) {
  const version = options.version !== undefined ? options.version : EXPECTED_VERSION;
  const templateContent = options.templateContent !== undefined ? options.templateContent : ORIGINAL_TEMPLATE_CONTENT;
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'nsis-patch-test-'));

  if (version !== null) {
    fs.writeFileSync(
      path.join(tempDir, 'package.json'),
      JSON.stringify({ name: 'app-builder-lib', version }, null, 2),
      'utf8'
    );
  }

  if (templateContent !== null) {
    const templateDir = path.join(tempDir, 'templates', 'nsis');
    fs.mkdirSync(templateDir, { recursive: true });
    fs.writeFileSync(path.join(templateDir, 'multiUser.nsh'), templateContent, 'utf8');
  }

  const cleanup = () => {
    try {
      fs.rmSync(tempDir, { recursive: true, force: true });
    } catch {
      // ignore cleanup errors
    }
  };

  return { tempDir, cleanup };
}

test('hash constants validity', () => {
  const hash = crypto.createHash('sha256').update(ORIGINAL_TEMPLATE_CONTENT).digest('hex');
  assert.strictEqual(hash, ORIGINAL_SHA256, 'ORIGINAL_TEMPLATE_CONTENT SHA256 must match ORIGINAL_SHA256 constant');
});

test('Scenario a: first-time successful application on original 25.1.8 template', async () => {
  const { tempDir, cleanup } = createMockEnvironment();
  try {
    const result = await patchNsisTemplate(tempDir);

    assert.strictEqual(result.status, 'patched', 'result status should be "patched"');
    assert.strictEqual(result.hash, PATCHED_SHA256, 'result hash should match PATCHED_SHA256');

    // Read modified file on disk
    const templatePath = path.join(tempDir, 'templates', 'nsis', 'multiUser.nsh');
    const diskContent = fs.readFileSync(templatePath, 'utf8');
    const diskHash = crypto.createHash('sha256').update(diskContent).digest('hex');

    assert.strictEqual(diskHash, PATCHED_SHA256, 'disk file hash must equal PATCHED_SHA256');
    assert.ok(
      diskContent.includes('KERNEL32::lstrcpynW(w .r0, p r2, i ${NSIS_MAX_STRLEN})p'),
      'disk file must contain KERNEL32::lstrcpynW'
    );
    assert.ok(
      diskContent.includes('OLE32::CoTaskMemFree(p r2)'),
      'disk file must safely free memory with CoTaskMemFree'
    );
    assert.ok(
      !diskContent.includes('*$2(&w${NSIS_MAX_STRLEN} .s)'),
      'disk file must no longer contain the unsafe memory read'
    );
  } finally {
    cleanup();
  }
});

test('Scenario b: idempotency test on already-patched template', async () => {
  const { tempDir, cleanup } = createMockEnvironment();
  try {
    // 1st application
    const firstResult = await patchNsisTemplate(tempDir);
    assert.strictEqual(firstResult.status, 'patched');

    const templatePath = path.join(tempDir, 'templates', 'nsis', 'multiUser.nsh');
    const contentAfterFirst = fs.readFileSync(templatePath, 'utf8');
    const mtimeAfterFirst = fs.statSync(templatePath).mtimeMs;

    // 2nd application
    const secondResult = await patchNsisTemplate(tempDir);
    assert.strictEqual(secondResult.status, 'already_patched', 'subsequent run should report "already_patched"');
    assert.strictEqual(secondResult.hash, PATCHED_SHA256);

    const contentAfterSecond = fs.readFileSync(templatePath, 'utf8');
    assert.strictEqual(contentAfterSecond, contentAfterFirst, 'content must not change on idempotent call');
  } finally {
    cleanup();
  }
});

test('Scenario c: version mismatch defense', async () => {
  // Test version 25.1.9
  {
    const { tempDir, cleanup } = createMockEnvironment({ version: '25.1.9' });
    try {
      await assert.rejects(
        async () => {
          await patchNsisTemplate(tempDir);
        },
        (err) => {
          assert.match(err.message, /app-builder-lib version mismatch/i);
          assert.match(err.message, /25\.1\.9/);
          return true;
        }
      );
    } finally {
      cleanup();
    }
  }

  // Test version 24.0.0
  {
    const { tempDir, cleanup } = createMockEnvironment({ version: '24.0.0' });
    try {
      await assert.rejects(
        async () => {
          await patchNsisTemplate(tempDir);
        },
        (err) => {
          assert.match(err.message, /app-builder-lib version mismatch/i);
          assert.match(err.message, /24\.0\.0/);
          return true;
        }
      );
    } finally {
      cleanup();
    }
  }

  // Test missing package.json
  {
    const { tempDir, cleanup } = createMockEnvironment({ version: null });
    try {
      await assert.rejects(
        async () => {
          await patchNsisTemplate(tempDir);
        },
        /package\.json not found/i
      );
    } finally {
      cleanup();
    }
  }
});

test('Scenario d: template content drift defense', async () => {
  // Corrupted / modified template content
  {
    const corruptedContent = ORIGINAL_TEMPLATE_CONTENT.replace('StrCpy $installMode CurrentUser', 'StrCpy $installMode Modified');
    const { tempDir, cleanup } = createMockEnvironment({ templateContent: corruptedContent });
    try {
      await assert.rejects(
        async () => {
          await patchNsisTemplate(tempDir);
        },
        (err) => {
          assert.match(err.message, /Unknown multiUser\.nsh template drift/i);
          return true;
        }
      );
    } finally {
      cleanup();
    }
  }

  // Missing multiUser.nsh
  {
    const { tempDir, cleanup } = createMockEnvironment({ templateContent: null });
    try {
      await assert.rejects(
        async () => {
          await patchNsisTemplate(tempDir);
        },
        /multiUser\.nsh template not found/i
      );
    } finally {
      cleanup();
    }
  }
});

test('beforePack hook integration with electron-builder context', async () => {
  const { tempDir, cleanup } = createMockEnvironment();
  try {
    // electron-builder passes context object
    const mockContext = {
      packager: {
        projectDir: path.dirname(tempDir), // not used directly if we pass options
      },
      electronPlatformName: 'win32',
      arch: 'x64'
    };

    // Passing custom appBuilderLibDir through options
    const result = await beforePack({ appBuilderLibDir: tempDir });
    assert.strictEqual(result.status, 'patched');
    assert.strictEqual(result.hash, PATCHED_SHA256);
  } finally {
    cleanup();
  }
});
