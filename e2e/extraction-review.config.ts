import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './specs',
  testMatch: /extraction-review\.spec\.ts/,
  outputDir: './test-results/extraction-review',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: 'list',
  use: {
    ...devices['Desktop Chrome'],
    baseURL: 'http://127.0.0.1:5280',
    serviceWorkers: 'block',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'npm run dev -- --port 5280 --strictPort',
    cwd: '../frontend',
    url: 'http://127.0.0.1:5280',
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
