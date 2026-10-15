import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './specs',
  testMatch: /today-save\.spec\.ts/,
  outputDir: './test-results/today-save',
  workers: 1,
  retries: 0,
  reporter: 'list',
  use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:5281', serviceWorkers: 'block' },
  webServer: {
    command: 'npm run dev -- --port 5281 --strictPort',
    cwd: '../frontend',
    url: 'http://127.0.0.1:5281',
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
