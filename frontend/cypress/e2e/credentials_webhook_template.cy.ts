describe('Credentials Webhook Template Preview', () => {
  beforeEach(() => {
    cy.stubApi()
    cy.intercept('GET', '/api/credentials', {
      body: {
        id: 1,
        longbridge_app_key: '',
        longbridge_app_secret: '',
        longbridge_access_token: '',
        sct_key: '',
        has_longbridge_app_key: false,
        has_longbridge_app_secret: false,
        has_longbridge_access_token: false,
        has_sct_key: false,
        notification_channels: [
          {
            type: 'webhook',
            severity_floor: 'INFO',
            url: 'https://example.com/hook',
            template: '{title} - {severity} - {source}',
          },
        ],
        updated_at: '2026-01-01T00:00:00Z',
      },
    }).as('getCredentialsWebhook')
    cy.visit('/#/credentials')
    cy.wait('@getCredentialsWebhook')
  })

  it('shows webhook template hint and preview when channel is webhook', () => {
    cy.get('[data-testid="webhook-template"]').should('be.visible')
    cy.get('[data-testid="webhook-template-preview"]').should('be.visible')
    cy.get('[data-testid="webhook-template-preview"]').contains('Auto Trade: 测试消息').should('be.visible')
    cy.get('[data-testid="webhook-template-preview"]').contains('WARNING').should('be.visible')
    cy.contains('可用变量：{title} {content} {severity} {timestamp} {source}').should('be.visible')
  })
})
