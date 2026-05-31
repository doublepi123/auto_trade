describe('Strategy Experiments', () => {
  beforeEach(() => {
    cy.visitApp('/#/experiments')
  })

  it('renders experiments page with create card', () => {
    cy.get('[data-testid="experiments-page"]').should('be.visible')
    cy.get('[data-testid="create-experiment-card"]').should('be.visible')
    cy.contains('创建实验').should('be.visible')
    cy.get('[data-testid="exp-run-btn"]').should('be.visible')
  })

  it('shows error when CSV is empty and does not call create', () => {
    let createCalled = false
    cy.intercept('POST', '/api/strategy-experiments', (req) => {
      createCalled = true
      req.reply({ statusCode: 201, body: { id: 1 } })
    })
    cy.get('[data-testid="exp-run-btn"]').click()
    cy.contains('请填入价格数据 CSV').should('be.visible')
    cy.wrap(null).then(() => {
      expect(createCalled).to.be.false
    })
  })

  it('runs experiment and displays leaderboard with sorted metrics', () => {
    // Fill form
    cy.get('[data-testid="exp-name"]').type('Test AAPL Grid')
    cy.get('[data-testid="exp-symbol"]').type('AAPL.US')
    cy.get('[data-testid="exp-grid-buy-low"]').type('178,180')
    cy.get('[data-testid="exp-grid-sell-high"]').type('189,190')
    cy.get('[data-testid="exp-csv"]').type(
      'timestamp,open,high,low,close,volume\n' +
      '2026-05-01T09:30:00Z,180,181,179,180.5,1000\n' +
      '2026-05-01T09:31:00Z,180.5,182,180,181.5,1200',
    )

    // Intercept create
    cy.intercept('POST', '/api/strategy-experiments', {
      statusCode: 200,
      body: {
        id: 1,
        name: 'Test AAPL Grid',
        symbol: 'AAPL.US',
        base_params_json: '{}',
        parameter_grid_json: '{}',
        status: 'PENDING',
        estimated_runs: 4,
        completed_runs: 0,
        failed_runs: 0,
        error: '',
        created_at: '2026-01-01T00:00:00Z',
        completed_at: null,
      },
    }).as('createExp')

    // Intercept run
    cy.intercept('POST', '/api/strategy-experiments/1/run', {
      statusCode: 200,
      body: {
        id: 1,
        name: 'Test AAPL Grid',
        symbol: 'AAPL.US',
        base_params_json: '{}',
        parameter_grid_json: '{}',
        status: 'COMPLETED',
        estimated_runs: 4,
        completed_runs: 4,
        failed_runs: 0,
        error: '',
        created_at: '2026-01-01T00:00:00Z',
        completed_at: '2026-01-01T00:00:10Z',
      },
    }).as('runExp')

    // Intercept runs list
    cy.intercept('GET', '/api/strategy-experiments/1/runs*', {
      statusCode: 200,
      body: {
        items: [
          {
            id: 1,
            experiment_id: 1,
            parameters: { buy_low: 178, sell_high: 189 },
            status: 'COMPLETED',
            total_pnl: 120.5,
            total_return_pct: 0.12,
            max_drawdown_pct: 0.02,
            win_rate: 0.5,
            trade_count: 4,
            closed_trade_count: 4,
            error: null,
            created_at: '2026-01-01T00:00:00Z',
          },
          {
            id: 2,
            experiment_id: 1,
            parameters: { buy_low: 180, sell_high: 190 },
            status: 'COMPLETED',
            total_pnl: 80.0,
            total_return_pct: 0.08,
            max_drawdown_pct: 0.03,
            win_rate: 0.6,
            trade_count: 3,
            closed_trade_count: 3,
            error: null,
            created_at: '2026-01-01T00:00:01Z',
          },
        ],
        total: 2,
        page: 1,
        page_size: 20,
      },
    }).as('listRuns')

    // Click run
    cy.get('[data-testid="exp-run-btn"]').click()

    // Wait for API calls
    cy.wait('@createExp')
    cy.wait('@runExp')
    cy.wait('@listRuns')

    // Assert leaderboard visible
    cy.get('[data-testid="leaderboard-card"]').should('be.visible')
    cy.get('[data-testid="leaderboard-table"]').should('be.visible')

    // Assert runs render with metrics
    cy.get('[data-testid="run-params"]').first().should('contain', 'buy_low')
    cy.get('[data-testid="run-pnl"]').first().should('contain', '120.50')
    cy.get('[data-testid="run-return"]').first().should('contain', '12.00%')
    cy.get('[data-testid="run-drawdown"]').first().should('contain', '2.00%')
    cy.get('[data-testid="run-win-rate"]').first().should('contain', '50.0%')

    // Assert sort controls exist
    cy.get('[data-testid="sort-field-select"]').should('be.visible')
    cy.get('[data-testid="sort-order-select"]').should('be.visible')

    // Assert second row exists
    cy.get('[data-testid="run-pnl"]').eq(1).should('contain', '80.00')
  })

  it('has desktop nav item for experiments', () => {
    cy.viewport(1024, 768)
    cy.get('[data-testid="desktop-nav"]').within(() => {
      cy.contains('策略实验').should('be.visible')
    })
  })
})
