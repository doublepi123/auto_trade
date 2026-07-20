describe('Credentials notifications channels', () => {
  it('adds Telegram, keeps a masked token out of the save payload, and renders it after reload', () => {
    let savedChannels = [{
      type: 'telegram',
      severity_floor: 'WARNING',
      bot_token: '***',
      chat_id: '-1001234567890',
    }]

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
    }).as('getTelegramCredentials')

    cy.intercept('PUT', '/api/credentials', (req) => {
      expect(req.body.notification_channels).to.deep.equal([{
        type: 'telegram',
        severity_floor: 'CRITICAL',
        chat_id: '-1001234567890',
      }])
      savedChannels = [{
        type: 'telegram',
        severity_floor: 'CRITICAL',
        bot_token: '***',
        chat_id: '-1001234567890',
      }]
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
    }).as('saveTelegramCredentials')

    cy.visit('/#/credentials')
    cy.wait('@getTelegramCredentials')
    cy.get('[data-testid="telegram-bot-token"]').should('have.value', '')
    cy.get('[data-testid="telegram-bot-token"]').should('have.attr', 'placeholder', '已保存，留空则保留当前 Bot Token')
    cy.get('[data-testid="telegram-chat-id"]').should('have.value', '-1001234567890')

    cy.get('[data-testid="notification-channel-row"]').find('.el-select').eq(1).click()
    cy.get('.el-select-dropdown__item:visible').contains('仅 CRITICAL').click()
    cy.contains('button', '保存').click()
    cy.wait('@saveTelegramCredentials')

    cy.reload()
    cy.wait('@getTelegramCredentials')
    cy.get('[data-testid="notification-channel-row"]').should('have.length', 1)
    cy.get('[data-testid="telegram-bot-token"]').should('have.value', '')
    cy.get('[data-testid="telegram-chat-id"]').should('have.value', '-1001234567890')
  })

  it('adds Telegram with a replacement token and submits it for testing', () => {
    cy.stubApi()
    cy.intercept('PUT', '/api/credentials', (req) => {
      expect(req.body.notification_channels).to.deep.equal([
        { type: 'serverchan', severity_floor: 'INFO' },
        {
          type: 'telegram',
          severity_floor: 'INFO',
          bot_token: '123456:replacement-token',
          chat_id: '-1009876543210',
        },
      ])
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
          notification_channels: req.body.notification_channels,
          updated_at: '2026-01-01T00:00:00Z',
          reload_warning: null,
        },
      })
    }).as('saveNewTelegramCredentials')
    cy.visit('/#/credentials')
    cy.wait('@getCredentials')

    cy.contains('button', '添加渠道').click()
    cy.get('[data-testid="notification-channel-row"]').eq(1).find('.el-select').first().click()
    cy.get('.el-select-dropdown__item:visible').contains('Telegram').click()
    cy.get('[data-testid="notification-channel-row"]').eq(1).within(() => {
      cy.get('[data-testid="telegram-bot-token"]').type('123456:replacement-token')
      cy.get('[data-testid="telegram-chat-id"]').type('-1009876543210')
      cy.get('[data-testid="channel-test-btn"]').click()
    })
    cy.wait('@testNotificationChannel').its('request.body').should('deep.equal', {
      type: 'telegram',
      severity_floor: 'INFO',
      bot_token: '123456:replacement-token',
      chat_id: '-1009876543210',
    })
    cy.contains('button', '保存').click()
    cy.wait('@saveNewTelegramCredentials')
  })

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
