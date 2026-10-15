function claimSingleInstance(app, getWindows) {
  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return false;
  }
  app.on('second-instance', () => {
    const win = getWindows().find(candidate => candidate && !candidate.isDestroyed());
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
  });
  return true;
}

module.exports = { claimSingleInstance };
