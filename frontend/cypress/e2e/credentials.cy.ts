describe('Credentials', () => {
  beforeEach(function () {
    if (this.currentTest?.title.includes('during initial load')) return

    cy.visitApp('/#/credentials')
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

  it('has save button', () => {
    cy.get('button.el-button--primary').should('be.visible')
  })

  it('disables credential form and shows loading state during initial load', () => {
    cy.intercept('GET', '/api/credentials', (req) => {
      return new Promise<void>((resolve) => {
        setTimeout(() => {
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
              updated_at: '2026-01-01T00:00:00Z',
            },
          })
          resolve()
        }, 2000)
      })
    }).as('slowCredentials')

    cy.visit('/#/credentials')
    cy.contains('凭证状态加载中...').should('be.visible')
    cy.contains('label', '长桥应用标识').parents('.el-form-item').first().find('input').should('be.disabled')
    cy.contains('button', '保存').should('be.disabled')

    cy.wait('@slowCredentials')
    cy.contains('凭证状态加载中...').should('not.exist')
    cy.contains('label', '长桥应用标识').parents('.el-form-item').first().find('input').should('not.be.disabled')
  })
})
