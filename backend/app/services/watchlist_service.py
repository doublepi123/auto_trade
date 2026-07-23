from __future__ import annotations

from app.models import WatchlistItem
from app.schemas import WatchlistItemSchema
from sqlalchemy.orm import Session

class WatchlistService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_items(self) -> list[WatchlistItem]:
        return self.db.query(WatchlistItem).order_by(WatchlistItem.created_at.desc()).all()

    def get_item(self, item_id: int) -> WatchlistItem | None:
        return self.db.query(WatchlistItem).filter(WatchlistItem.id == item_id).first()

    def add_item(
        self,
        data: WatchlistItemSchema,
        *,
        source: str = "manual",
    ) -> WatchlistItem:
        existing = self.db.query(WatchlistItem).filter(WatchlistItem.symbol == data.symbol).first()
        if existing:
            raise ValueError(f"Symbol {data.symbol} already in watchlist")
        item = WatchlistItem(
            symbol=data.symbol,
            market=data.market,
            alias=data.alias,
            source=(source or "manual")[:32],
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def remove_item(self, item_id: int) -> bool:
        item = self.get_item(item_id)
        if not item:
            return False
        self.db.delete(item)
        self.db.commit()
        return True

    def set_trading_symbol(self, item_id: int) -> WatchlistItem:
        item = self.get_item(item_id)
        if not item:
            raise ValueError("Watchlist item not found")
        
        # Clear all active flags
        self.db.query(WatchlistItem).update({WatchlistItem.is_active: False})
        
        # Set this one active
        item.is_active = True
        self.db.commit()
        self.db.refresh(item)
        return item

    def get_active_item(self) -> WatchlistItem | None:
        return self.db.query(WatchlistItem).filter(WatchlistItem.is_active.is_(True)).first()
