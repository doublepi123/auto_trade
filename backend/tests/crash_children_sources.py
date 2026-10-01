"""Child-process sources for the REAL lifecycle/recovery crash tests.

Kept in a dedicated module so the triple-quoted child code sits at MODULE
level (uniform 4-space indent inside the string, correct for
``textwrap.dedent``) rather than being nested inside test methods.
"""
from __future__ import annotations

import textwrap

LIFECYCLE_CHILD = textwrap.dedent(
    """
    import os, sys, json
    sys.path.insert(0, {backend_root!r})
    phase, db_path, journal_path = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.argv = [sys.argv[0]]
    os.environ.setdefault("AUTO_TRADE_ENV", "test")

    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.risk import RiskController
    from app.core.notify import ServerChanNotifier
    from app.database import _ensure_passive_mandates_table
    from app.domain.passive_allocation import policy as passive_policy
    from app.domain.passive_allocation import protocol as pp
    from app.domain.passive_allocation.model import (
        PASSIVE_LANE, PASSIVE_SYMBOL, POLICY_VERSION,
    )
    from app.models import Base, PassiveMandate
    from app.services.passive_allocation_service import (
        PassiveAllocationService,
    )
    from app.services import trade_execution_service as _tes
    _tes.is_trading_hours = lambda _m: True
    from app.services.trade_execution_service import (
        FinalOrderQuoteCheckResult, TradeExecutionService,
    )
    from app.core.broker import Quote, OrderResult

    NOW = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)

    class Cash:
        def __init__(self, amount):
            self.amount = amount
            self.currency = "USD"
            self.request_started_at = NOW - timedelta(seconds=2)
            self.request_completed_at = NOW - timedelta(seconds=1)
            self.provenance = pp.PASSIVE_CASH_PROVENANCE

    class JournalBroker:
        def __init__(self, journal_path, phase):
            self.journal_path = journal_path
            self.phase = phase
            self.submitted = 0
        def get_positions(self):
            return []
        def get_cash(self, currency=None):
            return Decimal("10000")
        def get_strict_usd_cash_snapshot(self):
            return Cash(Decimal("10000"))
        def estimate_margin_max_quantity(self, *a, **k):
            return Decimal("1000")
        def submit_limit_order(self, symbol, side, quantity, price):
            self.submitted += 1
            order_id = "lc-%d" % self.submitted
            entry = {{"event": "submit", "order_id": order_id,
                     "symbol": symbol, "side": side,
                     "quantity": str(quantity), "price": str(price)}}
            with open(self.journal_path, "a") as fh:
                fh.write(json.dumps(entry) + "\\n")
                fh.flush(); os.fsync(fh.fileno())
            if self.phase == "accepted-before-return":
                os._exit(9)
            return OrderResult(order_id, symbol, side, quantity, price,
                               "SUBMITTED")

    def qchk(_b, _s, _a, price):
        return FinalOrderQuoteCheckResult(executable_price=price, bid=price,
                                          ask=price)

    engine = create_engine("sqlite:///" + db_path,
                           connect_args={{"timeout": 30}})
    Base.metadata.create_all(engine)
    _ensure_passive_mandates_table(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def record_then_die(order_id, symbol, side, qty, price, status,
                        *metadata):
        if phase == "idbound-before-record":
            # The mandate bind (record_outcome) has already committed by
            # the time record_order runs; die BEFORE the ordinary
            # orders-table write, with the bind durable in the DB.
            os._exit(9)
        return None

    execution = TradeExecutionService(
        record_order=record_then_die,
        update_order_status=lambda *a, **k: None,
        record_risk_event=lambda *a, **k: None,
        max_position_quantity=100,
        max_position_notional=5000.0,
        max_risk_per_trade=250.0,
        stop_loss_pct=1.0,
        final_order_quote_check=qchk,
    )
    passive = PassiveAllocationService(
        execution=execution, session_factory=sessions,
        lane_enabled_reader=lambda: True,
        paper_account_confirmed_reader=lambda: True, clock=lambda: NOW,
    )
    execution.passive_submit_hooks = passive.build_hook_bundle()
    broker = JournalBroker(journal_path, phase)

    with sessions() as db:
        db.add(PassiveMandate(
            lane=PASSIVE_LANE, policy_version=POLICY_VERSION,
            symbol=PASSIVE_SYMBOL, status="ACTIVE", allotment_usd=5000.0,
            risk_model="FULL_PRINCIPAL",
            exemptions=",".join(passive_policy.REQUIRED_EXEMPTIONS),
            review_interval_months=6, entry_authorisation_available=True,
            approved_at=NOW, approved_by="owner", approval_reason="r",
            order_binding="paper-only",
        ))
        db.commit()

    quote = Quote("SPY.US", 600.0, 599.99, 600.01, "t")

    if phase == "die-after-checking":
        ref = passive.reserve_entry(price=Decimal("600"))
        assert not isinstance(ref, str), ref
        hooks = passive.build_hook_bundle()
        owner = hooks.begin_execution(ref, "exec-lc")
        assert not isinstance(owner, pp.PassiveRejection), owner
        print("LIFECYCLE CHECKING", flush=True)
        os._exit(9)

    if phase == "die-after-submitting":
        ref = passive.reserve_entry(price=Decimal("600"))
        assert not isinstance(ref, str), ref
        hooks = passive.build_hook_bundle()
        owner = hooks.begin_execution(ref, "exec-lc")
        assert not isinstance(owner, pp.PassiveRejection), owner
        cash = Cash(Decimal("10000"))
        order = pp.PassiveOrderSpec(
            symbol=PASSIVE_SYMBOL, side="BUY",
            quantity=Decimal("8"), price=Decimal("600.00"),
        )
        assert hooks.claim_submission(owner, order, cash)
        print("LIFECYCLE SUBMITTING", flush=True)
        os._exit(9)

    if phase in ("accepted-before-return", "idbound-before-record"):
        ref = passive.reserve_entry(price=Decimal("600"))
        assert not isinstance(ref, str), ref
        status = execution.execute_passive_entry(
            ref=ref, quote=quote, broker=broker, risk=RiskController(),
            notifier=ServerChanNotifier(""),
        )
        print("LIFECYCLE done", getattr(status, "status", None), flush=True)
        os._exit(0)
    """,
)

