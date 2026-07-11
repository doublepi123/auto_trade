import { defineConfig } from 'cypress'

const baseUrl = process.env.CYPRESS_BASE_URL || 'http://localhost:8080'

export default defineConfig({
  allowCypressEnv: false,
  e2e: {
    baseUrl,
    supportFile: 'cypress/support/e2e.ts',
    specPattern: 'cypress/e2e/**/*.cy.{js,ts}',
    viewportWidth: 1280,
    viewportHeight: 720,
    defaultCommandTimeout: 10000,
  },
})
