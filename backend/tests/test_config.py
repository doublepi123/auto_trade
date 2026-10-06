from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


class TestSettings:
    def test_default_values(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("AUTO_TRADE_ENV", raising=False)
        monkeypatch.delenv("AUTO_TRADE_DATABASE_URL", raising=False)
        # Read the true code defaults, not the developer-local .env (which the
        # Settings env_file would otherwise pick up and mask the defaults).
        monkeypatch.chdir(tmp_path)
        s = Settings()
        assert s.env == "dev"
        assert s.database_url == "sqlite:///./data/auto_trade.db"
        assert s.universe_selection_enabled is False
        assert s.universe_selection_apply_to_watchlist is False
        assert s.universe_selection_enable_shadow is False
        assert s.strategy_v2_portfolio_shadow_enabled is False
        assert s.universe_selection_max_symbols == 12
        assert s.universe_selection_exploration_max_symbols == 24
        assert (
            s.universe_selection_exploration_top_score_challengers
            == 2
        )
        assert s.live_regime_gate_enabled is False
        assert s.live_regime_max_data_age_seconds == 600
        assert s.live_max_entries_per_symbol_per_day == 1
        assert s.live_entry_crossing_required is False
        assert s.live_entry_crossing_max_age_seconds == 30
        assert s.watchlist_quant_v6_evaluation_enabled is False
        assert s.watchlist_quant_v6_evaluation_interval_minutes == 1_440
        assert (
            s.watchlist_quant_v6_evaluation_retry_interval_minutes == 60
        )
        assert s.watchlist_quant_v6_evaluation_timeout_seconds == 1_800
        assert s.watchlist_quant_v6_provider_page_timeout_seconds == 30.0
        assert s.watchlist_quant_v6_compute_workers == 4
        assert s.watchlist_quant_v6_pipeline_memory_limit_mib == 2_048
        assert s.degraded_exit_max_adverse_deviation_pct == 0.5
        assert s.degraded_exit_reference_max_age_seconds == 300

    @pytest.mark.parametrize("value", ["30", "0", "1801"])
    def test_degraded_exit_reference_age_rejects_values_at_or_below_quote_freshness(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_DEGRADED_EXIT_REFERENCE_MAX_AGE_SECONDS",
            value,
        )

        with pytest.raises(ValidationError):
            Settings()

    @pytest.mark.parametrize("value", ["0", "1.01", "5"])
    def test_degraded_exit_deviation_rejects_values_wider_than_the_hard_stop(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_DEGRADED_EXIT_MAX_ADVERSE_DEVIATION_PCT",
            value,
        )

        with pytest.raises(ValidationError):
            Settings()

    def test_quant_v6_evaluation_controls_read_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_EVALUATION_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_EVALUATION_INTERVAL_MINUTES",
            "360",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_EVALUATION_RETRY_INTERVAL_MINUTES",
            "30",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_EVALUATION_TIMEOUT_SECONDS",
            "900",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_COMPUTE_WORKERS",
            "3",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_PIPELINE_MEMORY_LIMIT_MIB",
            "4096",
        )

        configured = Settings()

        assert configured.watchlist_quant_v6_evaluation_enabled is True
        assert configured.watchlist_quant_v6_evaluation_interval_minutes == 360
        assert (
            configured.watchlist_quant_v6_evaluation_retry_interval_minutes
            == 30
        )
        assert configured.watchlist_quant_v6_evaluation_timeout_seconds == 900
        assert configured.watchlist_quant_v6_compute_workers == 3
        assert configured.watchlist_quant_v6_pipeline_memory_limit_mib == 4_096

    @pytest.mark.parametrize("value", ["59", "10081"])
    def test_quant_v6_evaluation_interval_is_bounded(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_EVALUATION_INTERVAL_MINUTES",
            value,
        )

        with pytest.raises(ValidationError):
            Settings()

    @pytest.mark.parametrize("value", ["14", "1441"])
    def test_quant_v6_evaluation_retry_interval_is_bounded(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_EVALUATION_RETRY_INTERVAL_MINUTES",
            value,
        )

        with pytest.raises(ValidationError):
            Settings()

    def test_quant_v6_retry_cannot_exceed_regular_interval(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_EVALUATION_INTERVAL_MINUTES",
            "60",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_EVALUATION_RETRY_INTERVAL_MINUTES",
            "61",
        )

        with pytest.raises(
            ValidationError,
            match="retry interval must not exceed",
        ):
            Settings()

    @pytest.mark.parametrize("value", ["60", "7200"])
    def test_quant_v6_evaluation_timeout_accepts_boundaries(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_EVALUATION_TIMEOUT_SECONDS",
            value,
        )

        configured = Settings()

        assert configured.watchlist_quant_v6_evaluation_timeout_seconds == int(
            value
        )

    @pytest.mark.parametrize("value", ["59", "7201", "not-a-number"])
    def test_quant_v6_evaluation_timeout_rejects_invalid_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_EVALUATION_TIMEOUT_SECONDS",
            value,
        )

        with pytest.raises(ValidationError):
            Settings()

    @pytest.mark.parametrize("value", ["4.99", "120.01", "nan"])
    def test_quant_v6_provider_page_timeout_is_bounded(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_PROVIDER_PAGE_TIMEOUT_SECONDS",
            value,
        )

        with pytest.raises(ValidationError):
            Settings()

    @pytest.mark.parametrize("value", ["2", "4"])
    def test_quant_v6_compute_workers_accepts_boundaries(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_COMPUTE_WORKERS",
            value,
        )

        configured = Settings()

        assert configured.watchlist_quant_v6_compute_workers == int(value)

    @pytest.mark.parametrize("value", ["0", "1", "5", "not-a-number"])
    def test_quant_v6_compute_workers_rejects_invalid_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_COMPUTE_WORKERS",
            value,
        )

        with pytest.raises(ValidationError):
            Settings()

    @pytest.mark.parametrize("value", ["512", "8192"])
    def test_quant_v6_pipeline_memory_limit_accepts_boundaries(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_PIPELINE_MEMORY_LIMIT_MIB",
            value,
        )

        configured = Settings()

        assert (
            configured.watchlist_quant_v6_pipeline_memory_limit_mib
            == int(value)
        )

    @pytest.mark.parametrize("value", ["511", "8193", "not-a-number"])
    def test_quant_v6_pipeline_memory_limit_rejects_invalid_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_V6_PIPELINE_MEMORY_LIMIT_MIB",
            value,
        )

        with pytest.raises(ValidationError):
            Settings()

    def test_default_strategy_empty(self) -> None:
        s = Settings()
        assert s.default_strategy["symbol"] == ""
        assert s.default_strategy["market"] == "US"

    def test_notification_dedup_window_defaults_and_reads_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AUTO_TRADE_NOTIFY_DEDUP_WINDOW_SECONDS", raising=False)
        assert Settings().notify_dedup_window_seconds == 300.0

        monkeypatch.setenv("AUTO_TRADE_NOTIFY_DEDUP_WINDOW_SECONDS", "12.5")
        assert Settings().notify_dedup_window_seconds == 12.5

    def test_interval_recenter_half_width_defaults_to_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Unset must preserve today's behaviour exactly: the recenter falls
        # back to llm_interval_volatility_threshold_pct.
        monkeypatch.delenv(
            "AUTO_TRADE_INTERVAL_RECENTER_HALF_WIDTH_PCT", raising=False
        )
        s = Settings()
        assert s.interval_recenter_half_width_pct is None
        assert s.recenter_half_width_pct() == s.llm_interval_volatility_threshold_pct

    def test_interval_recenter_half_width_reads_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_INTERVAL_RECENTER_HALF_WIDTH_PCT", "0.3")

        s = Settings()
        assert s.interval_recenter_half_width_pct == 0.3
        # The override must not leak into the volatility threshold itself,
        # which still drives LLM re-analysis and interval application.
        assert s.llm_interval_volatility_threshold_pct == 1.0
        assert s.recenter_half_width_pct() == 0.3

    def test_interval_recenter_half_width_empty_env_is_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Compose forwards `${VAR:-}` (an empty string) when the operator has
        # not set the variable; an empty string must mean "unset", not a
        # validation crash on a float field.
        monkeypatch.setenv("AUTO_TRADE_INTERVAL_RECENTER_HALF_WIDTH_PCT", "")

        s = Settings()
        assert s.interval_recenter_half_width_pct is None
        assert s.recenter_half_width_pct() == s.llm_interval_volatility_threshold_pct

    @pytest.mark.parametrize("value", ["0", "-0.1", "10.1", "nan", "inf"])
    def test_interval_recenter_half_width_rejects_invalid_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_INTERVAL_RECENTER_HALF_WIDTH_PCT", value)

        with pytest.raises(ValidationError):
            Settings()

    def test_production_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTO_TRADE_ENV", "prod")
        monkeypatch.setenv("AUTO_TRADE_API_KEY", "")

        with pytest.raises(
            ValidationError,
            match="AUTO_TRADE_API_KEY is required outside dev/test environments",
        ):
            Settings()

    @pytest.mark.parametrize("environment", ["dev", "test"])
    def test_non_production_allows_empty_api_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        environment: str,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_ENV", environment)
        monkeypatch.setenv("AUTO_TRADE_API_KEY", "")

        assert Settings().api_key == ""

    def test_p0_live_safety_defaults_fail_closed(self) -> None:
        s = Settings()
        assert s.allow_short_entries is False
        assert s.hard_allow_position_addons is False
        assert s.hard_max_position_quantity == 100
        assert s.hard_max_position_notional == 5000
        assert s.hard_max_risk_per_trade == 250
        assert s.full_buying_power_usage_enabled is False
        assert s.entry_round_trip_slippage_bps == 4
        assert s.min_entry_edge_cost_ratio == 2
        assert s.min_entry_reward_risk_ratio == 1
        assert s.trading_open_warmup_minutes == 5
        assert s.live_exit_challenger_enabled is False
        assert s.hard_stop_loss_pct == 1
        assert s.hard_max_holding_minutes == 60
        assert s.hard_entry_cutoff_minutes_before_close == 45
        assert s.hard_flatten_minutes_before_close == 15
        assert s.llm_shadow_mode is True
        assert s.llm_max_order_price_deviation_pct == 1
        assert s.llm_max_interval_bound_deviation_pct == 5

    def test_p0_environment_cannot_loosen_hard_safety_limits(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        overrides = {
            "AUTO_TRADE_ALLOW_SHORT_ENTRIES": "true",
            "AUTO_TRADE_HARD_ALLOW_POSITION_ADDONS": "true",
            "AUTO_TRADE_LLM_SHADOW_MODE": "false",
            "AUTO_TRADE_HARD_MAX_POSITION_QUANTITY": "10000",
            "AUTO_TRADE_HARD_MAX_POSITION_NOTIONAL": "1000000",
            "AUTO_TRADE_HARD_MAX_RISK_PER_TRADE": "100000",
            "AUTO_TRADE_HARD_STOP_LOSS_PCT": "10",
            "AUTO_TRADE_HARD_MAX_HOLDING_MINUTES": "1440",
            "AUTO_TRADE_HARD_ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE": "1",
            "AUTO_TRADE_HARD_FLATTEN_MINUTES_BEFORE_CLOSE": "1",
            "AUTO_TRADE_LLM_MIN_CONFIDENCE": "0.1",
            "AUTO_TRADE_LLM_MAX_STRIPE_WIDTH_PCT": "50",
            "AUTO_TRADE_LLM_MAX_INTERVAL_BOUND_DEVIATION_PCT": "50",
            "AUTO_TRADE_LLM_MAX_ORDER_PRICE_DEVIATION_PCT": "10",
        }
        for name, value in overrides.items():
            monkeypatch.setenv(name, value)

        s = Settings()

        assert s.allow_short_entries is False
        assert s.hard_allow_position_addons is False
        assert s.llm_shadow_mode is True
        assert s.hard_max_position_quantity == 100
        assert s.hard_max_position_notional == 5000
        assert s.hard_max_risk_per_trade == 250
        assert s.hard_stop_loss_pct == 1
        assert s.hard_max_holding_minutes == 60
        assert s.hard_entry_cutoff_minutes_before_close == 45
        assert s.hard_flatten_minutes_before_close == 15
        assert s.llm_min_confidence == 0.7
        assert s.llm_max_stripe_width_pct == 8
        assert s.llm_max_interval_bound_deviation_pct == 5
        assert s.llm_max_order_price_deviation_pct == 1

    def test_paper_experiment_raises_only_the_notional_ceiling(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A notional-only request raises only the notional ceiling.

        Each paper relaxation (quantity, notional, per-trade risk) must be
        separately requested; requesting notional alone leaves the funded
        100-share and $250 caps exactly in place. Whether more notional
        would also need a bigger risk budget depends on the stop; see
        ``test_notional_headroom_above_the_paper_bound_depends_on_the_stop``.
        """
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_MAX_POSITION_NOTIONAL",
            "25000",
        )

        s = Settings()

        assert s.hard_max_position_notional == 25000
        # Everything else keeps the funded-account ceiling.
        assert s.hard_max_risk_per_trade == 250
        assert s.hard_stop_loss_pct == 1
        assert s.hard_max_position_quantity == 100
        assert s.allow_short_entries is False
        assert s.hard_allow_position_addons is False
        assert s.llm_shadow_mode is True
        assert s.full_buying_power_usage_enabled is False

    def test_paper_quantity_and_risk_requests_are_honoured_separately(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each relaxation is paper-attestation-only and separately requested.

        With the confirmation flag set, an explicit quantity request raises
        the quantity cap and an explicit risk request raises the risk cap,
        each bounded by its own paper code ceiling. The un-requested notional
        cap stays at the funded default.
        """
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_MAX_POSITION_QUANTITY",
            "1000",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_MAX_RISK_PER_TRADE",
            "400",
        )

        s = Settings()

        assert s.hard_max_position_quantity == 1000
        assert s.hard_max_risk_per_trade == 400
        # The notional cap was not requested, so it keeps the funded ceiling.
        assert s.hard_max_position_notional == 5000
        # The other P0 invariants are untouched by sizing relaxations.
        assert s.hard_stop_loss_pct == 1
        assert s.hard_max_holding_minutes == 60
        assert s.hard_entry_cutoff_minutes_before_close == 45
        assert s.hard_flatten_minutes_before_close == 15
        assert s.allow_short_entries is False
        assert s.hard_allow_position_addons is False
        assert s.llm_shadow_mode is True
        assert s.full_buying_power_usage_enabled is False

    def test_paper_quantity_and_risk_requests_clamp_to_their_bounds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The paper requests are bounded, not unlimited switches.

        Without bounds the confirmation flag would become an
        unlimited-exposure switch, which is exactly the property the P0
        clamps exist to deny.
        """
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_MAX_POSITION_QUANTITY",
            "999999",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_MAX_RISK_PER_TRADE",
            "999999",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_MAX_POSITION_NOTIONAL",
            "999999",
        )

        s = Settings()

        assert s.hard_max_position_quantity == 5000
        assert s.hard_max_risk_per_trade == 2000
        assert s.hard_max_position_notional == 200000

    def test_paper_requests_below_the_funded_caps_lower_them(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The requests are ceilings, not floors: dialling down stays possible.

        The relaxation can only ever widen what is permitted; it must never
        force exposure upward, or an operator could not reduce risk while
        the exception is active.
        """
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_MAX_POSITION_QUANTITY",
            "40",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_MAX_RISK_PER_TRADE",
            "100",
        )

        s = Settings()

        assert s.hard_max_position_quantity == 40
        assert s.hard_max_risk_per_trade == 100

    def test_paper_quantity_and_risk_requests_are_ignored_without_confirmation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fail closed: the amounts alone must not relax anything.

        A funded deployment that inherits only the amounts — from a copied
        .env, say — keeps every funded ceiling. Each of the three caps must
        stay funded-regardless of its request.
        """
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_MAX_POSITION_QUANTITY",
            "1000",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_MAX_RISK_PER_TRADE",
            "400",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_MAX_POSITION_NOTIONAL",
            "85000",
        )

        s = Settings()

        assert s.hard_max_position_quantity == 100
        assert s.hard_max_risk_per_trade == 250
        assert s.hard_max_position_notional == 5000

    def test_paper_confirmation_with_zero_requests_keeps_funded_caps(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Zero is "no request", not a request for zero exposure.

        Confirming the paper account without stating any amount must not
        move any cap: the funded 100 / 5000 / 250 stay exactly as they are.
        """
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")

        s = Settings()

        assert s.hard_max_position_quantity == 100
        assert s.hard_max_risk_per_trade == 250
        assert s.hard_max_position_notional == 5000

    def test_paper_notional_request_is_a_ceiling_not_a_floor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Requesting less than the funded default must still lower the cap.

        The exception widens what is permitted; it must never force exposure
        up, or an operator could not dial risk back down while it is active.
        """
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")
        monkeypatch.setenv("AUTO_TRADE_PAPER_MAX_POSITION_NOTIONAL", "1000")

        s = Settings()

        assert s.hard_max_position_notional == 1000

    def test_notional_headroom_above_the_paper_bound_depends_on_the_stop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The paper notional bound is NOT proof that more notional needs more risk.

        `config.py` historically justified a 25,000 bound by saying that going
        past it "would require raising the risk budget as well". That holds
        only when the stop sits exactly at the 1% ceiling, where
        `250 / 1% == 25,000` makes the two caps bind at the same point. It is
        not a general statement: the risk cap binds notional at
        `max_risk / (stop_pct/100)`, which GROWS as the stop tightens, so at a
        0.5% stop the SAME 250 budget already permits 50,000 of notional and
        the notional ceiling — not the risk budget — is what binds.

        This test pins the arithmetic so the rationale cannot drift back into
        an unconditional claim. It deliberately does NOT assert that any cap
        should be raised: the clamps stay exactly where they are.
        """
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")
        monkeypatch.setenv("AUTO_TRADE_PAPER_MAX_POSITION_NOTIONAL", "25000")
        # Keep the sibling requests out of this arithmetic check.
        monkeypatch.delenv(
            "AUTO_TRADE_PAPER_MAX_POSITION_QUANTITY",
            raising=False,
        )
        monkeypatch.delenv(
            "AUTO_TRADE_PAPER_MAX_RISK_PER_TRADE",
            raising=False,
        )

        s = Settings()

        # The two caps coincide only at the 1% stop ceiling.
        assert s.hard_stop_loss_pct == 1
        assert s.hard_max_risk_per_trade == 250
        notional_allowed_at_ceiling_stop = s.hard_max_risk_per_trade / (
            s.hard_stop_loss_pct / 100
        )
        assert notional_allowed_at_ceiling_stop == 25000
        assert notional_allowed_at_ceiling_stop == s.hard_max_position_notional

        # Tighten the stop and the unchanged risk budget permits strictly MORE
        # notional, so the risk budget is no longer the binding constraint.
        for tighter_stop_pct, expected_allowed in ((0.5, 50000.0), (0.25, 100000.0)):
            allowed = s.hard_max_risk_per_trade / (tighter_stop_pct / 100)
            assert allowed == expected_allowed
            assert allowed > s.hard_max_position_notional, (
                "at a tighter stop the notional ceiling binds before the risk "
                "budget does, so 'more notional requires more risk' is false"
            )

    def test_paper_notional_cannot_exceed_the_authorised_bound(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The paper exception is itself bounded.

        Without an upper bound the flag would be an unlimited-exposure switch,
        which is the property the P0 clamps exist to deny.
        """
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_MAX_POSITION_NOTIONAL",
            "1000000",
        )

        s = Settings()

        assert s.hard_max_position_notional == 200000

    def test_paper_notional_is_ignored_without_the_confirmation_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fail closed: the amount alone must not relax anything.

        A funded deployment that inherits only the amount variable — from a
        copied .env, say — keeps the funded ceiling.
        """
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_MAX_POSITION_NOTIONAL",
            "25000",
        )

        s = Settings()

        assert s.hard_max_position_notional == 5000

    def test_paper_confirmation_alone_changes_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The flag is permission, not an amount.

        Confirming a paper account must not silently move exposure; the
        operator still has to state the number they want.
        """
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")

        s = Settings()

        assert s.hard_max_position_notional == 5000

    def test_paper_exception_lapses_when_the_account_identity_changes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The attestation must bind to the account it was made about.

        Today the exception is two env vars and nothing else, so a copied
        .env — or the same .env repointed at funded credentials — keeps the
        25,000 ceiling. The startup banner says a funded account must never
        carry this, but nothing enforces it.

        Bind it: when the operator records which account they attested for,
        a different account must fall back to the funded ceiling. The
        fingerprint reuses the same credential SHA-256 the runner already
        computes for order provenance (``_credential_identity_fingerprint``),
        so the binding is fail-closed and needs no broker call.
        """
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")
        monkeypatch.setenv("AUTO_TRADE_PAPER_MAX_POSITION_NOTIONAL", "25000")
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_ACCOUNT_FINGERPRINT",
            "a" * 64,
        )

        s = Settings()

        # The attested account still gets the exception.
        assert s.paper_exception_notional_for("a" * 64) == 25000
        # A different account does not, however the .env travelled.
        assert s.paper_exception_notional_for("b" * 64) == 5000
        # An unknown identity fails closed rather than assuming a match.
        assert s.paper_exception_notional_for("") == 5000

    def test_paper_exception_without_fingerprint_keeps_current_behaviour(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Binding is opt-in; omitting it must not silently tighten the cap.

        Existing deployments that attested without recording an identity keep
        working exactly as before, so this change cannot strand a running
        paper account.
        """
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")
        monkeypatch.setenv("AUTO_TRADE_PAPER_MAX_POSITION_NOTIONAL", "25000")

        s = Settings()

        assert s.hard_max_position_notional == 25000
        assert s.paper_exception_notional_for("anything") == 25000

    def test_paper_experiment_cannot_relax_any_other_p0_invariant(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The exception is scoped to notional and grants nothing else.

        Shorts, add-ons, live LLM ordering, full buying power, the stop
        ceiling and the session guards stay exactly as they are for a funded
        account even while the paper exception is active.
        """
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")
        monkeypatch.setenv(
            "AUTO_TRADE_PAPER_MAX_POSITION_NOTIONAL",
            "25000",
        )
        for name, value in {
            "AUTO_TRADE_ALLOW_SHORT_ENTRIES": "true",
            "AUTO_TRADE_HARD_ALLOW_POSITION_ADDONS": "true",
            "AUTO_TRADE_LLM_SHADOW_MODE": "false",
            "AUTO_TRADE_FULL_BUYING_POWER_USAGE_ENABLED": "true",
            "AUTO_TRADE_HARD_MAX_RISK_PER_TRADE": "100000",
            "AUTO_TRADE_HARD_STOP_LOSS_PCT": "10",
            "AUTO_TRADE_HARD_MAX_POSITION_QUANTITY": "10000",
            "AUTO_TRADE_HARD_MAX_HOLDING_MINUTES": "1440",
            "AUTO_TRADE_HARD_ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE": "1",
            "AUTO_TRADE_HARD_FLATTEN_MINUTES_BEFORE_CLOSE": "1",
        }.items():
            monkeypatch.setenv(name, value)

        s = Settings()

        assert s.hard_max_position_notional == 25000
        assert s.allow_short_entries is False
        assert s.hard_allow_position_addons is False
        assert s.llm_shadow_mode is True
        assert s.full_buying_power_usage_enabled is False
        assert s.hard_max_risk_per_trade == 250
        assert s.hard_stop_loss_pct == 1
        assert s.hard_max_position_quantity == 100
        assert s.hard_max_holding_minutes == 60
        assert s.hard_entry_cutoff_minutes_before_close == 45
        assert s.hard_flatten_minutes_before_close == 15

    def test_full_buying_power_usage_opt_in_is_ignored(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_FULL_BUYING_POWER_USAGE_ENABLED",
            "true",
        )

        settings = Settings()

        assert settings.full_buying_power_usage_enabled is False
        assert settings.hard_max_position_quantity == 100
        assert settings.hard_max_position_notional == 5000
        assert settings.hard_max_risk_per_trade == 250

    def test_entry_cost_gate_reads_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_ENTRY_ROUND_TRIP_SLIPPAGE_BPS",
            "6.5",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_MIN_ENTRY_EDGE_COST_RATIO",
            "2.5",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_MIN_ENTRY_REWARD_RISK_RATIO",
            "1.25",
        )

        configured = Settings()

        assert configured.entry_round_trip_slippage_bps == 6.5
        assert configured.min_entry_edge_cost_ratio == 2.5
        assert configured.min_entry_reward_risk_ratio == 1.25

    def test_opening_warmup_supports_delayed_fixed_range_entry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_TRADING_OPEN_WARMUP_MINUTES",
            "90",
        )

        assert Settings().trading_open_warmup_minutes == 90

        monkeypatch.setenv(
            "AUTO_TRADE_TRADING_OPEN_WARMUP_MINUTES",
            "181",
        )
        with pytest.raises(ValidationError):
            Settings()

    def test_universe_and_live_regime_controls_read_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_UNIVERSE_SELECTION_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_UNIVERSE_SELECTION_APPLY_TO_WATCHLIST",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_SHADOW_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_CHALLENGER_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_UNIVERSE_SELECTION_ENABLE_SHADOW",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_STRATEGY_V2_PORTFOLIO_SHADOW_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_LIVE_EXIT_CHALLENGER_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_AUTO_SCORE_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_INTERVAL_MINUTES",
            "20",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_SCORE_TTL_MINUTES",
            "120",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_BATCH_SIZE",
            "4",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_UNIVERSE_SELECTION_EXPLORATION_MAX_SYMBOLS",
            "6",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_UNIVERSE_SELECTION_EXPLORATION_"
            "TOP_SCORE_CHALLENGERS",
            "3",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_LIVE_REGIME_GATE_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_LIVE_REGIME_MAX_DATA_AGE_SECONDS",
            "300",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_LIVE_MAX_ENTRIES_PER_SYMBOL_PER_DAY",
            "1",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_LIVE_ENTRY_CROSSING_REQUIRED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_LIVE_ENTRY_CROSSING_MAX_AGE_SECONDS",
            "45",
        )

        configured = Settings()

        assert configured.universe_selection_enabled is True
        assert configured.universe_selection_apply_to_watchlist is True
        assert configured.opening_momentum_shadow_enabled is True
        assert configured.opening_momentum_challenger_enabled is True
        assert configured.universe_selection_enable_shadow is True
        assert configured.strategy_v2_portfolio_shadow_enabled is True
        assert configured.live_exit_challenger_enabled is True
        assert configured.watchlist_quant_auto_score_enabled is True
        assert configured.watchlist_quant_interval_minutes == 20
        assert configured.watchlist_quant_score_ttl_minutes == 120
        assert configured.watchlist_quant_batch_size == 4
        assert configured.universe_selection_exploration_max_symbols == 6
        assert (
            configured.universe_selection_exploration_top_score_challengers
            == 3
        )
        assert configured.live_regime_gate_enabled is True
        assert configured.live_regime_max_data_age_seconds == 300
        assert configured.live_max_entries_per_symbol_per_day == 1
        assert configured.live_entry_crossing_required is True
        assert configured.live_entry_crossing_max_age_seconds == 45

    @pytest.mark.parametrize(
        "value",
        ["4", "301"],
    )
    def test_rejects_invalid_live_entry_crossing_window(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_LIVE_ENTRY_CROSSING_MAX_AGE_SECONDS",
            value,
        )

        with pytest.raises(ValidationError):
            Settings()

    def test_universe_shadow_requires_watchlist_application(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_UNIVERSE_SELECTION_ENABLE_SHADOW",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_UNIVERSE_SELECTION_APPLY_TO_WATCHLIST",
            "false",
        )

        with pytest.raises(
            ValidationError,
            match="shadow requires watchlist application",
        ):
            Settings()

    def test_opening_momentum_shadow_requires_universe_selection(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_SHADOW_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_UNIVERSE_SELECTION_ENABLED",
            "false",
        )

        with pytest.raises(
            ValidationError,
            match="opening momentum shadow requires universe selection",
        ):
            Settings()

    def test_opening_momentum_challenger_requires_shadow(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_CHALLENGER_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_SHADOW_ENABLED",
            "false",
        )

        with pytest.raises(
            ValidationError,
            match="opening momentum challenger requires opening momentum "
            "shadow",
        ):
            Settings()

    def test_opening_momentum_execution_opt_in_is_observation_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_EXECUTION_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_CHALLENGER_ENABLED",
            "false",
        )

        configured = Settings()

        assert configured.opening_momentum_execution_enabled is False
        assert configured.full_buying_power_usage_enabled is False

    def test_opening_momentum_execution_requires_paper_confirmation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_UNIVERSE_SELECTION_ENABLED", "true")
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_SHADOW_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_CHALLENGER_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_EXECUTION_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_EXECUTION_PAPER_CONFIRMED",
            "false",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_FULL_BUYING_POWER_USAGE_ENABLED",
            "true",
        )

        configured = Settings()

        assert configured.opening_momentum_execution_enabled is False
        assert configured.full_buying_power_usage_enabled is False

    def test_opening_momentum_execution_requires_full_buying_power_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_UNIVERSE_SELECTION_ENABLED", "true")
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_SHADOW_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_CHALLENGER_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_EXECUTION_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_EXECUTION_PAPER_CONFIRMED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_FULL_BUYING_POWER_USAGE_ENABLED",
            "false",
        )

        configured = Settings()

        assert configured.opening_momentum_execution_enabled is False
        assert configured.full_buying_power_usage_enabled is False

    def test_opening_momentum_execution_rejects_explicit_paper_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_UNIVERSE_SELECTION_ENABLED", "true")
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_SHADOW_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_CHALLENGER_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_EXECUTION_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_EXECUTION_PAPER_CONFIRMED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_FULL_BUYING_POWER_USAGE_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_EXECUTION_MAX_ENTRY_DELAY_SECONDS",
            "45",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_OPENING_MOMENTUM_EXECUTION_MAX_PRICE_DEVIATION_BPS",
            "150",
        )

        configured = Settings()

        assert configured.opening_momentum_execution_enabled is False
        assert configured.opening_momentum_execution_paper_confirmed is True
        assert configured.full_buying_power_usage_enabled is False
        assert (
            configured.opening_momentum_execution_max_entry_delay_seconds
            == 45
        )
        assert (
            configured.opening_momentum_execution_max_price_deviation_bps
            == 150
        )

    def test_portfolio_shadow_requires_universe_shadow(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_STRATEGY_V2_PORTFOLIO_SHADOW_ENABLED",
            "true",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_UNIVERSE_SELECTION_ENABLE_SHADOW",
            "false",
        )

        with pytest.raises(
            ValidationError,
            match="portfolio shadow requires universe selection shadow",
        ):
            Settings()

    def test_watchlist_quant_ttl_cannot_be_shorter_than_refresh(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_INTERVAL_MINUTES",
            "60",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_WATCHLIST_QUANT_SCORE_TTL_MINUTES",
            "30",
        )

        with pytest.raises(
            ValidationError,
            match="score TTL must not be shorter",
        ):
            Settings()

    @pytest.mark.parametrize("value", ["-0.1", "1.1", "nan", "inf"])
    def test_rejects_invalid_llm_min_confidence(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_LLM_MIN_CONFIDENCE", value)

        with pytest.raises(ValidationError):
            Settings()

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("AUTO_TRADE_LLM_VOLATILITY_THRESHOLD_PCT", "nan"),
            ("AUTO_TRADE_LLM_VOLATILITY_THRESHOLD_PCT", "inf"),
            ("AUTO_TRADE_LLM_MAX_ORDER_PRICE_DEVIATION_PCT", "inf"),
            ("AUTO_TRADE_MIN_EXIT_PROFIT_PCT", "nan"),
            ("AUTO_TRADE_HARD_MAX_POSITION_NOTIONAL", "inf"),
            ("AUTO_TRADE_HARD_MAX_RISK_PER_TRADE", "nan"),
            ("AUTO_TRADE_HARD_STOP_LOSS_PCT", "inf"),
        ],
    )
    def test_rejects_non_finite_live_safety_settings(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        value: str,
    ) -> None:
        monkeypatch.setenv(name, value)

        with pytest.raises(ValidationError):
            Settings()

    def test_ensure_data_dir(self) -> None:
        s = Settings()
        data_dir = Path("data")
        s.ensure_data_dir()
        assert data_dir.exists()

    def test_reads_official_longport_credentials_from_parent_env_file(self, monkeypatch, tmp_path) -> None:
        for name in (
            "AUTO_TRADE_LONGBRIDGE_APP_KEY",
            "AUTO_TRADE_LONGBRIDGE_APP_SECRET",
            "AUTO_TRADE_LONGBRIDGE_ACCESS_TOKEN",
            "LONGBRIDGE_APP_KEY",
            "LONGBRIDGE_APP_SECRET",
            "LONGBRIDGE_ACCESS_TOKEN",
            "LONGPORT_APP_KEY",
            "LONGPORT_APP_SECRET",
            "LONGPORT_ACCESS_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)

        root = tmp_path / "project"
        backend = root / "backend"
        backend.mkdir(parents=True)
        (root / ".env").write_text(
            "LONGPORT_APP_KEY=official-key\n"
            "LONGPORT_APP_SECRET=official-secret\n"
            "LONGPORT_ACCESS_TOKEN=official-token\n",
            encoding="utf-8",
        )

        monkeypatch.chdir(backend)
        s = Settings()

        assert s.longbridge_app_key == "official-key"
        assert s.longbridge_app_secret == "official-secret"
        assert s.longbridge_access_token == "official-token"

    def test_reads_legacy_longbridge_credentials_from_parent_env_file(self, monkeypatch, tmp_path) -> None:
        for name in (
            "AUTO_TRADE_LONGBRIDGE_APP_KEY",
            "AUTO_TRADE_LONGBRIDGE_APP_SECRET",
            "AUTO_TRADE_LONGBRIDGE_ACCESS_TOKEN",
            "LONGBRIDGE_APP_KEY",
            "LONGBRIDGE_APP_SECRET",
            "LONGBRIDGE_ACCESS_TOKEN",
            "LONGPORT_APP_KEY",
            "LONGPORT_APP_SECRET",
            "LONGPORT_ACCESS_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)

        root = tmp_path / "project"
        backend = root / "backend"
        backend.mkdir(parents=True)
        (root / ".env").write_text(
            "LONGBRIDGE_APP_KEY=legacy-key\n"
            "LONGBRIDGE_APP_SECRET=legacy-secret\n"
            "LONGBRIDGE_ACCESS_TOKEN=legacy-token\n",
            encoding="utf-8",
        )

        monkeypatch.chdir(backend)
        s = Settings()

        assert s.longbridge_app_key == "legacy-key"
        assert s.longbridge_app_secret == "legacy-secret"
        assert s.longbridge_access_token == "legacy-token"

    def test_ignores_credential_master_key_from_parent_env_file(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("AUTO_TRADE_ENV", raising=False)
        monkeypatch.delenv("CREDENTIAL_MASTER_KEY", raising=False)

        root = tmp_path / "project"
        backend = root / "backend"
        backend.mkdir(parents=True)
        (root / ".env").write_text(
            "CREDENTIAL_MASTER_KEY=local-encryption-key\n",
            encoding="utf-8",
        )

        monkeypatch.chdir(backend)
        s = Settings()

        assert s.env == "dev"

    def test_reads_deepseek_api_key_from_unprefixed_env_var(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("AUTO_TRADE_DEEPSEEK_API_KEY", raising=False)

        root = tmp_path / "project"
        backend = root / "backend"
        backend.mkdir(parents=True)
        (root / ".env").write_text(
            "DEEPSEEK_API_KEY=sk-test-key\n",
            encoding="utf-8",
        )

        monkeypatch.chdir(backend)
        s = Settings()

        assert s.deepseek_api_key == "sk-test-key"

    def test_reads_minimax_api_key_from_unprefixed_env_var(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("AUTO_TRADE_MINIMAX_API_KEY", raising=False)

        root = tmp_path / "project"
        backend = root / "backend"
        backend.mkdir(parents=True)
        (root / ".env").write_text(
            "MINIMAX_API_KEY=mm-test-key\n",
            encoding="utf-8",
        )

        monkeypatch.chdir(backend)
        s = Settings()

        assert s.minimax_api_key == "mm-test-key"

    def test_deepseek_model_defaults_to_v4_pro_thinking_max_256k(self) -> None:
        s = Settings()
        assert s.deepseek_model == "deepseek-v4-pro"
        assert s.deepseek_reasoning_effort == "max"
        assert s.deepseek_thinking_type == "enabled"
        assert s.deepseek_max_tokens == 262144

    def test_llm_provider_defaults_to_deepseek_and_minimax_defaults_are_available(self) -> None:
        s = Settings()

        assert s.llm_provider == "deepseek"
        assert s.minimax_base_url == "https://api.minimaxi.com/v1"
        assert s.minimax_api_url == ""
        assert s.minimax_model == "MiniMax-M3"
        assert s.minimax_thinking_type == "adaptive"
        assert s.minimax_max_completion_tokens == 8192

    def test_extended_hours_protective_exits_default_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.delenv(
            "AUTO_TRADE_EXTENDED_HOURS_PROTECTIVE_EXITS_ENABLED",
            raising=False,
        )
        # Read the true code default, not the developer-local .env.
        monkeypatch.chdir(tmp_path)

        assert Settings().extended_hours_protective_exits_enabled is False

    @pytest.mark.parametrize("value", ["true", "1"])
    def test_extended_hours_protective_exits_opt_in_reads_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv(
            "AUTO_TRADE_EXTENDED_HOURS_PROTECTIVE_EXITS_ENABLED",
            value,
        )

        assert Settings().extended_hours_protective_exits_enabled is True


class TestFundedMarginExceptionSettings:
    """AUTO_TRADE_FUNDED_MARGIN_* — account-bound full-margin exception.

    Owner decision (2026-10-05/06): relax the funded caps ONLY through this
    default-OFF exception; the hard_max_* clamps (100/5000/250) themselves
    are NEVER mutated. "Configured" = enabled AND not paper AND a valid
    64-hex fingerprint AND all three requests > 0.
    """

    _VARS = (
        "AUTO_TRADE_FUNDED_MARGIN_ENABLED",
        "AUTO_TRADE_FUNDED_MARGIN_ACCOUNT_FINGERPRINT",
        "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_QUANTITY",
        "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_NOTIONAL",
        "AUTO_TRADE_FUNDED_MARGIN_MAX_RISK_PER_TRADE",
    )
    _FP = "3" * 64

    def _clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in self._VARS:
            monkeypatch.delenv(name, raising=False)

    def _arm(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        enabled: str = "true",
        fingerprint: str | None = None,
        qty: str = "1000",
        notional: str = "25000",
        risk: str = "250",
    ) -> None:
        self._clear(monkeypatch)
        monkeypatch.setenv("AUTO_TRADE_FUNDED_MARGIN_ENABLED", enabled)
        monkeypatch.setenv(
            "AUTO_TRADE_FUNDED_MARGIN_ACCOUNT_FINGERPRINT",
            self._FP if fingerprint is None else fingerprint,
        )
        monkeypatch.setenv(
            "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_QUANTITY", qty,
        )
        monkeypatch.setenv(
            "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_NOTIONAL", notional,
        )
        monkeypatch.setenv(
            "AUTO_TRADE_FUNDED_MARGIN_MAX_RISK_PER_TRADE", risk,
        )

    def test_defaults_are_off_and_inert(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self._clear(monkeypatch)
        monkeypatch.chdir(tmp_path)

        s = Settings()

        assert s.funded_margin_enabled is False
        assert s.funded_margin_account_fingerprint == ""
        assert s.funded_margin_max_position_quantity == 0
        assert s.funded_margin_max_position_notional == 0.0
        assert s.funded_margin_max_risk_per_trade == 0.0
        # Flag off: hard caps/floors/extended flag byte-identical to today.
        assert s.hard_max_position_quantity == 100
        assert s.hard_max_position_notional == 5000
        assert s.hard_max_risk_per_trade == 250
        assert s.hard_entry_cutoff_minutes_before_close == 45
        assert s.hard_flatten_minutes_before_close == 15
        assert s.extended_hours_trading_enabled is False

    def test_enabled_without_configuration_changes_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Flag on but no fingerprint / zero requests => identical to off.
        self._arm(monkeypatch, fingerprint="", qty="0", notional="0", risk="0")

        s = Settings()

        assert s.funded_margin_enabled is True
        assert s.hard_max_position_quantity == 100
        assert s.hard_max_position_notional == 5000
        assert s.hard_max_risk_per_trade == 250
        assert s.hard_entry_cutoff_minutes_before_close == 45
        assert s.hard_flatten_minutes_before_close == 15
        assert s.extended_hours_trading_enabled is False

    def test_configured_raises_cutoff_and_flatten_floors(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._arm(monkeypatch)

        s = Settings()

        # The exception never mutates the sizing clamps...
        assert s.hard_max_position_quantity == 100
        assert s.hard_max_position_notional == 5000
        assert s.hard_max_risk_per_trade == 250
        # ...but the account-wide session floors rise (safer direction).
        assert s.hard_entry_cutoff_minutes_before_close == 90
        assert s.hard_flatten_minutes_before_close == 30

    def test_configured_forces_extended_hours_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._arm(monkeypatch)
        monkeypatch.setenv(
            "AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED", "true",
        )

        s = Settings()

        assert s.extended_hours_trading_enabled is False
        assert s.extended_hours_trading_effective() is False
        # The protective-exits opt-in is NOT touched.
        assert s.extended_hours_protective_exits_enabled is False

    def test_configured_keeps_flatten_not_above_cutoff_when_operator_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._arm(monkeypatch)
        monkeypatch.setenv(
            "AUTO_TRADE_HARD_ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE", "120",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_HARD_FLATTEN_MINUTES_BEFORE_CLOSE", "45",
        )

        s = Settings()

        # Operator-raised values survive; flatten stays <= cutoff.
        assert s.hard_entry_cutoff_minutes_before_close == 120
        assert s.hard_flatten_minutes_before_close == 45

    def test_paper_attestation_disarms_the_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._arm(monkeypatch)
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")

        s = Settings()

        # Paper + paper requests keep their own path; the funded-margin
        # exception must not fire on a paper-attested account.
        assert s.funded_margin_enabled is True
        assert s.hard_entry_cutoff_minutes_before_close == 45
        assert s.hard_flatten_minutes_before_close == 15
        assert s.extended_hours_trading_enabled is False

    @pytest.mark.parametrize(
        "fingerprint",
        ["", "XYZ", "abc", "A" * 64, "3" * 63, "3" * 65, "g" * 64],
    )
    def test_invalid_fingerprint_is_treated_as_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fingerprint: str,
    ) -> None:
        self._arm(monkeypatch, fingerprint=fingerprint)

        s = Settings()

        assert s.funded_margin_account_fingerprint == ""
        # Not configured: identical to flag-off.
        assert s.hard_entry_cutoff_minutes_before_close == 45
        assert s.hard_flatten_minutes_before_close == 15

    def test_valid_fingerprint_survives_normalization(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._arm(monkeypatch)

        s = Settings()

        assert s.funded_margin_account_fingerprint == self._FP

    @pytest.mark.parametrize(
        ("qty", "notional", "risk"),
        [
            ("0", "25000", "250"),
            ("1000", "0", "250"),
            ("1000", "25000", "0"),
            ("-5", "25000", "250"),
            ("1000", "-5", "250"),
            ("1000", "25000", "-5"),
            ("nan", "25000", "250"),
            ("1000", "nan", "250"),
            ("1000", "25000", "nan"),
            ("inf", "25000", "250"),
        ],
    )
    def test_zero_negative_or_non_finite_requests_behave_as_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
        qty: str,
        notional: str,
        risk: str,
    ) -> None:
        self._arm(monkeypatch, qty=qty, notional=notional, risk=risk)

        s = Settings()

        # Not configured: identical to flag-off, regardless of the value.
        assert s.hard_entry_cutoff_minutes_before_close == 45
        assert s.hard_flatten_minutes_before_close == 15

    def test_requests_above_the_code_bounds_clamp_to_the_bounds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._arm(monkeypatch, qty="99999", notional="999999", risk="99999")

        from app.config import (
            FUNDED_MARGIN_MAX_POSITION_NOTIONAL_BOUND,
            FUNDED_MARGIN_MAX_POSITION_QUANTITY_BOUND,
            FUNDED_MARGIN_MAX_RISK_PER_TRADE_BOUND,
        )

        s = Settings()

        assert s.funded_margin_max_position_quantity == (
            FUNDED_MARGIN_MAX_POSITION_QUANTITY_BOUND
        )
        assert s.funded_margin_max_position_notional == (
            FUNDED_MARGIN_MAX_POSITION_NOTIONAL_BOUND
        )
        assert s.funded_margin_max_risk_per_trade == (
            FUNDED_MARGIN_MAX_RISK_PER_TRADE_BOUND
        )
        # The bounds are the authorization ceilings from the design.
        assert FUNDED_MARGIN_MAX_POSITION_QUANTITY_BOUND == 1000
        assert FUNDED_MARGIN_MAX_POSITION_NOTIONAL_BOUND == 25000.0
        assert FUNDED_MARGIN_MAX_RISK_PER_TRADE_BOUND == 250.0

    def test_requests_below_the_bounds_are_kept(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._arm(monkeypatch, qty="400", notional="12000", risk="180")

        s = Settings()

        assert s.funded_margin_max_position_quantity == 400
        assert s.funded_margin_max_position_notional == 12000
        assert s.funded_margin_max_risk_per_trade == 180

    def test_not_configured_states_are_distinguishable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.config import FundedMarginConfiguration

        # OFF
        self._clear(monkeypatch)
        s = Settings()
        c = s.funded_margin_configuration()
        assert isinstance(c, FundedMarginConfiguration)
        assert c.configured is False
        assert c.not_configured_reason == "DISABLED"
        assert c.effective_quantity is None

        # PAPER
        self._arm(monkeypatch)
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")
        s = Settings()
        c = s.funded_margin_configuration()
        assert c.configured is False
        assert c.not_configured_reason == "PAPER"

        # INVALID_FINGERPRINT
        self._arm(monkeypatch, fingerprint="nothex")
        monkeypatch.delenv(
            "AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", raising=False,
        )
        s = Settings()
        c = s.funded_margin_configuration()
        assert c.configured is False
        assert c.not_configured_reason == "INVALID_FINGERPRINT"

        # ZERO_REQUEST
        self._arm(monkeypatch, qty="0", notional="25000", risk="250")
        monkeypatch.delenv(
            "AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", raising=False,
        )
        s = Settings()
        c = s.funded_margin_configuration()
        assert c.configured is False
        assert c.not_configured_reason == "ZERO_REQUEST"

        # CONFIGURED
        self._arm(monkeypatch)
        monkeypatch.delenv(
            "AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", raising=False,
        )
        s = Settings()
        c = s.funded_margin_configuration()
        assert c.configured is True
        assert c.not_configured_reason is None
        assert c.effective_quantity == 1000
        assert c.effective_notional == 25000.0
        assert c.effective_risk == 250.0