RUNNER_CHILD = textwrap.dedent(
    """
    import os, sys, json
    sys.path.insert(0, {backend_root!r})
    db_path, journal_path = sys.argv[1], sys.argv[2]
    sys.argv = [sys.argv[0]]
    os.environ.setdefault("AUTO_TRADE_ENV", "test")
    os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///" + db_path

    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from app.core.broker import BrokerGateway
    from app.api.deps import init_audit_logger
    from app.runner import AppRunner

    NOW = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)

    class Cash:
        def __init__(self, amount):
            self.amount = amount
            self.currency = "USD"
            self.request_started_at = NOW - timedelta(seconds=2)
            self.request_completed_at = NOW - timedelta(seconds=1)
            self.provenance = "account_balance.cash_infos.available_cash"

    class RecoverBroker(BrokerGateway):
        def __init__(self, journal_path):
            self.journal_path = journal_path
            self.submitted = 0
            self.cancelled = 0
        def get_positions(self):
            return []
        def get_today_orders(self):
            return []
        def close(self):
            pass
        def register_disconnect_hook(self, callback):
            pass
        def subscribe_quotes_batch(self, symbols, callback):
            pass
        def get_cash(self, currency=None):
            return Decimal("10000")
        def get_strict_usd_cash_snapshot(self):
            return Cash(Decimal("10000"))
        def estimate_margin_max_quantity(self, *a, **k):
            return Decimal("1000")
        def submit_limit_order(self, *a, **k):
            self.submitted += 1
            with open(self.journal_path, "a") as fh:
                fh.write(json.dumps({{"event": "recovery_submit"}}) + "\\n")
                fh.flush(); os.fsync(fh.fileno())
            raise AssertionError("recovery must never submit")
        def cancel_order(self, *a, **k):
            self.cancelled += 1
            raise AssertionError("recovery must never cancel")
        def get_order_status(self, order_id):
            from app.core.broker import OrderStatusResult
            return OrderStatusResult(
                broker_order_id=order_id, status="SUBMITTED",
                executed_quantity=None, executed_price=None,
            )

    from app.core.risk import RiskController
    from app.core.engine import StrategyEngine
    from app.services.trade_execution_service import (
        FinalOrderQuoteCheckResult, TradeExecutionService,
    )
    from app.services.reconciliation_incident_service import (
        ReconciliationIncidentService,
    )

    from pathlib import Path
    from tests.test_runner_passive_startup_integration import _startup_environment
    from app.core.risk import ResumeBlockedError
    broker = RecoverBroker(journal_path)
    with _startup_environment(Path(db_path), broker) as (constructor, sessions):
        runner = constructor()
        runner._initialize_runner()
        block = runner.risk.external_block()
        resume_denied = False
        try:
            runner.risk.resume()
        except ResumeBlockedError:
            resume_denied = True
        refusal = runner._trade_svc._final_submission_precheck(
            "BUY", "SPY.US", Decimal("8"), Decimal("600"), broker, runner.risk,
        )
        assert refusal.status == "SKIPPED", refusal
        assert "external safety block" in refusal.reason, refusal
        print("RUNNER_RECOVER " + json.dumps({{
            "block": (block.reason[:80] if block else None),
            "quarantined": sorted(runner._passive_quarantined_symbols),
            "hooks_wired": runner._trade_svc.passive_submit_hooks is not None,
            "broker_submits": broker.submitted,
            "broker_cancels": broker.cancelled,
            "full_initialize": runner._passive_recovery_complete,
            "resume_denied": resume_denied,
            "new_exposure_denied": refusal.status == "SKIPPED",
        }}), flush=True)
    os._exit(0)
    """,
)
