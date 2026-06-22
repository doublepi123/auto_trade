import os
os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_portfolio_config.db"

from app.database import engine, init_db
from app.models import Base, PortfolioConfig
from sqlalchemy.orm import Session


def test_portfolio_config_roundtrip():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        cfg = PortfolioConfig(
            name="test-portfolio",
            symbols_json='["AAPL.US","TSLA.US"]',
            allocations_json='{"AAPL.US":0.6,"TSLA.US":0.4}',
            per_symbol_risk_json='{"AAPL.US":0.05,"TSLA.US":0.05}',
            rebalance_threshold_pct=5.0,
            max_gross_exposure=1.0,
            max_net_exposure=0.5,
        )
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
        assert cfg.id is not None
        assert cfg.name == "test-portfolio"
        assert cfg.enabled is True

        fetched = db.query(PortfolioConfig).filter_by(name="test-portfolio").first()
        assert fetched is not None
        assert fetched.symbols_json == '["AAPL.US","TSLA.US"]'


def test_portfolio_config_table_ensured_by_init_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with Session(engine) as db:
        cfg = PortfolioConfig(name="init-db-portfolio", symbols_json="[]", allocations_json="{}", per_symbol_risk_json="{}")
        db.add(cfg)
        db.commit()
        fetched = db.query(PortfolioConfig).filter_by(name="init-db-portfolio").first()
        assert fetched is not None
