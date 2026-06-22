from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import PortfolioConfig as PortfolioConfigModel
from app.platform.portfolio_config import PortfolioConfig


class PortfolioService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_configs(self) -> list[PortfolioConfig]:
        rows = self.db.query(PortfolioConfigModel).order_by(PortfolioConfigModel.id.desc()).all()
        return [self._to_domain(row) for row in rows]

    def get_config(self, name: str) -> PortfolioConfig | None:
        row = self.db.query(PortfolioConfigModel).filter(PortfolioConfigModel.name == name).first()
        if row is None:
            return None
        return self._to_domain(row)

    def save_config(self, config: PortfolioConfig) -> PortfolioConfig:
        row = self.db.query(PortfolioConfigModel).filter(PortfolioConfigModel.name == config.name).first()
        data = {
            "symbols_json": json.dumps(config.symbols),
            "allocations_json": json.dumps({k: str(v) for k, v in config.allocations.items()}),
            "per_symbol_risk_json": json.dumps({k: str(v) for k, v in config.per_symbol_risk_budget.items()}),
            "rebalance_threshold_pct": float(config.rebalance_threshold_pct),
            "max_gross_exposure": float(config.max_gross_exposure),
            "max_net_exposure": float(config.max_net_exposure),
            "enabled": config.enabled,
        }
        if row is None:
            row = PortfolioConfigModel(name=config.name, **data)
            self.db.add(row)
        else:
            for k, v in data.items():
                setattr(row, k, v)
        self.db.commit()
        self.db.refresh(row)
        return self._to_domain(row)

    def _to_domain(self, row: PortfolioConfigModel) -> PortfolioConfig:
        return PortfolioConfig(
            name=row.name,
            symbols=json.loads(row.symbols_json),
            allocations={k: Decimal(v) for k, v in json.loads(row.allocations_json).items()},
            per_symbol_risk_budget={k: Decimal(v) for k, v in json.loads(row.per_symbol_risk_json).items()},
            rebalance_threshold_pct=Decimal(str(row.rebalance_threshold_pct)),
            max_gross_exposure=Decimal(str(row.max_gross_exposure)),
            max_net_exposure=Decimal(str(row.max_net_exposure)),
            enabled=row.enabled,
        )
