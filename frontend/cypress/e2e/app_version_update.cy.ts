import {
  APP_VERSION_MANIFEST_PATH,
  fetchAppBuildId,
  parseAppBuildId,
} from '../../src/utils/appVersion'

describe('App version manifest parsing', () => {
  it('accepts only a bounded non-empty build identifier', () => {
    expect(parseAppBuildId({ build_id: '  build-123  ' })).to.equal('build-123')
    expect(parseAppBuildId({ build_id: '' })).to.equal(null)
    expect(parseAppBuildId({ build_id: 123 })).to.equal(null)
    expect(parseAppBuildId([])).to.equal(null)
    expect(parseAppBuildId('not-an-object')).to.equal(null)
    expect(parseAppBuildId({ build_id: 'x'.repeat(201) })).to.equal(null)
  })

  it('requests the manifest without caching and fails open on errors', async () => {
    let requestedInput: RequestInfo | URL | null = null
    let requestedInit: RequestInit | undefined
    const buildId = await fetchAppBuildId(async (input, init) => {
      requestedInput = input
      requestedInit = init
      return new Response(JSON.stringify({ build_id: 'build-456' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    })

    expect(requestedInput).to.equal(APP_VERSION_MANIFEST_PATH)
    expect(requestedInit?.cache).to.equal('no-store')
    expect(requestedInit?.signal).to.be.instanceOf(AbortSignal)
    expect(buildId).to.equal('build-456')

    const failed = await fetchAppBuildId(async () => {
      throw new Error('network unavailable')
    })
    expect(failed).to.equal(null)
  })
})

describe('App version updates', () => {
  beforeEach(() => {
    cy.stubApi()
  })

  it('fails open for an invalid manifest and detects a new build on focus', () => {
    let serveNewBuild = false
    cy.intercept('GET', '/version.json', (request) => {
      if (serveNewBuild) {
        request.reply({ body: { build_id: 'cypress-different-build' } })
        return
      }
      request.reply({
        body: 'not-json',
        headers: { 'content-type': 'application/json' },
      })
    }).as('appVersion')

    cy.visit('/')
    cy.wait('@appVersion')
    cy.get('[data-testid="app-version-update"]').should('not.exist')

    cy.then(() => { serveNewBuild = true })
    cy.window().then((win) => {
      win.dispatchEvent(new Event('focus'))
    })
    cy.wait('@appVersion')
    cy.get('[data-testid="app-version-update"]')
      .should('be.visible')
      .and('contain', '检测到新的前端版本')
  })

  it('requires confirmation and reloads only after the user accepts', () => {
    let serveNewBuild = true
    cy.intercept('GET', '/version.json', (request) => {
      request.reply(serveNewBuild
        ? { body: { build_id: 'cypress-different-build' } }
        : {
            body: 'not-json',
            headers: { 'content-type': 'application/json' },
          })
    }).as('appVersion')

    cy.visit('/')
    cy.wait('@appVersion')
    cy.get('[data-testid="app-version-update"]').should('be.visible')

    cy.get('[data-testid="app-version-reload"]').click()
    cy.get('.el-message-box').should('contain', '不会停止后台交易与研究任务')
    cy.contains('.el-message-box button', '稍后处理').click()
    cy.get('[data-testid="app-version-update"]').should('be.visible')

    cy.then(() => { serveNewBuild = false })
    cy.get('[data-testid="app-version-reload"]').click()
    cy.contains('.el-message-box button', '刷新页面').click()
    cy.wait('@appVersion')
    cy.get('[data-testid="app-version-update"]').should('not.exist')
  })

  it('turns a Vite preload failure into a persistent recovery prompt', () => {
    cy.intercept('GET', '/version.json', {
      body: 'not-json',
      headers: { 'content-type': 'application/json' },
    }).as('appVersion')

    cy.visit('/')
    cy.wait('@appVersion')
    cy.window().then((win) => {
      const event = new Event('vite:preloadError', { cancelable: true })
      expect(win.dispatchEvent(event)).to.equal(false)
    })

    cy.get('[data-testid="app-version-update"]')
      .should('be.visible')
      .and('contain', '当前版本无法继续加载')
  })
})
