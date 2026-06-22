import pytest
from decimal import Decimal

from app.database import engine, init_db
from app.models import Base, PortfolioConfig
from app.platform.portfolio_config import PortfolioConfig as PortfolioConfigDataclass
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


def test_portfolio_config_validates_allocations_sum_to_one():
    with pytest.raises(ValueError, match="allocations must sum to 1"):
        PortfolioConfigDataclass(
            name="bad",
            symbols=["AAPL.US", "TSLA.US"],
            allocations={"AAPL.US": Decimal("0.6"), "TSLA.US": Decimal("0.5")},
        )


def test_portfolio_config_orm_roundtrip():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        cfg = PortfolioConfig(name="init-db-portfolio", symbols_json="[]", allocations_json="{}", per_symbol_risk_json="{}")
        db.add(cfg)
        db.commit()
        fetched = db.query(PortfolioConfig).filter_by(name="init-db-portfolio").first()
        assert fetched is not None


def test_portfolio_config_rejects_empty_symbols():
    with pytest.raises(ValueError, match="symbols must not be empty"):
        PortfolioConfigDataclass(
            name="empty",
            symbols=[],
            allocations={},
        )


def test_portfolio_config_rejects_negative_threshold():
    with pytest.raises(ValueError, match="rebalance_threshold_pct must be positive"):
        PortfolioConfigDataclass(
            name="bad-threshold",
            symbols=["AAPL.US"],
            allocations={"AAPL.US": Decimal("1")},
            rebalance_threshold_pct=Decimal("0"),
        )


def test_portfolio_config_rejects_mismatched_allocation_keys():
    with pytest.raises(ValueError, match="allocations keys must match symbols"):
        PortfolioConfigDataclass(
            name="bad-alloc",
            symbols=["AAPL.US", "TSLA.US"],
            allocations={"AAPL.US": Decimal("1")},
        )


def test_portfolio_config_rejects_non_positive_exposure():
    with pytest.raises(ValueError, match="max_gross_exposure must be positive"):
        PortfolioConfigDataclass(
            name="bad-gross",
            symbols=["AAPL.US"],
            allocations={"AAPL.US": Decimal("1")},
            max_gross_exposure=Decimal("0"),
        )
    with pytest.raises(ValueError, match="max_net_exposure must be positive"):
        PortfolioConfigDataclass(
            name="bad-net",
            symbols=["AAPL.US"],
            allocations={"AAPL.US": Decimal("1")},
            max_net_exposure=Decimal("-1"),
        )


def test_portfolio_config_auto_default_risk_budget():
    cfg = PortfolioConfigDataclass(
        name="auto-risk",
        symbols=["AAPL.US", "TSLA.US"],
        allocations={"AAPL.US": Decimal("0.5"), "TSLA.US": Decimal("0.5")},
    )
    assert cfg.per_symbol_risk_budget == {"AAPL.US": Decimal("0.05"), "TSLA.US": Decimal("0.05")}


def test_portfolio_config_rejects_mismatched_risk_budget_keys():
    with pytest.raises(ValueError, match="per_symbol_risk_budget keys must match symbols"):
        PortfolioConfigDataclass(
            name="bad-risk-budget",
            symbols=["AAPL.US", "TSLA.US"],
            allocations={"AAPL.US": Decimal("0.5"), "TSLA.US": Decimal("0.5")},
            per_symbol_risk_budget={"AAPL.US": Decimal("0.05")},
        )


def test_portfolio_config_json_roundtrip():
    original = PortfolioConfigDataclass(
        name="roundtrip-test",
        symbols=["AAPL.US", "TSLA.US"],
        allocations={"AAPL.US": Decimal("0.6"), "TSLA.US": Decimal("0.4")},
        per_symbol_risk_budget={"AAPL.US": Decimal("0.05"), "TSLA.US": Decimal("0.05")},
        rebalance_threshold_pct=Decimal("5"),
        max_gross_exposure=Decimal("1.0"),
        max_net_exposure=Decimal("0.8"),
        enabled=True,
    )
    raw = original.to_json()
    restored = PortfolioConfigDataclass.from_json(raw)
    assert restored.name == original.name
    assert restored.symbols == original.symbols
    assert restored.allocations == original.allocations
    assert restored.per_symbol_risk_budget == original.per_symbol_risk_budget
    assert restored.rebalance_threshold_pct == original.rebalance_threshold_pct
    assert restored.max_gross_exposure == original.max_gross_exposure
    assert restored.max_net_exposure == original.max_net_exposure
    assert restored.enabled == original.enabled


def test_portfolio_config_from_json_rejects_missing_required_fields():
    with pytest.raises(ValueError, match="Missing required fields"):
        PortfolioConfigDataclass.from_json('{"name": "x"}')


def test_portfolio_config_from_json_uses_defaults_for_optional_fields():
    cfg = PortfolioConfigDataclass.from_json(
        '{"name": "defaults", "symbols": ["AAPL.US"], "allocations": {"AAPL.US": "1"}}'
    )
    assert cfg.rebalance_threshold_pct == Decimal("5")
    assert cfg.max_gross_exposure == Decimal("1.0")
    assert cfg.max_net_exposure == Decimal("1.0")
    assert cfg.enabled is True
    assert cfg.per_symbol_risk_budget == {"AAPL.US": Decimal("0.05")}
