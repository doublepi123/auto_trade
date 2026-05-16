describe('Credentials', () => {
  beforeEach(() => {
    cy.intercept('GET', '/api/credentials', {
      id: 1,
      longbridge_app_key: '',
      longbridge_app_secret: '',
      longbridge_access_token: '',
      sct_key: '',
      has_longbridge_app_key: true,
      has_longbridge_app_secret: false,
      has_longbridge_access_token: false,
      has_sct_key: false,
      updated_at: '2026-01-01T00:00:00Z',
    }).as('getCredentials')
    cy.visit('/#/credentials')
    cy.wait('@getCredentials')
    cy.get('h3', { timeout: 10000 }).should('contain', '凭证设置')
  })

  it('displays credentials form title', () => {
    cy.get('h3').should('contain', '凭证设置')
  })

  it('shows credential input labels', () => {
    cy.get('.el-form-item__label').should('contain', '长桥应用标识')
    cy.get('.el-form-item__label').should('contain', '长桥应用密钥')
    cy.get('.el-form-item__label').should('contain', '长桥访问令牌')
  })

  it('shows exact blank-field helper text', () => {
    cy.contains('留空表示保留当前凭证；如需清除，请使用清除按钮。').should('be.visible')
  })

  it('has save button', () => {
    cy.get('button.el-button--primary').should('be.visible')
  })

  it('does not submit when all credential fields are blank', () => {
    cy.intercept('PUT', '/api/credentials').as('updateCredentials')

    cy.get('button.el-button--primary').click()

    cy.contains('没有需要保存的凭证变更').should('be.visible')
    cy.get('@updateCredentials.all').should('have.length', 0)
  })

  it('submits only non-empty credential fields', () => {
    cy.intercept('PUT', '/api/credentials', (req) => {
      expect(req.body).to.deep.equal({ longbridge_app_key: 'new-key' })
      req.reply({
        id: 1,
        longbridge_app_key: '',
        longbridge_app_secret: '',
        longbridge_access_token: '',
        sct_key: '',
        has_longbridge_app_key: true,
        has_longbridge_app_secret: false,
        has_longbridge_access_token: false,
        has_sct_key: false,
        updated_at: '2026-01-01T00:00:00Z',
      })
    }).as('updateCredentials')

    cy.get('input[placeholder="留空则保留当前应用标识"]').type('new-key')
    cy.get('button.el-button--primary').click()

    cy.wait('@updateCredentials')
    cy.contains('已保存').should('be.visible')
  })

  it('submits credentials only once when save is clicked rapidly', () => {
    cy.intercept('PUT', '/api/credentials', (req) => {
      expect(req.body).to.deep.equal({ longbridge_app_key: 'new-key' })
      req.reply({
        delay: 200,
        body: {
          id: 1,
          longbridge_app_key: '',
          longbridge_app_secret: '',
          longbridge_access_token: '',
          sct_key: '',
          has_longbridge_app_key: true,
          has_longbridge_app_secret: false,
          has_longbridge_access_token: false,
          has_sct_key: false,
          updated_at: '2026-01-01T00:00:00Z',
        },
      })
    }).as('updateCredentials')

    cy.get('input[placeholder="留空则保留当前应用标识"]').type('new-key')
    cy.get('button.el-button--primary').click().click({ force: true })

    cy.wait('@updateCredentials')
    cy.get('@updateCredentials.all').should('have.length', 1)
  })

  it('clears one configured credential field explicitly', () => {
    cy.intercept('PUT', '/api/credentials', (req) => {
      expect(req.body).to.deep.equal({ longbridge_app_key: '' })
      req.reply({
        id: 1,
        longbridge_app_key: '',
        longbridge_app_secret: '',
        longbridge_access_token: '',
        sct_key: '',
        has_longbridge_app_key: false,
        has_longbridge_app_secret: false,
        has_longbridge_access_token: false,
        has_sct_key: false,
        updated_at: '2026-01-01T00:00:00Z',
      })
    }).as('clearCredential')

    cy.contains('button', '清除').click()

    cy.wait('@clearCredential')
    cy.contains('已保存').should('be.visible')
  })

  it('clears one credential only once when clear is clicked rapidly', () => {
    cy.intercept('PUT', '/api/credentials', (req) => {
      expect(req.body).to.deep.equal({ longbridge_app_key: '' })
      req.reply({
        delay: 200,
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
          updated_at: '2026-01-01T00:00:00Z',
        },
      })
    }).as('clearCredential')

    cy.contains('button', '清除').click().click({ force: true })

    cy.wait('@clearCredential')
    cy.get('@clearCredential.all').should('have.length', 1)
  })
})
