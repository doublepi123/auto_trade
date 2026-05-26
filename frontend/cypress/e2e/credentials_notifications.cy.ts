describe('Credentials notifications channels', () => {
  it('adds webhook channel, saves, persists after reload', () => {
    let savedChannels = [{ type: 'serverchan' as const, severity_floor: 'INFO' as const }]

    cy.intercept('GET', '/api/credentials', (req) => {
      req.reply({
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
          notification_channels: savedChannels,
          updated_at: '2026-01-01T00:00:00Z',
        },
      })
    }).as('getCredentialsDynamic')

    cy.intercept('PUT', '/api/credentials', (req) => {
      expect(req.body.notification_channels).to.have.length.at.least(1)
      expect(req.body.notification_channels[0].type).to.equal('serverchan')
      expect(req.body.notification_channels[0].severity_floor).to.equal('WARNING')
      savedChannels = req.body.notification_channels
      req.reply({
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
          notification_channels: savedChannels,
          updated_at: '2026-01-01T00:00:00Z',
          reload_warning: null,
        },
      })
    }).as('saveCredentialsNotifications')

    cy.visit('/#/credentials')
    cy.get('h3', { timeout: 10000 }).should('contain', '凭证设置')
    cy.wait('@getCredentialsDynamic')

    cy.contains('button', '添加渠道').click()
    cy.get('[data-testid="notification-channel-row"]').should('have.length', 2)

    cy.get('[data-testid="notification-channel-row"]')
      .eq(1)
      .find('.el-select')
      .eq(1)
      .click()
    cy.get('.el-select-dropdown__item:visible').contains('WARNING+').click()

    cy.get('[data-testid="notification-channel-row"]').eq(0).contains('删除').click()

    cy.contains('button', '保存').click()
    cy.wait('@saveCredentialsNotifications')
    cy.contains('已保存').should('be.visible')

    cy.reload()
    cy.wait('@getCredentialsDynamic')
    cy.get('[data-testid="notification-channel-row"]').should('have.length', 1)
    cy.get('[data-testid="notification-channel-row"]').first().find('.el-select').eq(1).should('contain.text', 'WARNING+')
  })
})
