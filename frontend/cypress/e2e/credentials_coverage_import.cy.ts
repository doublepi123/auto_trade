describe('Credentials Coverage Matrix, Channels JSON & Dirty Diff', () => {
  it('marks all severities covered when an INFO-floor channel exists', () => {
    cy.stubApi()
    cy.visit('/#/credentials')
    cy.wait('@getCredentials')
    cy.get('[data-testid="coverage-matrix"]').should('be.visible')
    cy.get('[data-testid="coverage-matrix"]').should('contain', 'CRITICAL 已覆盖')
    cy.get('[data-testid="coverage-matrix"]').should('contain', 'INFO 已覆盖')
  })

  it('flags uncovered severities when only a CRITICAL-floor channel is configured', () => {
    cy.stubApi()
    cy.intercept('GET', '/api/credentials', {
      body: {
        id: 1, longbridge_app_key: '', longbridge_app_secret: '',
        longbridge_access_token: '', sct_key: '',
        has_longbridge_app_key: false, has_longbridge_app_secret: false,
        has_longbridge_access_token: false, has_sct_key: false,
        notification_channels: [{ type: 'webhook', severity_floor: 'CRITICAL', url: 'https://example.com/hook' }],
        updated_at: '2026-01-01T00:00:00Z',
      },
    }).as('getCritCreds')
    cy.visit('/#/credentials')
    cy.wait('@getCritCreds')
    cy.get('[data-testid="coverage-matrix"]').should('contain', 'CRITICAL 已覆盖')
    cy.get('[data-testid="coverage-matrix"]').should('contain', 'WARNING 未覆盖')
    cy.get('[data-testid="coverage-matrix"]').should('contain', 'INFO 未覆盖')
  })

  it('exports channels JSON and shows a dirty diff after editing', () => {
    cy.stubApi()
    cy.visit('/#/credentials')
    cy.wait('@getCredentials')
    cy.get('[data-testid="channels-export-json"]').click()
    cy.document().its('body').should('contain', '已导出通知渠道配置')

    // Type into the Server酱密钥 field → dirty diff surfaces it
    cy.contains('.el-form-item', 'Server酱推送密钥').find('input').type('newkey')
    cy.get('[data-testid="credentials-dirty-diff"]').should('be.visible')
    cy.get('[data-testid="credentials-dirty-diff"]').should('contain', 'Server酱密钥')
  })
})
