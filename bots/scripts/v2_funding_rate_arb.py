import asyncio
import json
import math
import os
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Set, Tuple

import pandas as pd
from pydantic import Field, field_validator

from hummingbot.client.ui.interface_utils import format_df_for_printout
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.clock import Clock
from hummingbot.core.data_type.common import MarketDict, OrderType, PositionAction, PositionMode, PriceType, TradeType
from hummingbot.core.event.events import FundingPaymentCompletedEvent
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction


class FundingRateArbitrageConfig(StrategyV2ConfigBase):
    script_file_name: str = os.path.basename(__file__)
    dry_run: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": lambda mi: "Run in dry-run simulation mode without real orders? (True/False): ",
            "prompt_on_new": True,
        }
    )
    leverage: int = Field(
        default=20, gt=0,
        json_schema_extra={"prompt": lambda mi: "Enter the leverage (e.g. 20): ", "prompt_on_new": True},
    )
    min_funding_rate_profitability: Decimal = Field(
        default=0.001,
        json_schema_extra={
            "prompt": lambda mi: "Enter the min funding rate profitability to enter in a position (e.g. 0.001): ",
            "prompt_on_new": True}
    )
    connectors: Set[str] = Field(
        default="hyperliquid_perpetual,binance_perpetual",
        json_schema_extra={
            "prompt": lambda mi: "Enter the connectors separated by commas (e.g. hyperliquid_perpetual,binance_perpetual): ",
            "prompt_on_new": True}
    )
    tokens: Set[str] = Field(
        default="WIF,FET",
        json_schema_extra={"prompt": lambda mi: "Enter the tokens separated by commas (e.g. WIF,FET): ", "prompt_on_new": True},
    )
    position_size_quote: Decimal = Field(
        default=100,
        json_schema_extra={
            "prompt": lambda mi: "Enter the position size in quote asset (e.g. order amount 100 will open 100 long on hyperliquid and 100 short on binance): ",
            "prompt_on_new": True
        }
    )
    profitability_to_take_profit: Decimal = Field(
        default=Decimal("0.02"),
        json_schema_extra={
            "prompt": lambda mi: "Enter the profitability to take profit (including PNL of positions and fundings received): ",
            "prompt_on_new": True}
    )
    funding_rate_diff_stop_loss: Decimal = Field(
        default=-0.001,
        json_schema_extra={
            "prompt": lambda mi: "Enter the funding rate difference to stop the position (e.g. -0.001): ",
            "prompt_on_new": True}
    )
    trade_profitability_condition_to_enter: bool = Field(
        default=False,
        json_schema_extra={
            "prompt": lambda mi: "Do you want to check the trade profitability condition to enter? (True/False): ",
            "prompt_on_new": True}
    )
    max_payback_hours: Decimal = Field(
        default=Decimal("12.0"),
        json_schema_extra={
            "prompt": lambda mi: "Max payback hours to accept entry friction vs funding yield (e.g. 12.0): ",
            "prompt_on_new": True,
        }
    )
    dynamic_position_sizing: bool = Field(
        default=True,
        json_schema_extra={
            "prompt": lambda mi: "Enable dynamic position sizing based on order book depth? (True/False): ",
            "prompt_on_new": True,
        }
    )
    max_orderbook_depth_ratio: Decimal = Field(
        default=Decimal("0.15"),
        json_schema_extra={
            "prompt": lambda mi: "Max ratio of top order book depth to consume (e.g. 0.15 for 15%): ",
            "prompt_on_new": True,
        }
    )
    min_position_size_quote: Decimal = Field(
        default=Decimal("100"),
        json_schema_extra={
            "prompt": lambda mi: "Minimum position size in quote asset to open (e.g. 100): ",
            "prompt_on_new": True,
        }
    )
    liquidation_buffer_pct: Decimal = Field(
        default=Decimal("0.08"),
        json_schema_extra={
            "prompt": lambda mi: "Safety distance threshold to liquidation price for defense exit (e.g. 0.08 for 8%): ",
            "prompt_on_new": True,
        }
    )
    min_leverage_capital_threshold: Decimal = Field(
        default=Decimal("500.0"),
        json_schema_extra={
            "prompt": lambda mi: "Min capital threshold for applying leverage (below this size defaults to 1x safe mode, e.g. 500): ",
            "prompt_on_new": False,
        }
    )
    max_allowed_price_divergence: Decimal = Field(
        default=Decimal("0.03"),
        json_schema_extra={
            "prompt": lambda mi: "Max allowed cross-exchange price divergence to prevent ticker collisions (e.g. 0.03 for 3%): ",
            "prompt_on_new": False,
        }
    )
    token_quarantine_hours: Decimal = Field(
        default=Decimal("24.0"),
        json_schema_extra={
            "prompt": lambda mi: "Hours to quarantine a toxic coin after heavy loss or consecutive stop losses (e.g. 24.0): ",
            "prompt_on_new": False,
        }
    )
    single_loss_quarantine_threshold: Decimal = Field(
        default=Decimal("15.0"),
        json_schema_extra={
            "prompt": lambda mi: "Single trade loss USD threshold to trigger quarantine (e.g. 15.0): ",
            "prompt_on_new": False,
        }
    )
    rate_inversion_confirmation_ticks: int = Field(
        default=3,
        json_schema_extra={
            "prompt": lambda mi: "Consecutive ticks of rate inversion required to confirm stop loss (e.g. 3): ",
            "prompt_on_new": False,
        }
    )
    reentry_cooldown_seconds: int = Field(
        default=1800,
        json_schema_extra={
            "prompt": lambda mi: "Cooldown seconds before re-entering a recently closed token (e.g. 1800 for 30m): ",
            "prompt_on_new": False,
        }
    )
    max_entry_spread_friction_pct: Decimal = Field(
        default=Decimal("0.005"),
        json_schema_extra={
            "prompt": lambda mi: "Max allowable entry friction (spread + fee discount) to open position (e.g. 0.005 for 0.5%): ",
            "prompt_on_new": False,
        }
    )
    entry_persistence_ticks: int = Field(
        default=3,
        json_schema_extra={
            "prompt": lambda mi: "Consecutive ticks of positive spread/payback required to confirm entry (e.g. 3): ",
            "prompt_on_new": False,
        }
    )
    max_basis_loss_pct: Decimal = Field(
        default=Decimal("0.005"),
        json_schema_extra={
            "prompt": lambda mi: "Max allowed trading PnL basis loss ratio before emergency cut (e.g. 0.005 for 0.5%): ",
            "prompt_on_new": False,
        }
    )
    settlement_blackout_minutes: int = Field(
        default=30,
        json_schema_extra={
            "prompt": lambda mi: "Minutes before 8h funding settlement to freeze new entries on volatile tokens (e.g. 30): ",
            "prompt_on_new": False,
        }
    )
    settlement_blackout_tier3_only: bool = Field(
        default=False,
        json_schema_extra={
            "prompt": lambda mi: "Only apply settlement blackout to Tier 3 tokens? (True/False): ",
            "prompt_on_new": False,
        }
    )
    tier_1_tokens: Set[str] = Field(
        default="BTC,ETH,SOL",
        json_schema_extra={"prompt": lambda mi: "Tier 1 mega-cap tokens (e.g. BTC,ETH,SOL): ", "prompt_on_new": False}
    )
    tier_1_max_quote: Decimal = Field(
        default=Decimal("10000.0"),
        json_schema_extra={"prompt": lambda mi: "Tier 1 maximum position size quote (e.g. 10000.0): ", "prompt_on_new": False}
    )
    tier_2_tokens: Set[str] = Field(
        default="AVAX,SUI,APT,NEAR,LINK,ARB,OP,INJ,DOGE,XRP,ADA,MATIC,DOT,UNI,LTC,BCH,BNB,SAND",
        json_schema_extra={"prompt": lambda mi: "Tier 2 large/mid-cap tokens: ", "prompt_on_new": False}
    )
    tier_2_max_quote: Decimal = Field(
        default=Decimal("2000.0"),
        json_schema_extra={"prompt": lambda mi: "Tier 2 maximum position size quote (e.g. 2000.0): ", "prompt_on_new": False}
    )
    tier_3_max_quote: Decimal = Field(
        default=Decimal("500.0"),
        json_schema_extra={"prompt": lambda mi: "Tier 3 micro-cap / meme tokens maximum position size quote (e.g. 500.0): ", "prompt_on_new": False}
    )

    # --- v1.3.0 Autonomous Universe Auto-Rotation Fields ---
    auto_rotation_enabled: bool = Field(
        default=True,
        json_schema_extra={"prompt": lambda mi: "Enable autonomous token universe rotation? (True/False): ", "prompt_on_new": False}
    )
    auto_rotation_interval_minutes: int = Field(
        default=30,
        json_schema_extra={"prompt": lambda mi: "Interval in minutes between full market rotation checks (e.g. 30): ", "prompt_on_new": False}
    )
    auto_rotation_min_apr: float = Field(
        default=0.35,
        json_schema_extra={"prompt": lambda mi: "Minimum APR required for candidate tokens to enter universe (e.g. 0.35 for 35%): ", "prompt_on_new": False}
    )
    auto_rotation_min_apr_delta: float = Field(
        default=0.40,
        json_schema_extra={"prompt": lambda mi: "Minimum APR advantage over idle token to trigger rotation (e.g. 0.40 for 40%): ", "prompt_on_new": False}
    )
    auto_rotation_max_active_tokens: int = Field(
        default=6,
        json_schema_extra={"prompt": lambda mi: "Maximum number of tokens in active watchlist (e.g. 6): ", "prompt_on_new": False}
    )
    auto_rotation_min_volume_24h_usd: float = Field(
        default=2000000.0,
        json_schema_extra={"prompt": lambda mi: "Minimum 24h volume USD to qualify for auto-rotation (e.g. 2000000.0): ", "prompt_on_new": False}
    )

    # --- v1.3.0 Tier-Aware Dynamic Liquidation Buffers ---
    tier_1_liquidation_buffer: Decimal = Field(
        default=Decimal("0.04"),
        json_schema_extra={"prompt": lambda mi: "Tier 1 liquidation safety buffer (e.g. 0.04 for 4%): ", "prompt_on_new": False}
    )
    tier_2_liquidation_buffer: Decimal = Field(
        default=Decimal("0.08"),
        json_schema_extra={"prompt": lambda mi: "Tier 2 liquidation safety buffer (e.g. 0.08 for 8%): ", "prompt_on_new": False}
    )
    tier_3_liquidation_buffer: Decimal = Field(
        default=Decimal("0.12"),
        json_schema_extra={"prompt": lambda mi: "Tier 3 liquidation safety buffer (e.g. 0.12 for 12%): ", "prompt_on_new": False}
    )

    # --- v1.3.0 Scaling-Out Take Profit Fields (Option B) ---
    scaling_tp_enabled: bool = Field(
        default=True,
        json_schema_extra={"prompt": lambda mi: "Enable scaling-out tranche take profit? (True/False): ", "prompt_on_new": False}
    )
    partial_tp_ratio: Decimal = Field(
        default=Decimal("0.50"),
        json_schema_extra={"prompt": lambda mi: "Ratio of position to close on tranche 1 (e.g. 0.50 for 50%): ", "prompt_on_new": False}
    )
    partial_tp_threshold_pct: Decimal = Field(
        default=Decimal("0.010"),
        json_schema_extra={"prompt": lambda mi: "Net profit threshold on position to trigger tranche 1 (e.g. 0.010 for 1.0%): ", "prompt_on_new": False}
    )

    # --- v1.4.0 Institutional Risk & Profit Overhaul Fields ---
    post_tp_cooldown_hours: float = Field(
        default=4.0,
        json_schema_extra={"prompt": lambda mi: "Cooldown hours before re-entering a token after take profit (e.g. 4.0): ", "prompt_on_new": False}
    )
    breakeven_trailing_activation_pct: Decimal = Field(
        default=Decimal("0.006"),
        json_schema_extra={"prompt": lambda mi: "Net profit pct to activate breakeven stop loss (e.g. 0.006 for 0.6%): ", "prompt_on_new": False}
    )
    breakeven_trailing_stop_pct: Decimal = Field(
        default=Decimal("0.0005"),
        json_schema_extra={"prompt": lambda mi: "Locked breakeven stop loss level after activation (e.g. 0.0005 for +0.05%): ", "prompt_on_new": False}
    )
    cumulative_loss_hard_blacklist_usd: float = Field(
        default=30.0,
        json_schema_extra={"prompt": lambda mi: "Cumulative historical net loss threshold to permanently blacklist toxic token (e.g. 30.0 for $30): ", "prompt_on_new": False}
    )

    @field_validator("connectors", "tokens", "tier_1_tokens", "tier_2_tokens", mode="before")
    @classmethod
    def validate_sets(cls, v):
        if isinstance(v, str):
            return set(v.split(","))
        if isinstance(v, (list, tuple)):
            return set(v)
        return v

    def update_markets(self, markets: MarketDict) -> MarketDict:
        for connector in self.connectors:
            trading_pairs = {FundingRateArbitrage.get_trading_pair_for_connector(token, connector) for token in self.tokens}
            markets[connector] = markets.get(connector, set()) | trading_pairs
        return markets


class FundingRateArbitrage(StrategyV2Base):
    quote_markets_map = {
        "hyperliquid_perpetual": "USD",
        "binance_perpetual": "USDT"
    }
    funding_payment_interval_map = {
        "binance_perpetual": 60 * 60 * 8,
        "hyperliquid_perpetual": 60 * 60 * 1
    }
    funding_profitability_interval = 60 * 60 * 24

    canonical_token_registry = {
        "PEPE": {
            "binance_perpetual": {"symbol": "1000PEPE", "multiplier": 1000.0},
            "hyperliquid_perpetual": {"symbol": "kPEPE", "multiplier": 1000.0},
            "bybit_perpetual": {"symbol": "1000PEPE", "multiplier": 1000.0},
        },
        "BONK": {
            "binance_perpetual": {"symbol": "1000BONK", "multiplier": 1000.0},
            "hyperliquid_perpetual": {"symbol": "kBONK", "multiplier": 1000.0},
            "bybit_perpetual": {"symbol": "1000BONK", "multiplier": 1000.0},
        },
        "SHIB": {
            "binance_perpetual": {"symbol": "1000SHIB", "multiplier": 1000.0},
            "hyperliquid_perpetual": {"symbol": "kSHIB", "multiplier": 1000.0},
            "bybit_perpetual": {"symbol": "1000SHIB", "multiplier": 1000.0},
        },
        "FLOKI": {
            "binance_perpetual": {"symbol": "1000FLOKI", "multiplier": 1000.0},
            "hyperliquid_perpetual": {"symbol": "kFLOKI", "multiplier": 1000.0},
            "bybit_perpetual": {"symbol": "1000FLOKI", "multiplier": 1000.0},
        },
        "LUNC": {
            "binance_perpetual": {"symbol": "1000LUNC", "multiplier": 1000.0},
            "hyperliquid_perpetual": {"symbol": "kLUNC", "multiplier": 1000.0},
            "bybit_perpetual": {"symbol": "1000LUNC", "multiplier": 1000.0},
        },
        "NEIRO": {
            "binance_perpetual": {"symbol": "NEIRO", "multiplier": 1.0},
            "hyperliquid_perpetual": {"symbol": "kNEIRO", "multiplier": 1000.0},
            "bybit_perpetual": {"symbol": "1000NEIRO", "multiplier": 1000.0},
        },
        "XEC": {
            "binance_perpetual": {"symbol": "1000XEC", "multiplier": 1000.0},
            "hyperliquid_perpetual": {"symbol": "kXEC", "multiplier": 1000.0},
        },
        "RATS": {
            "binance_perpetual": {"symbol": "1000RATS", "multiplier": 1000.0},
            "hyperliquid_perpetual": {"symbol": "kRATS", "multiplier": 1000.0},
        },
        "SATS": {
            "binance_perpetual": {"symbol": "1000SATS", "multiplier": 1000.0},
            "hyperliquid_perpetual": {"symbol": "kSATS", "multiplier": 1000.0},
        },
        "CAT": {
            "binance_perpetual": {"symbol": "1000CAT", "multiplier": 1000.0},
            "hyperliquid_perpetual": {"symbol": "kCAT", "multiplier": 1000.0},
        },
        "CHEEMS": {
            "binance_perpetual": {"symbol": "1000CHEEMS", "multiplier": 1000.0},
            "hyperliquid_perpetual": {"symbol": "kCHEEMS", "multiplier": 1000.0},
        },
        "WHY": {
            "binance_perpetual": {"symbol": "1000WHY", "multiplier": 1000.0},
            "hyperliquid_perpetual": {"symbol": "kWHY", "multiplier": 1000.0},
        },
        "MOG": {
            "binance_perpetual": {"symbol": "1000000MOG", "multiplier": 1000000.0},
            "hyperliquid_perpetual": {"symbol": "MOG", "multiplier": 1.0},
        },
    }

    @classmethod
    def get_canonical_token(cls, symbol: str, connector: str = "") -> str:
        if not symbol:
            return ""
        raw_clean = symbol.strip().split("-")[0]
        clean_sym = raw_clean.upper()
        c_lower = (connector or "").lower()

        # Check registry
        for canon, conns in cls.canonical_token_registry.items():
            for conn_key, data in conns.items():
                if raw_clean == data["symbol"] or clean_sym == data["symbol"].upper():
                    if not c_lower or conn_key.startswith(c_lower) or c_lower.startswith(conn_key.split("_")[0]):
                        return canon

        if clean_sym in cls.canonical_token_registry:
            return clean_sym

        # Heuristic: HL lowercase k-prefix
        if raw_clean.startswith("k") and len(raw_clean) >= 4 and raw_clean[1:].isupper():
            return raw_clean[1:]
        # dYdX '1k' prefix (e.g. 1kPEPE)
        if clean_sym.startswith("1K") and len(clean_sym) > 3:
            return clean_sym[2:]
        # Binance / Bybit / Gate '1000000' prefix
        if clean_sym.startswith("1000000") and len(clean_sym) > 7:
            return clean_sym[7:]
        # Bybit '10000' prefix (e.g. 10000LADYS)
        if clean_sym.startswith("10000") and len(clean_sym) > 5:
            return clean_sym[5:]
        # Binance / Bybit / Bitget / KuCoin '1000' prefix
        if clean_sym.startswith("1000") and len(clean_sym) > 4:
            return clean_sym[4:]
        return clean_sym

    @classmethod
    def get_exchange_symbol(cls, token: str, connector: str) -> str:
        canon = cls.get_canonical_token(token, connector)
        c_lower = (connector or "").lower()
        if canon in cls.canonical_token_registry:
            conns = cls.canonical_token_registry[canon]
            for conn_key, val in conns.items():
                if conn_key in c_lower or c_lower.startswith(conn_key.split("_")[0]):
                    return val["symbol"]

        if "hyperliquid" in c_lower:
            if canon in ["PEPE", "SHIB", "BONK", "FLOKI", "LUNC", "CAT", "CHEEMS"]:
                return f"k{canon}"
        elif "dydx" in c_lower:
            if canon in ["PEPE", "SHIB", "BONK"]:
                return f"1k{canon}"
        elif any(k in c_lower for k in ["binance", "bybit", "bitget", "kucoin"]):
            if canon in ["PEPE", "SHIB", "BONK", "FLOKI", "LUNC", "CAT", "CHEEMS", "WHY", "RATS", "SATS", "XEC"]:
                return f"1000{canon}"
            if canon in ["MOG", "BOB"]:
                return f"1000000{canon}"
        return canon

    @classmethod
    def get_token_multiplier(cls, token: str, connector: str) -> float:
        canon = cls.get_canonical_token(token, connector)
        c_lower = (connector or "").lower()
        if canon in cls.canonical_token_registry:
            conns = cls.canonical_token_registry[canon]
            for conn_key, val in conns.items():
                if conn_key in c_lower or c_lower.startswith(conn_key.split("_")[0]):
                    return float(val.get("multiplier", 1.0))

        raw_clean = token.strip().split("-")[0]
        clean_sym = raw_clean.upper()
        if clean_sym.startswith("1000000"):
            return 1000000.0
        if clean_sym.startswith("10000"):
            return 10000.0
        if clean_sym.startswith("1000") or clean_sym.startswith("1K"):
            return 1000.0
        if raw_clean.startswith("k") and len(raw_clean) >= 4 and raw_clean[1:].isupper():
            return 1000.0
        return 1.0

    @classmethod
    def get_trading_pair_for_connector(cls, token, connector):
        symbol = cls.get_exchange_symbol(token, connector)
        quote = cls.quote_markets_map.get(connector, 'USDT')
        return f"{symbol}-{quote}"

    @classmethod
    def init_markets(cls, config):
        super().init_markets(config)
        if getattr(config, "dry_run", False):
            cls._enable_dry_run_connectors()

    @classmethod
    def _enable_dry_run_connectors(cls):
        """
        In dry-run mode, patch ConnectorManager to allow perpetual connectors to initialize
        with trading_required=False and empty API keys, enabling live market data streaming
        without requiring private credentials.
        """
        try:
            from hummingbot.client.settings import AllConnectorSettings
            from hummingbot.core.connector_manager import ConnectorManager, get_connector_class
            from hummingbot.core.rate_oracle.rate_oracle import RateOracle

            if getattr(ConnectorManager, "_dry_run_patched", False):
                return

            orig_create = ConnectorManager.create_connector

            def patched_create_connector(self, connector_name: str, trading_pairs=None, trading_required=True, api_keys=None):
                if not api_keys:
                    from hummingbot.client.config.security import Security
                    keys = Security.api_keys(connector_name)
                else:
                    keys = api_keys

                if not keys:
                    conn_setting = AllConnectorSettings.get_connector_settings().get(connector_name)
                    if conn_setting:
                        init_params = conn_setting.conn_init_parameters(
                            trading_pairs=trading_pairs,
                            trading_required=False,
                            api_keys={},
                            balance_asset_limit=self.client_config_map.hb_config.balance_asset_limit,
                            rate_limits_share_pct=self.client_config_map.hb_config.rate_limits_share_pct,
                            gateway_config=self.client_config_map.hb_config.gateway,
                        )
                        connector_class = get_connector_class(connector_name)
                        connector = connector_class(**init_params)
                        self.connectors[connector_name] = connector
                        RateOracle.get_instance().register_connector(connector)
                        self._logger.info(f"[DRY-RUN] Initialized public market connector for '{connector_name}'.")
                        return connector

                return orig_create(self, connector_name, trading_pairs, trading_required, api_keys)

            ConnectorManager.create_connector = patched_create_connector
            ConnectorManager._dry_run_patched = True
        except Exception as e:
            cls.logger().error(f"Failed to patch ConnectorManager for dry-run: {e}")

    def __init__(self, connectors: Dict[str, ConnectorBase], config: FundingRateArbitrageConfig):
        super().__init__(connectors, config)
        self.config = config
        self.active_funding_arbitrages = {}
        self.stopped_funding_arbitrages = {token: [] for token in self.config.tokens}
        # v1.1.0 Institutional Risk Shields
        self.token_quarantine_ledger: Dict[str, Dict] = {}  # token -> {"quarantine_until": float, "reason": str, "consecutive_losses": int}
        self.inversion_tick_counter: Dict[str, int] = {}    # token -> int (ticks of sustained rate inversion)
        self.token_last_closed_timestamp: Dict[str, float] = {} # token -> float (timestamp of last closure)
        # Defense 1: Entry Hysteresis persistence counters
        self.entry_candidate_ticks: Dict[str, int] = {}     # token -> int (consecutive ticks meeting entry criteria)
        # v1.3.0 Autonomous Auto-Rotation & Scaling TP Engine
        self.rotation_event_history: List[Dict] = []
        self.last_rotation_timestamp: float = 0.0
        self._last_rotation_check_ts: float = 0.0
        self._last_cmd_check_ts: float = 0.0
        self.is_rotating: bool = False
        # v1.4.0 Institutional Risk & Profit Overhaul State
        self.token_post_tp_lockout: Dict[str, float] = {}   # token -> timestamp until which entries are locked post-TP
        self.token_permanent_blacklist: Set[str] = set()     # tokens permanently banned due to cumulative deficit

    def get_token_tier(self, token: str) -> Tuple[int, Decimal]:
        """
        Determine token risk tier and maximum position size quote.
        Tier 1: Mega-cap (BTC, ETH, SOL)
        Tier 2: Major alts (AVAX, SUI, APT, NEAR, LINK, ARB, OP, INJ, SAND, etc.)
        Tier 3: Micro-cap / meme / high-volatility tokens (BLUR, 2Z, BERA, RSR, etc.)
        """
        canon = self.get_canonical_token(token).upper()
        t1_tokens = getattr(self.config, "tier_1_tokens", {"BTC", "ETH", "SOL"})
        t1_set = {self.get_canonical_token(x).upper() for x in (t1_tokens if isinstance(t1_tokens, (set, list, tuple)) else str(t1_tokens).split(","))}

        t2_tokens = getattr(self.config, "tier_2_tokens", {
            "AVAX", "SUI", "APT", "NEAR", "LINK", "ARB", "OP", "INJ",
            "DOGE", "XRP", "ADA", "MATIC", "DOT", "UNI", "LTC", "BCH", "BNB", "SAND"
        })
        t2_set = {self.get_canonical_token(x).upper() for x in (t2_tokens if isinstance(t2_tokens, (set, list, tuple)) else str(t2_tokens).split(","))}

        if canon in t1_set:
            return 1, Decimal(str(getattr(self.config, "tier_1_max_quote", Decimal("10000.0"))))
        elif canon in t2_set:
            return 2, Decimal(str(getattr(self.config, "tier_2_max_quote", Decimal("2000.0"))))
        else:
            return 3, Decimal(str(getattr(self.config, "tier_3_max_quote", Decimal("500.0"))))

    def get_effective_leverage(self, token: str, position_size_quote: Decimal) -> int:
        """
        专业动态杠杆引擎 (Dynamic Adaptive Leverage Engine):
        1. 若全局配置为 1x，强制全额现货级对冲（零爆仓风险）；
        2. 若单仓名义价值 <= min_leverage_capital_threshold (默认 $500)，自动降为 1x 免杠杆，
           避免小资金产生单腿爆仓风险与频繁防御止损；
        3. 若仓位规模充足 (> 阈值)，才启用目标杠杆（如 2x~3x）提升资金使用效率。
        """
        cfg_lev = max(1, int(getattr(self.config, "leverage", 3)))
        if cfg_lev == 1:
            return 1
        threshold = Decimal(str(getattr(self.config, "min_leverage_capital_threshold", Decimal("500.0"))))
        if position_size_quote <= threshold:
            return 1
        return cfg_lev


    def start(self, clock: Clock, timestamp: float) -> None:
        """
        Start the strategy.
        :param clock: Clock to use.
        :param timestamp: Current time.
        """
        super().start(clock, timestamp)
        self.apply_initial_setting()

        # Load previous closed trades history from arbitrage_trades.json if available
        try:
            import json, os, time
            now_ts = int(timestamp if timestamp is not None else time.time())
            data_file = "/home/hummingbot/data/arbitrage_trades.json"
            if os.path.exists(data_file):
                with open(data_file, "r") as f:
                    saved_info = json.load(f)
                for ct in saved_info.get("closed_trades", []):
                    t = ct.get("token")
                    if t:
                        if t not in self.stopped_funding_arbitrages:
                            self.stopped_funding_arbitrages[t] = []
                        self.stopped_funding_arbitrages[t].append(ct)
                # Gracefully retire rotated-out tokens, and rehydrate active tokens
                for ap in saved_info.get("active_positions", []):
                    t = ap.get("token")
                    if not t:
                        continue
                    if t not in self.config.tokens:
                        if t not in self.stopped_funding_arbitrages:
                            self.stopped_funding_arbitrages[t] = []
                        self.stopped_funding_arbitrages[t].append({
                            "token": t,
                            "connector_1": ap.get("connector_1", ""),
                            "connector_2": ap.get("connector_2", ""),
                            "close_reason": "Manual Rotation: Low Yield Ditch",
                            "close_timestamp": now_ts,
                            "open_timestamp": int(now_ts - ap.get("duration_seconds", 0)),
                            "trading_pnl": float(ap.get("unrealized_trading_pnl", 0)),
                            "funding_pnl": float(ap.get("funding_income", 0)),
                            "total_net_pnl": float(ap.get("net_pnl", 0)),
                            "leverage": int(getattr(self.config, "leverage", 3)),
                        })
                    else:
                        # Re-hydrate active position so it is preserved across restarts
                        side_val = ap.get("side", "BUY")
                        side_enum = TradeType.BUY if str(side_val) in ["BUY", "TradeType.BUY"] else TradeType.SELL
                        p1 = Decimal(str(ap.get("entry_price_1", ap.get("price_1", 1))))
                        p2 = Decimal(str(ap.get("entry_price_2", ap.get("price_2", 1))))
                        pos_size = Decimal(str(ap.get("position_size", ap.get("position_size_quote", self.config.position_size_quote))))
                        open_ts = ap.get("open_timestamp", int(now_ts - ap.get("duration_seconds", 0)))
                        eff_lev = Decimal(str(ap.get("leverage", self.get_effective_leverage(t, pos_size))))

                        self.active_funding_arbitrages[t] = {
                            "is_virtual": ap.get("is_virtual", True),
                            "connector_1": ap.get("connector_1", "hyperliquid_perpetual"),
                            "connector_2": ap.get("connector_2", "binance_perpetual"),
                            "side": side_enum,
                            "open_timestamp": open_ts,
                            "price_1": p1,
                            "price_2": p2,
                            "amount_1": pos_size / p1 if p1 else Decimal("0"),
                            "amount_2": pos_size / p2 if p2 else Decimal("0"),
                            "position_size_quote": pos_size,
                            "leverage": eff_lev,
                            "entry_fee_quote": Decimal(str(ap.get("entry_fee_quote", 0))),
                            "expected_profitability": Decimal(str(ap.get("expected_profitability", 0))),
                            "entry_profitability_after_fees": Decimal(str(ap.get("entry_spread_pct", 0))),
                            "payback_hours": float(ap.get("payback_hours", 0)),
                            "entry_friction_pct": float(ap.get("entry_friction_pct", 0)),
                            "funding_payments": ap.get("funding_payments", []),
                            "next_funding_time_1": int(ap.get("next_funding_1", 0)),
                            "next_funding_time_2": int(ap.get("next_funding_2", 0)),
                            "executors_ids": ap.get("executors_ids", ["DRY_RUN_SANDBOX"]),
                            "partial_tp_executed": bool(ap.get("partial_tp_executed", False)),
                            "locked_tp_pnl": float(ap.get("locked_tp_pnl", 0.0)),
                            "breakeven_active": bool(ap.get("breakeven_active", False)),
                        }
                        self.logger().info(
                            f"[REHYDRATE] Restored active position for {t}: "
                            f"{side_val} | Size: ${float(pos_size):,.2f} | Leverage: {eff_lev}x | Partial TP: {ap.get('partial_tp_executed', False)} | Breakeven: {ap.get('breakeven_active', False)}"
                        )
                # Rehydrate rotation status history
                rot_status = saved_info.get("rotation_status", {})
                if rot_status:
                    self.rotation_event_history = rot_status.get("rotation_events", [])
                    self.last_rotation_timestamp = float(rot_status.get("last_rotation_timestamp", 0.0))
                self.logger().info("[ROTATION] Loaded historical closed trades, rotation events, and rehydrated active positions.")

                # Auto-initialize permanent toxic blacklist and quarantine ledger from historical closed trades
                hard_blacklist_usd = float(getattr(self.config, "cumulative_loss_hard_blacklist_usd", 30.0))
                loss_thresh = float(getattr(self.config, "single_loss_quarantine_threshold", Decimal("15.0")))
                quarantine_h = float(getattr(self.config, "token_quarantine_hours", Decimal("24.0")))

                for t_name, t_trades in self.stopped_funding_arbitrages.items():
                    if t_trades:
                        cum_net_pnl = sum(float(p.get("total_net_pnl", 0)) for p in t_trades)
                        # Module 3: Hard Permanent Blacklist for toxic tokens exceeding cumulative loss threshold
                        if cum_net_pnl <= -hard_blacklist_usd:
                            self.token_permanent_blacklist.add(t_name)
                            self.token_quarantine_ledger[t_name] = {
                                "quarantine_until": now_ts + 86400 * 3650,  # 10 years (effectively permanent)
                                "reason": f"Permanent Blacklist: Cumulative Deficit (${cum_net_pnl:.2f} <= -${hard_blacklist_usd:.2f})",
                                "consecutive_losses": len(t_trades),
                                "loss_amount": cum_net_pnl,
                                "triggered_at": now_ts,
                            }
                            self.logger().warning(
                                f"🚫 [PERMANENT-BLACKLIST-INIT] Auto-blacklisted toxic token '{t_name}' "
                                f"due to severe cumulative historical loss (${cum_net_pnl:.2f} <= -${hard_blacklist_usd:.2f})."
                            )
                            continue

                        consec_losses = 0
                        for p in reversed(t_trades):
                            if float(p.get("total_net_pnl", 0)) < 0:
                                consec_losses += 1
                            else:
                                break
                        last_pnl = float(t_trades[-1].get("total_net_pnl", 0))

                        # Only quarantine if token has true net loss deficit AND (last heavy loss or consecutive recent losses)
                        if (last_pnl < -loss_thresh or (consec_losses >= 2 and cum_net_pnl < 0)) and cum_net_pnl < -loss_thresh:
                            self.token_quarantine_ledger[t_name] = {
                                "quarantine_until": now_ts + quarantine_h * 3600,
                                "reason": f"Historical Loss Deficit (${cum_net_pnl:.2f})",
                                "consecutive_losses": consec_losses,
                                "loss_amount": cum_net_pnl,
                                "triggered_at": now_ts,
                            }
                            self.logger().warning(
                                f"[QUARANTINE-INIT] Auto-quarantined token '{t_name}' due to heavy historical deficit "
                                f"(${cum_net_pnl:.2f}) for {quarantine_h:.0f}h."
                            )

                # Restore any saved permanent blacklist
                for b_tok in saved_info.get("permanent_blacklist", []):
                    self.token_permanent_blacklist.add(b_tok)

                # Also restore any directly quarantined tokens from json if saved
                for q_tok, q_data in saved_info.get("quarantined_tokens", {}).items():
                    if q_data.get("quarantine_until", 0) > now_ts:
                        self.token_quarantine_ledger[q_tok] = q_data
        except Exception as e:
            self.logger().error(f"[START-ERROR] Failed to load historical closed trades and active positions: {e}", exc_info=True)

        # Patch Hyperliquid WebSocket funding rate bug if needed
        try:
            from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_api_order_book_data_source import HyperliquidPerpetualAPIOrderBookDataSource
            if not getattr(HyperliquidPerpetualAPIOrderBookDataSource, "_funding_rate_patch_applied", False):
                _orig_parse = HyperliquidPerpetualAPIOrderBookDataSource._parse_funding_info_message
                async def _patched_parse(ds_self, raw_message, message_queue):
                    try:
                        data = raw_message.get("data", {})
                        ctx = data.get("ctx", data)
                        if isinstance(ctx, dict) and "funding" in ctx and "openInterest" in ctx:
                            oi = ctx.pop("openInterest", None)
                            try:
                                return await _orig_parse(ds_self, raw_message, message_queue)
                            finally:
                                if oi is not None:
                                    ctx["openInterest"] = oi
                    except Exception:
                        pass
                    return await _orig_parse(ds_self, raw_message, message_queue)
                HyperliquidPerpetualAPIOrderBookDataSource._parse_funding_info_message = _patched_parse
                HyperliquidPerpetualAPIOrderBookDataSource._funding_rate_patch_applied = True
                self.logger().info("[PATCH] Applied Hyperliquid funding rate WebSocket patch.")
        except Exception as e:
            self.logger().debug(f"Could not patch Hyperliquid data source: {e}")

        try:
            from hummingbot.client.hummingbot_application import HummingbotApplication
            app = HummingbotApplication.main_application()
            if app and getattr(app, "_mqtt", None) is not None:
                from hummingbot.remote_iface.mqtt import ETopicPublisher
                self.mqtt_enabled = True
                self._pub = ETopicPublisher("performance", use_bot_prefix=True)
                self.logger().info("[DRY-RUN] Initialized MQTT publisher for live UI performance stream.")
        except Exception as e:
            self.logger().debug(f"MQTT publisher not initialized: {e}")

    def on_tick(self):
        if self.ready_to_trade and self.market_data_provider.ready and not getattr(self, "_is_stop_triggered", False):
            # v1.3.0 Strategy Commands Listener & Auto-Rotation
            self.check_strategy_commands()
            self.check_universe_auto_rotation()

            actions = self.determine_executor_actions()
            for action in actions:
                self.executor_orchestrator.execute_action(action)
            self.publish_arbitrage_performance()

            # Periodic scan logging (every 60s)
            now = self.current_timestamp
            if not hasattr(self, "_last_scan_log_ts"):
                self._last_scan_log_ts = 0.0
            if now - self._last_scan_log_ts >= 60.0:
                self._last_scan_log_ts = now
                if not self.active_funding_arbitrages:
                    thresh_pct = self.config.min_funding_rate_profitability * 100
                    spread_info = []
                    for t in self.config.tokens:
                        try:
                            f_rep = self.get_funding_info_by_token(t)
                            best = self.get_most_profitable_combination(f_rep)
                            if best:
                                spread_info.append(f"{t}: {best[3] * 100:+.4f}%/d")
                        except Exception:
                            pass
                    self.logger().info(
                        f"[SCAN] Monitoring funding spreads (Min threshold: {thresh_pct:+.4f}%/day). "
                        f"Current spreads: [{', '.join(spread_info)}]. Waiting for spread to exceed threshold."
                    )

    def check_strategy_commands(self):
        """
        Poll and execute real-time commands from Dashboard UI (Zero-Downtime Hot Control Channel).
        """
        now = self.current_timestamp
        if now - getattr(self, "_last_cmd_check_ts", 0.0) < 2.0:
            return
        self._last_cmd_check_ts = now

        cmd_path = "/home/hummingbot/data/strategy_commands.json"
        if os.path.exists(cmd_path):
            try:
                with open(cmd_path, "r") as f:
                    cmd = json.load(f)
                os.remove(cmd_path)
                self.logger().info(f"[COMMAND] Received strategy command from Dashboard: {cmd}")
                act = cmd.get("action")
                if act == "add_token":
                    t = cmd.get("token")
                    if t:
                        asyncio.create_task(self.async_add_token(t))
                elif act == "remove_token":
                    t = cmd.get("token")
                    if t:
                        asyncio.create_task(self.async_remove_token(t))
                elif act == "trigger_rotation":
                    asyncio.create_task(self.run_universe_auto_rotation(force=True))
                elif act == "clear_quarantine":
                    t = cmd.get("token")
                    if t and t in self.token_quarantine_ledger:
                        del self.token_quarantine_ledger[t]
                        self.logger().info(f"[COMMAND] Cleared quarantine for {t}")
                elif act == "unblacklist_token":
                    t = cmd.get("token")
                    if t:
                        if t in self.token_permanent_blacklist:
                            self.token_permanent_blacklist.discard(t)
                            self.logger().info(f"[COMMAND] Pardoned & removed token {t} from permanent toxic blacklist.")
                        if t in self.token_quarantine_ledger:
                            del self.token_quarantine_ledger[t]
                            self.logger().info(f"[COMMAND] Cleared quarantine ledger for {t}.")
            except Exception as e:
                self.logger().warning(f"[COMMAND] Failed processing strategy command: {e}")

    def check_universe_auto_rotation(self):
        """
        Periodically evaluate cross-exchange perpetual universe and rotate stale idle tokens.
        """
        if not getattr(self.config, "auto_rotation_enabled", True):
            return
        if getattr(self, "is_rotating", False):
            return
        now = self.current_timestamp
        interval_sec = float(getattr(self.config, "auto_rotation_interval_minutes", 30)) * 60.0
        if not hasattr(self, "_last_rotation_check_ts") or self._last_rotation_check_ts == 0.0:
            # On startup, give 60s for connectors to establish streams before initial scan
            self._last_rotation_check_ts = now - interval_sec + 60.0
        if now - self._last_rotation_check_ts >= interval_sec:
            self._last_rotation_check_ts = now
            asyncio.create_task(self.run_universe_auto_rotation(force=False))

    async def async_add_token(self, token: str) -> bool:
        """
        Dynamically add a token to connector WebSocket streams and strategy watchlist without restart.
        """
        token = self.get_canonical_token(token).upper()
        if token in self.config.tokens:
            self.logger().info(f"[DYNAMIC-MARKET] Token {token} already in active watchlist.")
            return True
        self.logger().info(f"[DYNAMIC-MARKET] Dynamically mounting token {token} to connectors...")
        for c_name in self.connectors:
            connector = self.connectors[c_name]
            pair = self.get_trading_pair_for_connector(token, c_name)
            if hasattr(connector, "add_trading_pair"):
                try:
                    success = await connector.add_trading_pair(pair)
                    if not success:
                        self.logger().warning(f"[DYNAMIC-MARKET] Connector {c_name} reported failure adding {pair}")
                        return False
                except Exception as e:
                    self.logger().warning(f"[DYNAMIC-MARKET] Error adding {pair} on {c_name}: {e}")
                    return False
        if isinstance(self.config.tokens, list):
            self.config.tokens.append(token)
        elif isinstance(self.config.tokens, set):
            self.config.tokens.add(token)
        self.stopped_funding_arbitrages.setdefault(token, [])
        self.logger().info(f"[DYNAMIC-MARKET] Successfully mounted {token} to watchlist: {list(self.config.tokens)}")
        self._sync_tokens_to_config_files()
        return True

    async def async_remove_token(self, token: str) -> bool:
        """
        Dynamically remove a token from active watchlist and unregister from connectors if no active position.
        """
        token = self.get_canonical_token(token).upper()
        self.logger().info(f"[DYNAMIC-MARKET] Requesting dynamic removal of token {token}...")
        if token in self.config.tokens:
            if isinstance(self.config.tokens, list):
                self.config.tokens.remove(token)
            elif isinstance(self.config.tokens, set):
                self.config.tokens.discard(token)
        if token not in self.active_funding_arbitrages:
            for c_name in self.connectors:
                connector = self.connectors[c_name]
                pair = self.get_trading_pair_for_connector(token, c_name)
                if hasattr(connector, "remove_trading_pair"):
                    try:
                        await connector.remove_trading_pair(pair)
                    except Exception as e:
                        self.logger().warning(f"[DYNAMIC-MARKET] Error unmounting {pair} on {c_name}: {e}")
            self.logger().info(f"[DYNAMIC-MARKET] Successfully unmounted market {token} from connectors")
        else:
            self.logger().info(f"[DYNAMIC-MARKET] Token {token} has active position; market will remain mounted until graceful exit completes.")
        self._sync_tokens_to_config_files()
        return True

    def _sync_tokens_to_config_files(self):
        """
        Persist dynamic token universe modifications to YAML config file so restarts stay 100% in sync.
        """
        conf_paths = [
            "/home/hummingbot/conf/scripts/funding_arb_hyperliquid_binance_dry_run.yml",
        ]
        for conf_path in conf_paths:
            if not os.path.exists(conf_path):
                continue
            try:
                with open(conf_path, "r") as f:
                    lines = f.readlines()
                new_lines = []
                in_tokens = False
                tokens_written = False
                for line in lines:
                    if line.strip().startswith("tokens:"):
                        in_tokens = True
                        new_lines.append(line)
                        for t in self.config.tokens:
                            new_lines.append(f"- {t}\n")
                        tokens_written = True
                        continue
                    if in_tokens:
                        if line.strip().startswith("- "):
                            continue
                        else:
                            in_tokens = False
                    new_lines.append(line)
                if tokens_written:
                    with open(conf_path, "w") as f:
                        f.writelines(new_lines)
                    self.logger().info(f"[CONFIG-SYNC] Synchronized {len(self.config.tokens)} tokens to {conf_path}")
            except Exception as e:
                self.logger().warning(f"[CONFIG-SYNC] Failed updating tokens in {conf_path}: {e}")

    async def run_universe_auto_rotation(self, force: bool = False):
        """
        Execute full market radar scan and rotate idle low-yield tokens to high-APR candidates.
        """
        if self.is_rotating:
            return
        self.is_rotating = True
        try:
            self.logger().info(f"[AUTO-ROTATION] Starting universe opportunity scan (force={force})...")
            candidates = await asyncio.to_thread(self._fetch_market_rotation_candidates)
            if not candidates:
                self.logger().info("[AUTO-ROTATION] No viable cross-exchange market candidates found.")
                return

            min_apr = float(getattr(self.config, "auto_rotation_min_apr", 0.35))
            min_delta = float(getattr(self.config, "auto_rotation_min_apr_delta", 0.40))
            max_tokens = int(getattr(self.config, "auto_rotation_max_active_tokens", 6))

            # Filter candidates above threshold and not quarantined
            viable_candidates = [
                c for c in candidates
                if c["apr"] >= min_apr * 100.0
                and c["token"] not in self.token_quarantine_ledger
                and c["token"] not in self.config.tokens
            ]

            current_tokens = list(self.config.tokens)
            idle_tokens = [t for t in current_tokens if t not in self.active_funding_arbitrages]

            top_cand_preview = [f"{c['token']} ({c['apr']:+.1f}%)" for c in viable_candidates[:5]]
            self.logger().info(
                f"[AUTO-ROTATION] Watchlist: {len(current_tokens)} tokens (Idle: {idle_tokens}). "
                f"Top viable candidates: {top_cand_preview}"
            )

            # 1. If watchlist is below max_tokens, simply add top candidate
            if len(current_tokens) < max_tokens and viable_candidates:
                cand = viable_candidates.pop(0)
                success = await self.async_add_token(cand["token"])
                if success:
                    event = {
                        "timestamp": int(self.current_timestamp),
                        "retired_token": "NONE (Slot Expansion)",
                        "retired_apr": 0.0,
                        "added_token": cand["token"],
                        "added_apr": cand["apr"],
                        "apr_delta": cand["apr"],
                        "reason": f"Slot Expansion: Added high-yield candidate {cand['token']} ({cand['apr']:+.1f}% APR)"
                    }
                    self.rotation_event_history.insert(0, event)
                    self.rotation_event_history = self.rotation_event_history[:25]
                    self.last_rotation_timestamp = self.current_timestamp
                    return

            # 2. Check if an idle token should be rotated out
            idle_apr_map = {}
            for t in idle_tokens:
                if t in self.token_quarantine_ledger:
                    idle_apr_map[t] = -9999.0
                    continue
                try:
                    f_rep = self.get_funding_info_by_token(t)
                    best = self.get_most_profitable_combination(f_rep)
                    idle_apr_map[t] = (best[3] * 365.0 * 100.0) if best else 0.0
                except Exception:
                    idle_apr_map[t] = 0.0

            sorted_idle = sorted(idle_apr_map.keys(), key=lambda t: idle_apr_map[t])

            for idle_tok in sorted_idle:
                idle_apr = idle_apr_map[idle_tok]
                if not viable_candidates:
                    break
                best_cand = viable_candidates[0]
                cand_tok = best_cand["token"]
                cand_apr = best_cand["apr"]
                delta_apr = cand_apr - idle_apr

                is_quarantined = (idle_tok in self.token_quarantine_ledger)
                is_stale_low = (idle_apr < 5.0 and cand_apr >= min_apr * 100.0)
                has_major_delta = (delta_apr >= min_delta * 100.0)

                if is_quarantined or is_stale_low or has_major_delta or force:
                    viable_candidates.pop(0)
                    self.logger().info(
                        f"[AUTO-ROTATION] Evicting idle token {idle_tok} (APR {idle_apr:+.1f}%) -> "
                        f"Injecting candidate {cand_tok} (APR {cand_apr:+.1f}%, ΔAPR {delta_apr:+.1f}%)..."
                    )
                    await self.async_remove_token(idle_tok)
                    success = await self.async_add_token(cand_tok)
                    if success:
                        reason_desc = (
                            f"Quarantine Eviction ({idle_tok})" if is_quarantined else
                            (f"Stale Low APR Eviction ({idle_tok} {idle_apr:+.1f}%)" if is_stale_low else
                             f"Yield Super-Spread (ΔAPR {delta_apr:+.1f}%)")
                        )
                        event = {
                            "timestamp": int(self.current_timestamp),
                            "retired_token": idle_tok,
                            "retired_apr": float(idle_apr),
                            "added_token": cand_tok,
                            "added_apr": float(cand_apr),
                            "apr_delta": float(delta_apr),
                            "reason": f"Auto-Rotation: {reason_desc} replaced by {cand_tok} ({cand_apr:+.1f}% APR)"
                        }
                        self.rotation_event_history.insert(0, event)
                        self.rotation_event_history = self.rotation_event_history[:25]
                        self.last_rotation_timestamp = self.current_timestamp
                        self.logger().info(f"[AUTO-ROTATION] Completed rotation: {event['reason']}")
                        break
        except Exception as e:
            self.logger().warning(f"[AUTO-ROTATION] Error during universe rotation: {e}")
        finally:
            self.is_rotating = False

    def _fetch_market_rotation_candidates(self) -> List[Dict]:
        """
        Fetch all cross-exchange perpetuals from Binance and Hyperliquid REST APIs.
        """
        min_vol = float(getattr(self.config, "auto_rotation_min_volume_24h_usd", 2000000.0))
        try:
            # 1. Binance Ticker & PremiumIndex
            t_req = urllib.request.Request("https://fapi.binance.com/fapi/v1/ticker/24hr", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(t_req, timeout=6) as r:
                bn_tickers = {x["symbol"][:-4]: float(x.get("quoteVolume", 0)) for x in json.loads(r.read()) if x.get("symbol", "").endswith("USDT")}

            p_req = urllib.request.Request("https://fapi.binance.com/fapi/v1/premiumIndex", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(p_req, timeout=6) as r:
                bn_rates = {x["symbol"][:-4]: {"rate_daily": float(x.get("lastFundingRate", 0)) * 3.0, "price": float(x.get("markPrice", 0))} for x in json.loads(r.read()) if x.get("symbol", "").endswith("USDT")}

            # 2. Hyperliquid Info
            hl_req = urllib.request.Request(
                "https://api.hyperliquid.xyz/info",
                data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(hl_req, timeout=6) as r:
                hl_data = json.loads(r.read())
                universe = hl_data[0].get("universe", [])
                ctxs = hl_data[1] if len(hl_data) > 1 else []
                hl_rates = {}
                for m, c in zip(universe, ctxs):
                    coin = m.get("name")
                    if coin:
                        hl_rates[coin] = {
                            "rate_daily": float(c.get("funding", 0)) * 24.0,
                            "price": float(c.get("markPx", 0)),
                            "vol_usd": float(c.get("dayNtlVlm", 0))
                        }

            # 3. Match cross-exchange pairs
            results = []
            for c, h in hl_rates.items():
                if c in bn_rates and c in bn_tickers:
                    b = bn_rates[c]
                    # Enforce strict 1.5% max price divergence to prevent entry friction trap and illiquid tokens
                    if b["price"] > 0 and abs(h["price"] - b["price"]) / b["price"] <= 0.015:
                        if b_vol >= min_vol and h["vol_usd"] >= 500000.0:
                            diff = abs(h["rate_daily"] - b["rate_daily"])
                            apr = diff * 365.0 * 100.0
                            results.append({
                                "token": c,
                                "apr": apr,
                                "daily_diff": diff * 100.0,
                                "bn_vol": b_vol,
                                "hl_vol": h["vol_usd"]
                            })
            results.sort(key=lambda x: x["apr"], reverse=True)
            return results
        except Exception as e:
            self.logger().debug(f"[AUTO-ROTATION] Scanner fetch failed: {e}")
            return []

    def publish_arbitrage_performance(self):
        """
        Aggregate and publish real-time arbitrage performance and trade details via MQTT and local disk.
        """
        now = self.current_timestamp
        active_list = []
        total_unrealized_pnl = Decimal("0")
        total_funding_pnl = Decimal("0")
        total_volume = Decimal("0")

        for token, arb in self.active_funding_arbitrages.items():
            funding_income = sum(Decimal(str(p.get("amount", 0))) for p in arb.get("funding_payments", []))
            total_funding_pnl += funding_income
            pos_size = Decimal(str(arb.get("position_size_quote", self.config.position_size_quote)))
            total_volume += pos_size * 2

            p1_curr = Decimal(str(arb.get("price_1", 0)))
            p2_curr = Decimal(str(arb.get("price_2", 0)))
            unrealized_trading_pnl = Decimal("0")

            try:
                trading_pair_1 = self.get_trading_pair_for_connector(token, arb["connector_1"])
                trading_pair_2 = self.get_trading_pair_for_connector(token, arb["connector_2"])
                p1_open = Decimal(str(arb.get("price_1", 0)))
                p2_open = Decimal(str(arb.get("price_2", 0)))

                is_buy = (arb["side"] == TradeType.BUY) if isinstance(arb["side"], TradeType) else (str(arb["side"]) in ["BUY", "TradeType.BUY"])

                p1_curr = self.get_safe_quote_price(
                    connector_name=arb["connector_1"],
                    trading_pair=trading_pair_1,
                    quote_volume=pos_size,
                    is_buy=not is_buy,
                    fallback_price=p1_open,
                )
                p2_curr = self.get_safe_quote_price(
                    connector_name=arb["connector_2"],
                    trading_pair=trading_pair_2,
                    quote_volume=pos_size,
                    is_buy=is_buy,
                    fallback_price=p2_open,
                )
                amt1 = Decimal(str(arb.get("amount_1", pos_size / p1_open)))
                amt2 = Decimal(str(arb.get("amount_2", pos_size / p2_open)))

                leg1_pnl = (p1_curr - p1_open) * amt1 if is_buy else (p1_open - p1_curr) * amt1
                leg2_pnl = (p2_open - p2_curr) * amt2 if is_buy else (p2_curr - p2_open) * amt2
                unrealized_trading_pnl = leg1_pnl + leg2_pnl
            except Exception:
                pass

            total_unrealized_pnl += unrealized_trading_pnl
            net_position_pnl = unrealized_trading_pnl + funding_income
            elapsed_sec = max(0, now - arb.get("open_timestamp", now))

            # Calculate estimated liquidation price and safety margin distance
            lev = Decimal(str(arb.get("leverage", self.get_effective_leverage(token, pos_size))))
            mmr = Decimal("0.02")  # ~2% maintenance margin ratio
            p1_open_val = Decimal(str(arb.get("price_1", p1_curr)))
            p2_open_val = Decimal(str(arb.get("price_2", p2_curr)))
            is_buy_side = (arb["side"] == TradeType.BUY) if isinstance(arb["side"], TradeType) else (str(arb["side"]) in ["BUY", "TradeType.BUY"])

            if is_buy_side:
                liq_p1 = max(Decimal("0"), p1_open_val * (Decimal("1") - Decimal("1") / lev + mmr))
                dist1_pct = (p1_curr - liq_p1) / p1_curr if p1_curr > 0 else Decimal("1")
                liq_p2 = p2_open_val * (Decimal("1") + Decimal("1") / lev - mmr)
                dist2_pct = (liq_p2 - p2_curr) / p2_curr if p2_curr > 0 else Decimal("1")
            else:
                liq_p1 = p1_open_val * (Decimal("1") + Decimal("1") / lev - mmr)
                dist1_pct = (liq_p1 - p1_curr) / p1_curr if p1_curr > 0 else Decimal("1")
                liq_p2 = max(Decimal("0"), p2_open_val * (Decimal("1") - Decimal("1") / lev + mmr))
                dist2_pct = (p2_curr - liq_p2) / p2_curr if p2_curr > 0 else Decimal("1")

            min_dist_pct = min(dist1_pct, dist2_pct)
            t_tier = int(self.get_token_tier(token)[0])
            if t_tier == 1:
                req_buf = float(getattr(self.config, "tier_1_liquidation_buffer", Decimal("0.04")))
            elif t_tier == 2:
                req_buf = float(getattr(self.config, "tier_2_liquidation_buffer", Decimal("0.08")))
            else:
                req_buf = float(getattr(self.config, "tier_3_liquidation_buffer", Decimal("0.12")))

            active_list.append({
                "token": token,
                "tier": t_tier,
                "connector_1": arb["connector_1"],
                "connector_2": arb["connector_2"],
                "side": arb["side"].name if hasattr(arb["side"], "name") else str(arb["side"]),
                "entry_price_1": float(arb.get("price_1", 0)),
                "entry_price_2": float(arb.get("price_2", 0)),
                "current_price_1": float(p1_curr),
                "current_price_2": float(p2_curr),
                "liq_price_1": float(liq_p1),
                "liq_price_2": float(liq_p2),
                "liq_distance_1_pct": float(dist1_pct),
                "liq_distance_2_pct": float(dist2_pct),
                "min_liq_distance_pct": float(min_dist_pct),
                "liquidation_buffer_req_pct": req_buf,
                "partial_tp_executed": bool(arb.get("partial_tp_executed", False)),
                "locked_tp_pnl": float(arb.get("locked_tp_pnl", 0.0)),
                "breakeven_active": bool(arb.get("breakeven_active", False)),
                "leverage": int(lev),
                "long_leverage": int(lev),
                "short_leverage": int(lev),
                "position_size": float(pos_size),
                "funding_income": float(funding_income),
                "settlement_count": len(arb.get("funding_payments", [])),
                "funding_payments": arb.get("funding_payments", []),
                "unrealized_trading_pnl": float(unrealized_trading_pnl),
                "net_pnl": float(net_position_pnl),
                "net_pnl_pct": float(net_position_pnl / pos_size) if pos_size else 0.0,
                "duration_seconds": int(elapsed_sec),
                "payback_hours": float(arb.get("payback_hours", 0.0)),
                "entry_spread_pct": float(arb.get("entry_profitability_after_fees", 0.0)),
                "next_funding_1": int(arb.get("next_funding_time_1", 0)),
                "next_funding_2": int(arb.get("next_funding_time_2", 0)),
            })

        # Closed trades
        closed_list = []
        total_realized_pnl = Decimal("0")
        tp_count = 0
        sl_count = 0

        for token, pos_list in self.stopped_funding_arbitrages.items():
            for pos in pos_list:
                t_pnl = Decimal(str(pos.get("trading_pnl", 0)))
                f_pnl = Decimal(str(pos.get("funding_pnl", 0)))
                tot_pnl = Decimal(str(pos.get("total_net_pnl", t_pnl + f_pnl)))
                total_realized_pnl += tot_pnl
                p_size = Decimal(str(pos.get("position_size_quote", self.config.position_size_quote)))
                total_volume += p_size * 4  # 2 legs entered + 2 legs closed
                reason = pos.get("close_reason", "Completed")
                if "Take Profit" in reason:
                    tp_count += 1
                elif "Stop Loss" in reason:
                    sl_count += 1
                closed_list.append({
                    "token": token,
                    "connector_1": pos.get("connector_1", ""),
                    "connector_2": pos.get("connector_2", ""),
                    "close_reason": reason,
                    "close_timestamp": int(pos.get("close_timestamp", 0)),
                    "open_timestamp": int(pos.get("open_timestamp", 0)),
                    "trading_pnl": float(t_pnl),
                    "funding_pnl": float(f_pnl),
                    "total_net_pnl": float(tot_pnl),
                    "position_size": float(p_size),
                    "leverage": int(self.config.leverage),
                    "close_price_1": float(pos.get("close_price_1", 0.0)),
                    "close_price_2": float(pos.get("close_price_2", 0.0)),
                    "open_price_1": float(pos.get("price_1", 0.0)),
                    "open_price_2": float(pos.get("price_2", 0.0)),
                    "duration_seconds": int(pos.get("duration_seconds", max(0, int(pos.get("close_timestamp", 0)) - int(pos.get("open_timestamp", 0))))),
                })

        # Live Market Comparison across watched tokens
        market_list = []
        try:
            for token in self.config.tokens:
                f_report = self.get_funding_info_by_token(token)
                best_comb = self.get_most_profitable_combination(f_report)
                row = {"token": token}
                for c_name, c_info in f_report.items():
                    norm_rate = self.get_normalized_funding_rate_in_seconds(f_report, c_name) * self.funding_profitability_interval * 100
                    row[f"{c_name}_rate_daily"] = float(norm_rate)
                    row[f"{c_name}_next_funding"] = int(c_info.next_funding_utc_timestamp)
                if best_comb:
                    c1, c2, side, diff = best_comb
                    row["best_path"] = f"{c1} -> {c2}"
                    row["daily_rate_diff_pct"] = float(diff * 100)
                market_list.append(row)
        except Exception:
            pass

        global_pnl = total_realized_pnl + total_unrealized_pnl + total_funding_pnl

        report = {
            "funding_rate_arb": {
                "controller_name": "funding_rate_arb",
                "trading_pair": ",".join(self.config.tokens),
                "connector_name": ",".join(self.config.connectors),
                "performance": {
                    "realized_pnl_quote": float(total_realized_pnl),
                    "unrealized_pnl_quote": float(total_unrealized_pnl + total_funding_pnl),
                    "global_pnl_quote": float(global_pnl),
                    "volume_traded": float(total_volume),
                    "close_type_counts": {
                        "CloseType.TAKE_PROFIT": tp_count,
                        "CloseType.STOP_LOSS": sl_count,
                    }
                },
                "custom_info": {
                    "is_dry_run": bool(self.config.dry_run),
                    "leverage": int(self.config.leverage),
                    "min_funding_rate_profitability": float(self.config.min_funding_rate_profitability),
                    "min_funding_rate_profitability_pct": float(self.config.min_funding_rate_profitability * 100),
                    "active_positions": active_list,
                    "closed_trades": closed_list,
                    "market_comparison": market_list,
                    "quarantined_tokens": {
                        tok: {
                            "quarantine_until": float(q.get("quarantine_until", 0)),
                            "remaining_seconds": max(0, int(q.get("quarantine_until", 0) - self.current_timestamp)),
                            "remaining_hours": max(0.0, float((q.get("quarantine_until", 0) - self.current_timestamp) / 3600.0)),
                            "reason": str(q.get("reason", "Risk Quarantine")),
                            "consecutive_losses": int(q.get("consecutive_losses", 0)),
                            "loss_amount": float(q.get("loss_amount", 0.0)),
                        } for tok, q in self.token_quarantine_ledger.items() if q.get("quarantine_until", 0) > self.current_timestamp
                    },
                    "permanent_blacklist": sorted(list(self.token_permanent_blacklist)),
                    "post_tp_lockout": {
                        tok: {
                            "lockout_until": float(ts),
                            "remaining_seconds": max(0, int(ts - self.current_timestamp)),
                            "remaining_minutes": max(0.0, float((ts - self.current_timestamp) / 60.0)),
                        } for tok, ts in self.token_post_tp_lockout.items() if ts > self.current_timestamp
                    },
                    "rotation_status": {
                        "auto_rotation_enabled": bool(getattr(self.config, "auto_rotation_enabled", True)),
                        "last_rotation_timestamp": float(getattr(self, "last_rotation_timestamp", 0.0)),
                        "next_rotation_timestamp": float(getattr(self, "_last_rotation_check_ts", 0.0) + getattr(self.config, "auto_rotation_interval_minutes", 30) * 60.0),
                        "rotation_interval_minutes": int(getattr(self.config, "auto_rotation_interval_minutes", 30)),
                        "min_apr_threshold": float(getattr(self.config, "auto_rotation_min_apr", 0.35)),
                        "min_apr_delta": float(getattr(self.config, "auto_rotation_min_apr_delta", 0.40)),
                        "rotation_events": getattr(self, "rotation_event_history", [])[:15],
                    },
                }
            }
        }

        # 1. Publish via MQTT if available
        if getattr(self, "_pub", None):
            try:
                self._pub(report)
            except Exception as e:
                self.logger().debug(f"Failed to publish arbitrage performance via MQTT: {e}")

        # 2. Persist local JSON copy to data directory for instant UI read
        try:
            import json
            data_dir = "/home/hummingbot/data"
            if not os.path.isdir(data_dir):
                cfg = getattr(self, "client_config_map", None)
                data_dir = getattr(cfg, "data_file_path", None) if cfg else None
            if data_dir and os.path.isdir(data_dir):
                json_path = os.path.join(data_dir, "arbitrage_trades.json")
                dump_data = report["funding_rate_arb"]["custom_info"].copy()
                dump_data["performance"] = report["funding_rate_arb"]["performance"]
                with open(json_path, "w") as f:
                    json.dump(dump_data, f, indent=2)
        except Exception as e:
            self.logger().debug(f"Failed to persist arbitrage_trades.json: {e}")

    def apply_initial_setting(self):
        if self.config.dry_run:
            self.logger().info("[DRY-RUN] Dry run mode active: skipping exchange leverage and position mode API calls.")
            return

        for connector_name, connector in self.connectors.items():
            if self.is_perpetual(connector_name):
                position_mode = PositionMode.ONEWAY if connector_name == "hyperliquid_perpetual" else PositionMode.HEDGE
                connector.set_position_mode(position_mode)
                for trading_pair in self.market_data_provider.get_trading_pairs(connector_name):
                    token = trading_pair.split("-")[0]
                    eff_lev = self.get_effective_leverage(token, self.config.position_size_quote)
                    connector.set_leverage(trading_pair, eff_lev)


    def get_funding_info_by_token(self, token):
        """
        This method provides the funding rates across all the connectors
        """
        funding_rates = {}
        for connector_name, connector in self.connectors.items():
            trading_pair = self.get_trading_pair_for_connector(token, connector_name)
            funding_rates[connector_name] = connector.get_funding_info(trading_pair)
        return funding_rates

    def get_safe_quote_price(self, connector_name: str, trading_pair: str, quote_volume: Decimal, is_buy: bool, fallback_price: Decimal = None) -> Decimal:
        """
        Safely retrieve price for quote volume. If orderbook depth is insufficient or returns NaN,
        fall back to MidPrice, LastTradePrice, or fallback_price. Never returns NaN.
        """
        try:
            res = self.market_data_provider.get_price_for_quote_volume(
                connector_name=connector_name,
                trading_pair=trading_pair,
                quote_volume=float(quote_volume),
                is_buy=is_buy,
            )
            p = res.result_price
            if p is not None and not math.isnan(p) and p > 0:
                return Decimal(str(p))
        except Exception:
            pass

        # Try MidPrice
        try:
            mid = self.market_data_provider.get_price_by_type(connector_name, trading_pair, PriceType.MidPrice)
            if mid is not None and not math.isnan(float(mid)) and float(mid) > 0:
                return Decimal(str(mid))
        except Exception:
            pass

        # Try LastTradePrice
        try:
            last = self.market_data_provider.get_price_by_type(connector_name, trading_pair, PriceType.LastTradePrice)
            if last is not None and not math.isnan(float(last)) and float(last) > 0:
                return Decimal(str(last))
        except Exception:
            pass

        if fallback_price is not None and not fallback_price.is_nan() and fallback_price > 0:
            return fallback_price

        return Decimal("1")

    def get_current_profitability_after_fees(self, token: str, connector_1: str, connector_2: str, side: TradeType, quote_volume: Decimal = None):
        """
        This methods compares the profitability of buying at market in the two exchanges. If the side is TradeType.BUY
        means that the operation is long on connector 1 and short on connector 2.
        """
        vol = quote_volume if quote_volume is not None else self.config.position_size_quote
        trading_pair_1 = self.get_trading_pair_for_connector(token, connector_1)
        trading_pair_2 = self.get_trading_pair_for_connector(token, connector_2)

        connector_1_price = self.get_safe_quote_price(
            connector_name=connector_1,
            trading_pair=trading_pair_1,
            quote_volume=vol,
            is_buy=side == TradeType.BUY,
        )
        connector_2_price = self.get_safe_quote_price(
            connector_name=connector_2,
            trading_pair=trading_pair_2,
            quote_volume=vol,
            is_buy=side != TradeType.BUY,
        )
        estimated_fees_connector_1 = self.connectors[connector_1].get_fee(
            base_currency=trading_pair_1.split("-")[0],
            quote_currency=trading_pair_1.split("-")[1],
            order_type=OrderType.MARKET,
            order_side=TradeType.BUY,
            amount=vol / connector_1_price,
            price=connector_1_price,
            is_maker=False,
            position_action=PositionAction.OPEN
        ).percent
        estimated_fees_connector_2 = self.connectors[connector_2].get_fee(
            base_currency=trading_pair_2.split("-")[0],
            quote_currency=trading_pair_2.split("-")[1],
            order_type=OrderType.MARKET,
            order_side=TradeType.BUY,
            amount=vol / connector_2_price,
            price=connector_2_price,
            is_maker=False,
            position_action=PositionAction.OPEN
        ).percent

        m1 = Decimal(str(self.get_token_multiplier(token, connector_1)))
        m2 = Decimal(str(self.get_token_multiplier(token, connector_2)))
        p1_norm = connector_1_price / m1
        p2_norm = connector_2_price / m2

        if side == TradeType.BUY:
            estimated_trade_pnl_pct = (p2_norm - p1_norm) / p1_norm
        else:
            estimated_trade_pnl_pct = (p1_norm - p2_norm) / p2_norm
        return estimated_trade_pnl_pct - estimated_fees_connector_1 - estimated_fees_connector_2

    def get_most_profitable_combination(self, funding_info_report: Dict):
        best_combination = None
        highest_profitability = 0
        for connector_1 in funding_info_report:
            for connector_2 in funding_info_report:
                if connector_1 != connector_2:
                    rate_connector_1 = self.get_normalized_funding_rate_in_seconds(funding_info_report, connector_1)
                    rate_connector_2 = self.get_normalized_funding_rate_in_seconds(funding_info_report, connector_2)
                    funding_rate_diff = abs(rate_connector_1 - rate_connector_2) * self.funding_profitability_interval
                    if funding_rate_diff > highest_profitability:
                        trade_side = TradeType.BUY if rate_connector_1 < rate_connector_2 else TradeType.SELL
                        highest_profitability = funding_rate_diff
                        best_combination = (connector_1, connector_2, trade_side, funding_rate_diff)
        return best_combination

    def get_normalized_funding_rate_in_seconds(self, funding_info_report, connector_name):
        return funding_info_report[connector_name].rate / self.funding_payment_interval_map.get(connector_name, 60 * 60 * 8)

    def check_virtual_funding_payments(self):
        """
        Check and simulate funding payments for active virtual positions when in dry-run mode.
        """
        now = self.current_timestamp
        for token, arb in list(self.active_funding_arbitrages.items()):
            if not arb.get("is_virtual"):
                continue

            funding_info_report = self.get_funding_info_by_token(token)
            legs = [
                ("connector_1", "next_funding_time_1", arb["side"]),
                ("connector_2", "next_funding_time_2", TradeType.SELL if arb["side"] == TradeType.BUY else TradeType.BUY)
            ]

            for conn_key, time_key, side in legs:
                conn_name = arb[conn_key]
                next_time = arb.get(time_key, 0)
                if now >= next_time and next_time > 0:
                    current_info = funding_info_report[conn_name]
                    rate = current_info.rate
                    # Long pays if rate > 0, receives if rate < 0 -> payment = - rate * quote_size
                    # Short receives if rate > 0, pays if rate < 0 -> payment = + rate * quote_size
                    is_buy = (side == TradeType.BUY) if isinstance(side, TradeType) else (str(side) in ["BUY", "TradeType.BUY"])
                    payment_amount = -1 * arb["position_size_quote"] * rate if is_buy else arb["position_size_quote"] * rate
                    side_str = side.name if hasattr(side, "name") else str(side)

                    arb["funding_payments"].append({
                        "timestamp": int(now),
                        "connector": conn_name,
                        "side": side_str,
                        "rate": float(rate),
                        "amount": float(payment_amount),
                    })

                    interval = self.funding_payment_interval_map.get(conn_name, 60 * 60 * 8)
                    if getattr(current_info, "next_funding_utc_timestamp", 0) > now:
                        arb[time_key] = current_info.next_funding_utc_timestamp
                    else:
                        arb[time_key] = now + interval

                    self.logger().info(
                        f"[DRY-RUN] Simulated Funding Payment on {conn_name} for {token} ({side_str}): "
                        f"Rate={float(rate):+.6%}, Settlement={float(payment_amount):+.4f} Quote"
                    )

    def calculate_dynamic_position_size(
        self,
        token: str,
        connector_1: str,
        connector_2: str,
        trade_side: TradeType,
        max_target_size: Decimal
    ) -> Decimal:
        """
        Dynamically calculate safe position size based on token risk tier and order book depth of both exchanges.
        Takes the minimum safe capacity between connector 1 and connector 2 to guarantee
        neither leg exceeds the order book depth limit or suffers excessive slippage.
        """
        try:
            tier, tier_cap = self.get_token_tier(token)
            effective_target = min(max_target_size, tier_cap)

            base_depth_ratio = Decimal(str(getattr(self.config, "max_orderbook_depth_ratio", Decimal("0.15"))))
            depth_ratio = min(base_depth_ratio, Decimal("0.10")) if tier == 3 else base_depth_ratio
            min_size = Decimal(str(getattr(self.config, "min_position_size_quote", Decimal("100.0"))))

            safe_capacities = []
            legs = [
                (connector_1, trade_side == TradeType.BUY),
                (connector_2, trade_side != TradeType.BUY)
            ]

            for conn_name, is_buy in legs:
                tp = self.get_trading_pair_for_connector(token, conn_name)
                ob = self.market_data_provider.get_order_book(conn_name, tp)
                if ob is not None:
                    bids_df, asks_df = ob.snapshot
                    df = asks_df if is_buy else bids_df
                    if df is not None and not df.empty:
                        # Top 25 levels quote depth (within ~0.15% tight spread)
                        top_n = df.head(getattr(self.config, "orderbook_depth_levels", 25))
                        depth_usd = Decimal(str(float((top_n["price"] * top_n["amount"]).sum())))
                        safe_cap = depth_usd * depth_ratio
                        if safe_cap > 0:
                            safe_capacities.append(safe_cap)

            if safe_capacities:
                bottleneck_capacity = min(safe_capacities)
                calculated_size = min(effective_target, bottleneck_capacity)
                if calculated_size >= min_size:
                    step = Decimal("50")
                    rounded_size = (calculated_size // step) * step
                    final_size = max(min_size, rounded_size)
                    self.logger().info(
                        f"[DYNAMIC-SIZING] Token {token} (Tier {tier}, Tier Cap: ${float(tier_cap):,.0f}): "
                        f"OrderBook Safe Capacity=${float(bottleneck_capacity):,.0f} (Ratio: {float(depth_ratio):.0%}) "
                        f"-> Dynamic Size: ${float(final_size):,.0f}"
                    )
                    return final_size
                else:
                    self.logger().warning(
                        f"[DYNAMIC-SIZING] Token {token} (Tier {tier}): OrderBook safe capacity (${float(bottleneck_capacity):,.1f}) "
                        f"is below min allowed (${float(min_size):,.1f}). Using min size."
                    )
                    return min_size
        except Exception as e:
            self.logger().debug(f"Dynamic sizing calculation error for {token}: {e}")

        tier, tier_cap = self.get_token_tier(token)
        return min(max_target_size, tier_cap)

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        """
        Evaluate if a new set of positions has to be created for each token without active arbitrage.
        In dry-run mode, record virtual positions without dispatching real CreateExecutorActions.
        """
        create_actions = []
        for token in self.config.tokens:
            if token not in self.active_funding_arbitrages:
                # 0. Permanent Blacklist Check (毒币终生累计亏损熔断)
                if token in self.token_permanent_blacklist:
                    self.logger().debug(
                        f"[BLACKLIST-SKIP] Token '{token}' is permanently blacklisted due to severe cumulative historical loss. Skipping entry."
                    )
                    self.entry_candidate_ticks[token] = 0
                    continue

                # 1. Toxic Asset Quarantine Check (拦截有毒重亏损资产)
                quarantine_info = self.token_quarantine_ledger.get(token)
                if quarantine_info:
                    quarantine_until = quarantine_info.get("quarantine_until", 0)
                    if self.current_timestamp < quarantine_until:
                        remaining_m = (quarantine_until - self.current_timestamp) / 60
                        self.logger().debug(
                            f"[QUARANTINE-SKIP] Token '{token}' is quarantined for next {remaining_m:.1f}m. "
                            f"Reason: {quarantine_info.get('reason')}."
                        )
                        self.entry_candidate_ticks[token] = 0
                        continue
                    else:
                        self.logger().info(f"[QUARANTINE-RELEASE] Token '{token}' quarantine expired. Returned to active pool.")
                        del self.token_quarantine_ledger[token]

                # 1b. Post-Take-Profit Anti-Churning Lockout (止盈防高位回踩锁定)
                post_tp_until = self.token_post_tp_lockout.get(token, 0)
                if self.current_timestamp < post_tp_until:
                    remaining_tp_m = (post_tp_until - self.current_timestamp) / 60
                    self.logger().debug(
                        f"[POST-TP-LOCKOUT] Token '{token}' took profit recently. In post-TP anti-reversion lockout for {remaining_tp_m:.1f}m."
                    )
                    self.entry_candidate_ticks[token] = 0
                    continue

                # 2. Re-entry Cooldown Check (防假止损/频繁开关磨损)
                last_closed = self.token_last_closed_timestamp.get(token, 0)
                cooldown_sec = getattr(self.config, "reentry_cooldown_seconds", 1800)
                if self.current_timestamp - last_closed < cooldown_sec:
                    remaining_s = cooldown_sec - (self.current_timestamp - last_closed)
                    self.logger().debug(
                        f"[COOLDOWN-SKIP] Token '{token}' closed recently. In re-entry cooldown for {remaining_s:.0f}s."
                    )
                    self.entry_candidate_ticks[token] = 0
                    continue

                # 3. Pre-Settlement Blackout Window (费率结算前黑障区保护)
                blackout_m = int(getattr(self.config, "settlement_blackout_minutes", 30))
                if blackout_m > 0:
                    now_utc = int(self.current_timestamp)
                    # 8-hour funding intervals: 00:00, 08:00, 16:00 UTC (every 28800s)
                    seconds_into_cycle = now_utc % 28800
                    seconds_to_settlement = 28800 - seconds_into_cycle
                    if seconds_to_settlement <= blackout_m * 60:
                        tier, _ = self.get_token_tier(token)
                        tier3_only = getattr(self.config, "settlement_blackout_tier3_only", False)
                        if (not tier3_only and tier > 1) or (tier3_only and tier == 3):
                            self.entry_candidate_ticks[token] = 0
                            self.logger().debug(
                                f"[SETTLEMENT-BLACKOUT] Token '{token}' (Tier {tier}) is within {seconds_to_settlement/60:.1f}m of 8h settlement. "
                                f"Entry frozen to protect against rate manipulation and sudden flips."
                            )
                            continue

                funding_info_report = self.get_funding_info_by_token(token)
                best_combination = self.get_most_profitable_combination(funding_info_report)
                if not best_combination:
                    self.entry_candidate_ticks[token] = 0
                    continue

                connector_1, connector_2, trade_side, expected_profitability = best_combination
                if expected_profitability < self.config.min_funding_rate_profitability:
                    self.entry_candidate_ticks[token] = 0
                    continue

                # Dynamically size position first to reflect real orderbook capacity
                pos_size = self.config.position_size_quote
                if getattr(self.config, "dynamic_position_sizing", True):
                    pos_size = self.calculate_dynamic_position_size(
                        token=token,
                        connector_1=connector_1,
                        connector_2=connector_2,
                        trade_side=trade_side,
                        max_target_size=self.config.position_size_quote,
                    )

                current_profitability = self.get_current_profitability_after_fees(
                    token, connector_1, connector_2, trade_side, quote_volume=pos_size
                )

                # Mode 1: Payback Period Barrier (回本周期硬性拦截)
                hourly_yield = expected_profitability / Decimal("24")
                if current_profitability < 0:
                    friction_pct = -current_profitability
                    payback_hours = (friction_pct / hourly_yield) if hourly_yield > 0 else Decimal("999999")
                else:
                    friction_pct = Decimal("0")
                    payback_hours = Decimal("0")

                max_allowed_payback = Decimal(str(getattr(self.config, "max_payback_hours", Decimal("12.0"))))
                if payback_hours > max_allowed_payback:
                    self.logger().info(
                        f"[PAYBACK-BARRIER] Token {token} entry friction {float(friction_pct):.3%} requires "
                        f"{float(payback_hours):.1f}h of funding to breakeven (limit: {float(max_allowed_payback):.1f}h). "
                        f"Skipping entry to protect capital from excessive spread."
                    )
                    self.entry_candidate_ticks[token] = 0
                    continue

                # Mode 2: Hard Spread Friction Ceiling (流动性滑点与负基差硬天花板)
                max_entry_friction = Decimal(str(getattr(self.config, "max_entry_spread_friction_pct", Decimal("0.005"))))
                if friction_pct > max_entry_friction:
                    self.logger().info(
                        f"[SPREAD-FRICTION-BARRIER] Token {token} entry friction {float(friction_pct):.3%} exceeds "
                        f"hard limit of {float(max_entry_friction):.2%}. Skipping entry to avoid illiquid slippage trap."
                    )
                    self.entry_candidate_ticks[token] = 0
                    continue

                if self.config.trade_profitability_condition_to_enter:
                    if current_profitability < 0:
                        self.logger().info(
                            f"Best Combination: {connector_1} | {connector_2} | {trade_side} "
                            f"Funding rate profitability: {expected_profitability} "
                            f"Trading profitability after fees: {current_profitability} "
                            f"Trade profitability is negative, skipping..."
                        )
                        self.entry_candidate_ticks[token] = 0
                        continue

                trading_pair_1 = self.get_trading_pair_for_connector(token, connector_1)
                trading_pair_2 = self.get_trading_pair_for_connector(token, connector_2)
                p1 = self.get_safe_quote_price(
                    connector_name=connector_1,
                    trading_pair=trading_pair_1,
                    quote_volume=pos_size,
                    is_buy=trade_side == TradeType.BUY,
                )
                p2 = self.get_safe_quote_price(
                    connector_name=connector_2,
                    trading_pair=trading_pair_2,
                    quote_volume=pos_size,
                    is_buy=trade_side != TradeType.BUY,
                )

                # Asset Consistency & Price Divergence Circuit Breaker (同名不同币与假币强熔断防御)
                m1 = Decimal(str(self.get_token_multiplier(token, connector_1)))
                m2 = Decimal(str(self.get_token_multiplier(token, connector_2)))
                p1_norm = p1 / m1
                p2_norm = p2 / m2
                avg_p = (p1_norm + p2_norm) / Decimal("2")
                if avg_p > Decimal("0"):
                    price_divergence = abs(p1_norm - p2_norm) / avg_p
                    max_div = Decimal(str(getattr(self.config, "max_allowed_price_divergence", Decimal("0.03"))))
                    if price_divergence > max_div:
                        self.logger().warning(
                            f"[ASSET-INTEGRITY-REJECT] Token '{token}' cross-exchange normalized price divergence is {float(price_divergence):.2%} "
                            f"({connector_1} raw:${float(p1):.4f}/norm:${float(p1_norm):.6f} vs {connector_2} raw:${float(p2):.4f}/norm:${float(p2_norm):.6f}), "
                            f"exceeding safe limit of {float(max_div):.1%}!\n"
                            f"  Suspected homonym ticker collision (同名不同币), unannounced token split/rebrand, or fake impostor asset. "
                            f"Entry rejected immediately to protect capital!"
                        )
                        self.entry_candidate_ticks[token] = 0
                        continue

                # Defense 1: Entry Hysteresis & Persistence Filter (开仓迟滞滤波 / 连续 N 次 Tick 确认)
                req_ticks = int(getattr(self.config, "entry_persistence_ticks", 3))
                self.entry_candidate_ticks[token] = self.entry_candidate_ticks.get(token, 0) + 1
                if self.entry_candidate_ticks[token] < req_ticks:
                    self.logger().info(
                        f"[ENTRY-HYSTERESIS] Token '{token}' passed criteria ({self.entry_candidate_ticks[token]}/{req_ticks} ticks). "
                        f"Spread={float(current_profitability):+.3%}, Payback={float(payback_hours):.1f}h. Waiting for sustained signal..."
                    )
                    continue

                # Confirmed sustained opportunity! Reset counter and proceed to enter
                self.entry_candidate_ticks[token] = 0

                self.logger().info(
                    f"Best Combination: {connector_1} | {connector_2} | {trade_side} "
                    f"Funding rate profitability: {expected_profitability:.4%} "
                    f"Trading profitability after fees: {current_profitability:.4%} "
                    f"Payback: {float(payback_hours):.1f}h (Limit: {float(max_allowed_payback):.1f}h)"
                )

                if self.config.dry_run:
                    # Open Virtual Position in Sandbox
                    fee_pct_1 = self.connectors[connector_1].get_fee(
                        base_currency=trading_pair_1.split("-")[0],
                        quote_currency=trading_pair_1.split("-")[1],
                        order_type=OrderType.MARKET,
                        order_side=TradeType.BUY,
                        amount=pos_size / p1,
                        price=p1,
                        is_maker=False,
                        position_action=PositionAction.OPEN
                    ).percent
                    fee_pct_2 = self.connectors[connector_2].get_fee(
                        base_currency=trading_pair_2.split("-")[0],
                        quote_currency=trading_pair_2.split("-")[1],
                        order_type=OrderType.MARKET,
                        order_side=TradeType.BUY,
                        amount=pos_size / p2,
                        price=p2,
                        is_maker=False,
                        position_action=PositionAction.OPEN
                    ).percent

                    eff_lev = self.get_effective_leverage(token, pos_size)
                    self.active_funding_arbitrages[token] = {
                        "is_virtual": True,
                        "connector_1": connector_1,
                        "connector_2": connector_2,
                        "side": trade_side,
                        "open_timestamp": self.current_timestamp,
                        "price_1": p1,
                        "price_2": p2,
                        "amount_1": pos_size / p1,
                        "amount_2": pos_size / p2,
                        "position_size_quote": pos_size,
                        "tier": int(self.get_token_tier(token)[0]),
                        "leverage": eff_lev,
                        "entry_fee_quote": (fee_pct_1 + fee_pct_2) * pos_size,
                        "expected_profitability": expected_profitability,
                        "entry_profitability_after_fees": current_profitability,
                        "payback_hours": float(payback_hours),
                        "entry_friction_pct": float(friction_pct),
                        "funding_payments": [],
                        "next_funding_time_1": funding_info_report[connector_1].next_funding_utc_timestamp,
                        "next_funding_time_2": funding_info_report[connector_2].next_funding_utc_timestamp,
                        "executors_ids": ["DRY_RUN_SANDBOX"],
                    }
                    self.entry_candidate_ticks.pop(token, None)
                    self.logger().info(
                        f"[DRY-RUN] Virtual Arbitrage Position Established for {token}!\n"
                        f"  Leg 1 ({connector_1}): {'BUY' if trade_side == TradeType.BUY else 'SELL'} @ {p1:.4f}\n"
                        f"  Leg 2 ({connector_2}): {'SELL' if trade_side == TradeType.BUY else 'BUY'} @ {p2:.4f}\n"
                        f"  Notional Size: ${float(pos_size):,.2f} | Leverage: {eff_lev}x ({'Zero-Leverage Safe' if eff_lev == 1 else 'Capital Boost'})\n"
                        f"  Daily Rate Diff: {expected_profitability:.4%}, Net Entry Spread: {current_profitability:.4%}"
                    )
                else:
                    # Real Live Trading Executors
                    eff_lev = self.get_effective_leverage(token, pos_size)
                    self.logger().info(f"Starting live executors for {token} with dynamic size ${float(pos_size):,.2f} (Leverage: {eff_lev}x)...")
                    position_executor_config_1, position_executor_config_2 = self.get_position_executors_config(
                        token, connector_1, connector_2, trade_side, position_size_quote=pos_size
                    )
                    self.active_funding_arbitrages[token] = {
                        "is_virtual": False,
                        "connector_1": connector_1,
                        "connector_2": connector_2,
                        "executors_ids": [position_executor_config_1.id, position_executor_config_2.id],
                        "side": trade_side,
                        "open_timestamp": self.current_timestamp,
                        "price_1": p1,
                        "price_2": p2,
                        "amount_1": pos_size / p1,
                        "amount_2": pos_size / p2,
                        "position_size_quote": pos_size,
                        "tier": int(self.get_token_tier(token)[0]),
                        "leverage": eff_lev,
                        "expected_profitability": expected_profitability,
                        "entry_profitability_after_fees": current_profitability,
                        "funding_payments": [],
                    }
                    self.entry_candidate_ticks.pop(token, None)

                    return [CreateExecutorAction(executor_config=position_executor_config_1),
                            CreateExecutorAction(executor_config=position_executor_config_2)]
        return create_actions

    def _evaluate_token_quarantine(self, token: str, net_loss: float):
        """
        Evaluate if a closed token should be quarantined due to severe loss or consecutive losses,
        or permanently blacklisted if cumulative historical net loss exceeds threshold.
        """
        hard_blacklist_usd = float(getattr(self.config, "cumulative_loss_hard_blacklist_usd", 30.0))
        loss_thresh = float(getattr(self.config, "single_loss_quarantine_threshold", Decimal("15.0")))
        quarantine_h = float(getattr(self.config, "token_quarantine_hours", Decimal("24.0")))

        past_trades = self.stopped_funding_arbitrages.get(token, [])
        cum_net_pnl = sum(float(tr.get("total_net_pnl", 0)) for tr in past_trades)

        # 1. Module 3: Permanent Hard Blacklist (毒币终生累计亏损熔断)
        if cum_net_pnl <= -hard_blacklist_usd:
            self.token_permanent_blacklist.add(token)
            self.token_quarantine_ledger[token] = {
                "quarantine_until": self.current_timestamp + 86400 * 3650,  # 10 years (permanent)
                "reason": f"Permanent Blacklist: Cumulative Deficit (${cum_net_pnl:.2f} <= -${hard_blacklist_usd:.2f})",
                "consecutive_losses": len(past_trades),
                "triggered_at": self.current_timestamp,
                "loss_amount": cum_net_pnl,
            }
            self.logger().error(
                f"🚫 [HARD-BLACKLIST-TRIGGER] Token '{token}' permanently blacklisted! "
                f"Cumulative historical net deficit: ${cum_net_pnl:.2f} (Threshold: -${hard_blacklist_usd:.2f}). "
                f"Strategy will never enter this token again."
            )
            return

        # 2. Standard Quarantine for severe or consecutive losses
        consecutive_losses = 0
        for tr in reversed(past_trades):
            if float(tr.get("total_net_pnl", 0)) < 0:
                consecutive_losses += 1
            else:
                break

        should_quarantine = False
        q_reason = ""
        if net_loss < -loss_thresh:
            should_quarantine = True
            q_reason = f"Single Heavy Loss (${net_loss:.2f} < -${loss_thresh:.2f})"
        elif consecutive_losses >= 2:
            should_quarantine = True
            q_reason = f"Consecutive Losses ({consecutive_losses} trades in a row)"

        if should_quarantine:
            self.token_quarantine_ledger[token] = {
                "quarantine_until": self.current_timestamp + quarantine_h * 3600,
                "reason": q_reason,
                "consecutive_losses": consecutive_losses,
                "triggered_at": self.current_timestamp,
                "loss_amount": net_loss,
            }
            self.logger().warning(
                f"🚨 [QUARANTINE-TRIGGER] Token '{token}' quarantined for {quarantine_h:.0f}h! "
                f"Reason: {q_reason}."
            )

    def evaluate_liquidation_defense(self, token: str, funding_arbitrage_info: Dict) -> Tuple[bool, Decimal, Decimal]:
        """
        Calculates distance to liquidation for both legs and checks if liquidation buffer threshold is breached.
        """
        try:
            trading_pair_1 = self.get_trading_pair_for_connector(token, funding_arbitrage_info["connector_1"])
            trading_pair_2 = self.get_trading_pair_for_connector(token, funding_arbitrage_info["connector_2"])
            pos_size = Decimal(str(funding_arbitrage_info.get("position_size_quote", self.config.position_size_quote)))
            p1_open = Decimal(str(funding_arbitrage_info.get("price_1", 1)))
            p2_open = Decimal(str(funding_arbitrage_info.get("price_2", 1)))

            p1_curr = self.get_safe_quote_price(
                connector_name=funding_arbitrage_info["connector_1"],
                trading_pair=trading_pair_1,
                quote_volume=pos_size,
                is_buy=funding_arbitrage_info["side"] != TradeType.BUY,
                fallback_price=p1_open,
            )
            p2_curr = self.get_safe_quote_price(
                connector_name=funding_arbitrage_info["connector_2"],
                trading_pair=trading_pair_2,
                quote_volume=pos_size,
                is_buy=funding_arbitrage_info["side"] == TradeType.BUY,
                fallback_price=p2_open,
            )

            lev = Decimal(str(funding_arbitrage_info.get("leverage", self.get_effective_leverage(token, pos_size))))
            mmr = Decimal("0.02")
            is_buy_side = (funding_arbitrage_info["side"] == TradeType.BUY) if isinstance(funding_arbitrage_info["side"], TradeType) else (str(funding_arbitrage_info["side"]) in ["BUY", "TradeType.BUY"])
            if is_buy_side:
                liq_p1 = max(Decimal("0"), p1_open * (Decimal("1") - Decimal("1") / lev + mmr))
                dist1_pct = (p1_curr - liq_p1) / p1_curr if p1_curr > 0 else Decimal("1")
                liq_p2 = p2_open * (Decimal("1") + Decimal("1") / lev - mmr)
                dist2_pct = (liq_p2 - p2_curr) / p2_curr if p2_curr > 0 else Decimal("1")
            else:
                liq_p1 = p1_open * (Decimal("1") + Decimal("1") / lev - mmr)
                dist1_pct = (liq_p1 - p1_curr) / p1_curr if p1_curr > 0 else Decimal("1")
                liq_p2 = max(Decimal("0"), p2_open * (Decimal("1") - Decimal("1") / lev + mmr))
                dist2_pct = (p2_curr - liq_p2) / p2_curr if p2_curr > 0 else Decimal("1")

            min_dist_pct = min(dist1_pct, dist2_pct)
            tier_val, _ = self.get_token_tier(token)
            if tier_val == 1:
                liq_buffer = Decimal(str(getattr(self.config, "tier_1_liquidation_buffer", Decimal("0.04"))))
            elif tier_val == 2:
                liq_buffer = Decimal(str(getattr(self.config, "tier_2_liquidation_buffer", Decimal("0.08"))))
            else:
                liq_buffer = Decimal(str(getattr(self.config, "tier_3_liquidation_buffer", Decimal("0.12"))))

            funding_arbitrage_info["liquidation_buffer_pct"] = float(liq_buffer)
            funding_arbitrage_info["safety_distance_pct"] = float(min_dist_pct)
            return (min_dist_pct <= liq_buffer), min_dist_pct, liq_buffer
        except Exception as e:
            self.logger().debug(f"Failed to evaluate liquidation defense for {token}: {e}")
            return False, Decimal("1.0"), Decimal("0.08")

    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        """
        Monitor active arbitrages for take-profit or stop-loss conditions.
        """
        # First update any virtual funding payments
        if self.config.dry_run:
            self.check_virtual_funding_payments()

        stop_executor_actions = []
        for token, funding_arbitrage_info in list(self.active_funding_arbitrages.items()):
            if funding_arbitrage_info.get("is_virtual"):
                try:
                    # Handle Virtual Position Exit Logic
                    trading_pair_1 = self.get_trading_pair_for_connector(token, funding_arbitrage_info["connector_1"])
                    trading_pair_2 = self.get_trading_pair_for_connector(token, funding_arbitrage_info["connector_2"])

                    p1_open = Decimal(str(funding_arbitrage_info.get("price_1", 1)))
                    p2_open = Decimal(str(funding_arbitrage_info.get("price_2", 1)))
                    pos_size = Decimal(str(funding_arbitrage_info.get("position_size_quote", self.config.position_size_quote)))

                    p1_curr = self.get_safe_quote_price(
                        connector_name=funding_arbitrage_info["connector_1"],
                        trading_pair=trading_pair_1,
                        quote_volume=pos_size,
                        is_buy=funding_arbitrage_info["side"] != TradeType.BUY,
                        fallback_price=p1_open,
                    )
                    p2_curr = self.get_safe_quote_price(
                        connector_name=funding_arbitrage_info["connector_2"],
                        trading_pair=trading_pair_2,
                        quote_volume=pos_size,
                        is_buy=funding_arbitrage_info["side"] == TradeType.BUY,
                        fallback_price=p2_open,
                    )

                    amt1 = Decimal(str(funding_arbitrage_info.get("amount_1", pos_size / p1_open)))
                    amt2 = Decimal(str(funding_arbitrage_info.get("amount_2", pos_size / p2_open)))

                    leg1_pnl = (p1_curr - p1_open) * amt1 if funding_arbitrage_info["side"] == TradeType.BUY else (p1_open - p1_curr) * amt1
                    leg2_pnl = (p2_open - p2_curr) * amt2 if funding_arbitrage_info["side"] == TradeType.BUY else (p2_curr - p2_open) * amt2

                    exit_fee_quote = Decimal(str(funding_arbitrage_info.get("entry_fee_quote", 0)))
                    trading_pnl = leg1_pnl + leg2_pnl - exit_fee_quote
                    funding_pnl = sum(Decimal(str(p.get("amount", 0))) for p in funding_arbitrage_info.get("funding_payments", []))
                    total_net_pnl = trading_pnl + funding_pnl
                    if total_net_pnl.is_nan():
                        total_net_pnl = Decimal("0")
                    total_pnl_pct = total_net_pnl / pos_size if pos_size > 0 else Decimal("0")

                    # v1.3.0 Scaling-out Take Profit (Tranche 1: 50% locked on reaching +1.0% net profit)
                    scaling_tp_enabled = getattr(self.config, "scaling_tp_enabled", True)
                    partial_tp_ratio = Decimal(str(getattr(self.config, "partial_tp_ratio", Decimal("0.50"))))
                    partial_tp_thresh = Decimal(str(getattr(self.config, "partial_tp_threshold_pct", Decimal("0.010"))))
                    partial_tp_target = partial_tp_thresh * pos_size

                    partial_tp_condition = (
                        scaling_tp_enabled
                        and not funding_arbitrage_info.get("partial_tp_executed", False)
                        and (not total_net_pnl.is_nan())
                        and (total_net_pnl >= partial_tp_target)
                    )

                    if partial_tp_condition:
                        locked_trading_pnl = float(trading_pnl * partial_tp_ratio)
                        locked_funding_pnl = float(funding_pnl * partial_tp_ratio)
                        locked_net_pnl = float(total_net_pnl * partial_tp_ratio)

                        partial_record = {
                            "token": token,
                            "connector_1": funding_arbitrage_info.get("connector_1", ""),
                            "connector_2": funding_arbitrage_info.get("connector_2", ""),
                            "close_reason": f"Partial Take Profit ({int(partial_tp_ratio * 100)}% Tranche Locked)",
                            "close_timestamp": int(self.current_timestamp),
                            "open_timestamp": int(funding_arbitrage_info.get("open_timestamp", self.current_timestamp)),
                            "trading_pnl": locked_trading_pnl,
                            "funding_pnl": locked_funding_pnl,
                            "total_net_pnl": locked_net_pnl,
                            "position_size": float(pos_size * partial_tp_ratio),
                            "leverage": int(self.config.leverage),
                            "close_price_1": float(p1_curr),
                            "close_price_2": float(p2_curr),
                            "open_price_1": float(funding_arbitrage_info.get("price_1", 0.0)),
                            "open_price_2": float(funding_arbitrage_info.get("price_2", 0.0)),
                            "duration_seconds": max(0, int(self.current_timestamp - funding_arbitrage_info.get("open_timestamp", self.current_timestamp))),
                        }
                        if token not in self.stopped_funding_arbitrages:
                            self.stopped_funding_arbitrages[token] = []
                        self.stopped_funding_arbitrages[token].append(partial_record)

                        remaining_ratio = Decimal("1") - partial_tp_ratio
                        funding_arbitrage_info["position_size_quote"] = float(pos_size * remaining_ratio)
                        funding_arbitrage_info["amount_1"] = pos_size * remaining_ratio / p1_open if p1_open else Decimal("0")
                        funding_arbitrage_info["amount_2"] = pos_size * remaining_ratio / p2_open if p2_open else Decimal("0")
                        funding_arbitrage_info["partial_tp_executed"] = True
                        funding_arbitrage_info["locked_tp_pnl"] = float(funding_arbitrage_info.get("locked_tp_pnl", 0.0)) + locked_net_pnl
                        funding_arbitrage_info["funding_payments"] = [
                            {"amount": float(p.get("amount", 0) * float(remaining_ratio)), "timestamp": p.get("timestamp", 0)}
                            for p in funding_arbitrage_info.get("funding_payments", [])
                        ]
                        self.logger().info(
                            f"[PARTIAL TAKE PROFIT] Locked {int(partial_tp_ratio * 100)}% profit for {token}!\n"
                            f"  Locked Net PnL: ${locked_net_pnl:+.4f}, Remaining Position Size: ${funding_arbitrage_info['position_size_quote']:,.2f}"
                        )
                        pos_size = Decimal(str(funding_arbitrage_info["position_size_quote"]))

                    take_profit_target = Decimal(str(self.config.profitability_to_take_profit)) * pos_size
                    take_profit_condition = (not total_net_pnl.is_nan()) and (total_net_pnl > take_profit_target)

                    # Module 2: Dynamic Breakeven Trailing Stop (移动保本止损锁: 达标+0.6%激活, 回撤至+0.05%保本平仓)
                    be_activation = Decimal(str(getattr(self.config, "breakeven_trailing_activation_pct", Decimal("0.006"))))
                    be_stop = Decimal(str(getattr(self.config, "breakeven_trailing_stop_pct", Decimal("0.0005"))))
                    if (not total_pnl_pct.is_nan()) and total_pnl_pct >= be_activation and not funding_arbitrage_info.get("breakeven_active", False):
                        funding_arbitrage_info["breakeven_active"] = True
                        self.logger().info(
                            f"🛡️ [BREAKEVEN-ARMED] Token '{token}' net profit reached {float(total_pnl_pct):+.2%} "
                            f"(>= {float(be_activation):+.2%}). Breakeven trailing stop armed at +{float(be_stop):.2%}!"
                        )

                    breakeven_stop_condition = (
                        funding_arbitrage_info.get("breakeven_active", False)
                        and (not total_pnl_pct.is_nan())
                        and (total_pnl_pct <= be_stop)
                    )

                    # Defense 3: Basis Spread Divergence Stop-Loss (基差撕裂快速止损防线)
                    trading_pnl_pct = trading_pnl / pos_size if pos_size > 0 else Decimal("0")
                    max_basis_loss = Decimal(str(getattr(self.config, "max_basis_loss_pct", Decimal("0.005"))))
                    basis_divergence_condition = (not trading_pnl_pct.is_nan()) and (trading_pnl_pct <= -max_basis_loss)

                    # Anti-Whipsaw Rate Inversion Filter (防假止损迟滞滤波)
                    funding_info_report = self.get_funding_info_by_token(token)
                    rate1 = self.get_normalized_funding_rate_in_seconds(funding_info_report, funding_arbitrage_info["connector_1"])
                    rate2 = self.get_normalized_funding_rate_in_seconds(funding_info_report, funding_arbitrage_info["connector_2"])
                    if funding_arbitrage_info["side"] == TradeType.BUY:
                        funding_rate_diff = rate2 - rate1
                    else:
                        funding_rate_diff = rate1 - rate2

                    diff_val = funding_rate_diff * self.funding_profitability_interval
                    stop_loss_target = Decimal(str(self.config.funding_rate_diff_stop_loss))
                    raw_funding_inversion = (not diff_val.is_nan()) and (diff_val < stop_loss_target)

                    required_ticks = int(getattr(self.config, "rate_inversion_confirmation_ticks", 3))
                    if raw_funding_inversion:
                        self.inversion_tick_counter[token] = self.inversion_tick_counter.get(token, 0) + 1
                    else:
                        self.inversion_tick_counter[token] = 0

                    current_funding_condition = (self.inversion_tick_counter.get(token, 0) >= required_ticks)

                    # Liquidation Defense & Rotation checks
                    liq_defense_condition, min_dist_pct, liq_buffer = self.evaluate_liquidation_defense(token, funding_arbitrage_info)
                    token_removed_condition = token not in self.config.tokens

                    if take_profit_condition or breakeven_stop_condition or basis_divergence_condition or current_funding_condition or liq_defense_condition or token_removed_condition:
                        if token_removed_condition:
                            reason = "Manual Rotation: Low Yield Ditch"
                        elif liq_defense_condition:
                            reason = f"Liquidation Defense (Safety Margin {float(min_dist_pct):.1%} <= {float(liq_buffer):.1%})"
                        elif basis_divergence_condition:
                            reason = f"Basis Spread Divergence Stop Loss (Trading PnL {float(trading_pnl_pct):.2%} <= -{float(max_basis_loss):.2%})"
                        elif breakeven_stop_condition:
                            reason = f"Breakeven Trailing Stop (Retracement {float(total_pnl_pct):+.2%} <= +{float(be_stop):.2%})"
                        elif take_profit_condition:
                            reason = "Take Profit (Final)" if funding_arbitrage_info.get("partial_tp_executed", False) else "Take Profit"
                        elif current_funding_condition:
                            reason = "Rate Inversion Stop Loss (费率持续倒挂止损)"
                        else:
                            reason = "Stop Loss"

                        self.logger().info(
                            f"[DRY-RUN] Virtual Arbitrage Closed ({reason}) for {token}!\n"
                            f"  Trading PnL: {trading_pnl:+.4f}, Funding PnL: {funding_pnl:+.4f}, Total Net PnL: {total_net_pnl:+.4f} ({total_pnl_pct:+.2%})"
                        )
                        funding_arbitrage_info["close_timestamp"] = self.current_timestamp
                        funding_arbitrage_info["trading_pnl"] = float(trading_pnl)
                        funding_arbitrage_info["funding_pnl"] = float(funding_pnl)
                        funding_arbitrage_info["total_net_pnl"] = float(total_net_pnl)
                        funding_arbitrage_info["close_reason"] = reason
                        funding_arbitrage_info["close_price_1"] = float(p1_curr)
                        funding_arbitrage_info["close_price_2"] = float(p2_curr)
                        funding_arbitrage_info["leg1_pnl"] = float(leg1_pnl)
                        funding_arbitrage_info["leg2_pnl"] = float(leg2_pnl)
                        funding_arbitrage_info["duration_seconds"] = max(0, int(self.current_timestamp - funding_arbitrage_info.get("open_timestamp", self.current_timestamp)))
                        if token not in self.stopped_funding_arbitrages:
                            self.stopped_funding_arbitrages[token] = []
                        self.stopped_funding_arbitrages[token].append(funding_arbitrage_info)

                        # Cooldown & Quarantine Ledger
                        self.token_last_closed_timestamp[token] = self.current_timestamp
                        self.inversion_tick_counter[token] = 0
                        self._evaluate_token_quarantine(token, float(total_net_pnl))

                        # Module 1: Post-Take-Profit Anti-Churning Lockout (止盈防高位追高锁定)
                        if "Take Profit" in reason:
                            cooldown_h = float(getattr(self.config, "post_tp_cooldown_hours", 4.0))
                            self.token_post_tp_lockout[token] = self.current_timestamp + cooldown_h * 3600.0
                            self.logger().info(
                                f"🔒 [POST-TP-LOCKOUT] Token '{token}' took profit! Armed {cooldown_h:.1f}h anti-churning lockout until {datetime.fromtimestamp(self.token_post_tp_lockout[token], timezone.utc).strftime('%H:%M:%S UTC')}."
                            )

                        del self.active_funding_arbitrages[token]
                except Exception as e:
                    self.logger().warning(f"Error evaluating virtual stop condition for {token}: {e}")
            else:
                # Live Trading Executors Stop Logic
                try:
                    executors = self.filter_executors(
                        executors=self.get_all_executors(),
                        filter_func=lambda x: x.id in funding_arbitrage_info["executors_ids"]
                    )
                    funding_payments_pnl = sum(
                        (p.get("amount", 0) if isinstance(p, dict) else getattr(p, "amount", 0))
                        for p in funding_arbitrage_info.get("funding_payments", [])
                    )
                    executors_pnl = sum(executor.net_pnl_quote for executor in executors)
                    pos_size = Decimal(str(funding_arbitrage_info.get("position_size_quote", self.config.position_size_quote)))
                    total_net_pnl = Decimal(str(executors_pnl)) + Decimal(str(funding_payments_pnl))
                    total_pnl_pct = total_net_pnl / pos_size if pos_size > 0 else Decimal("0")

                    take_profit_condition = total_net_pnl > Decimal(str(self.config.profitability_to_take_profit)) * pos_size

                    # Module 2: Dynamic Breakeven Trailing Stop (移动保本止损锁: 达标+0.6%激活, 回撤至+0.05%保本平仓)
                    be_activation = Decimal(str(getattr(self.config, "breakeven_trailing_activation_pct", Decimal("0.006"))))
                    be_stop = Decimal(str(getattr(self.config, "breakeven_trailing_stop_pct", Decimal("0.0005"))))
                    if (not total_pnl_pct.is_nan()) and total_pnl_pct >= be_activation and not funding_arbitrage_info.get("breakeven_active", False):
                        funding_arbitrage_info["breakeven_active"] = True
                        self.logger().info(
                            f"🛡️ [BREAKEVEN-ARMED] Token '{token}' net profit reached {float(total_pnl_pct):+.2%} "
                            f"(>= {float(be_activation):+.2%}). Breakeven trailing stop armed at +{float(be_stop):.2%}!"
                        )

                    breakeven_stop_condition = (
                        funding_arbitrage_info.get("breakeven_active", False)
                        and (not total_pnl_pct.is_nan())
                        and (total_pnl_pct <= be_stop)
                    )

                    # Defense 3: Basis Spread Divergence Stop-Loss (基差撕裂快速止损防线)
                    trading_pnl_pct = Decimal(str(executors_pnl)) / pos_size if pos_size > 0 else Decimal("0")
                    max_basis_loss = Decimal(str(getattr(self.config, "max_basis_loss_pct", Decimal("0.005"))))
                    basis_divergence_condition = (not trading_pnl_pct.is_nan()) and (trading_pnl_pct <= -max_basis_loss)

                    # Anti-Whipsaw Rate Inversion Filter
                    funding_info_report = self.get_funding_info_by_token(token)
                    rate1 = self.get_normalized_funding_rate_in_seconds(funding_info_report, funding_arbitrage_info["connector_1"])
                    rate2 = self.get_normalized_funding_rate_in_seconds(funding_info_report, funding_arbitrage_info["connector_2"])
                    if funding_arbitrage_info["side"] == TradeType.BUY:
                        funding_rate_diff = rate2 - rate1
                    else:
                        funding_rate_diff = rate1 - rate2

                    diff_val = funding_rate_diff * self.funding_profitability_interval
                    stop_loss_target = Decimal(str(self.config.funding_rate_diff_stop_loss))
                    raw_funding_inversion = (not diff_val.is_nan()) and (diff_val < stop_loss_target)

                    required_ticks = int(getattr(self.config, "rate_inversion_confirmation_ticks", 3))
                    if raw_funding_inversion:
                        self.inversion_tick_counter[token] = self.inversion_tick_counter.get(token, 0) + 1
                    else:
                        self.inversion_tick_counter[token] = 0

                    current_funding_condition = (self.inversion_tick_counter.get(token, 0) >= required_ticks)

                    # Liquidation Defense & Rotation checks
                    liq_defense_condition, min_dist_pct, liq_buffer = self.evaluate_liquidation_defense(token, funding_arbitrage_info)
                    token_removed_condition = token not in self.config.tokens

                    if take_profit_condition or breakeven_stop_condition or basis_divergence_condition or current_funding_condition or liq_defense_condition or token_removed_condition:
                        if token_removed_condition:
                            reason = "Manual Rotation: Low Yield Ditch"
                        elif liq_defense_condition:
                            reason = f"Liquidation Defense (Safety Margin {float(min_dist_pct):.1%} <= {float(liq_buffer):.1%})"
                        elif basis_divergence_condition:
                            reason = f"Basis Spread Divergence Stop Loss (Trading PnL {float(trading_pnl_pct):.2%} <= -{float(max_basis_loss):.2%})"
                        elif breakeven_stop_condition:
                            reason = f"Breakeven Trailing Stop (Retracement {float(total_pnl_pct):+.2%} <= +{float(be_stop):.2%})"
                        elif take_profit_condition:
                            reason = "Take Profit (Final)" if funding_arbitrage_info.get("partial_tp_executed", False) else "Take Profit"
                        elif current_funding_condition:
                            reason = "Rate Inversion Stop Loss (费率持续倒挂止损)"
                        else:
                            reason = "Stop Loss"

                        self.logger().info(f"[LIVE] Arbitrage Closed ({reason}) for {token}! Stopping executors.")
                        funding_arbitrage_info["close_timestamp"] = self.current_timestamp
                        funding_arbitrage_info["trading_pnl"] = float(executors_pnl)
                        funding_arbitrage_info["funding_pnl"] = float(funding_payments_pnl)
                        funding_arbitrage_info["total_net_pnl"] = float(total_net_pnl)
                        funding_arbitrage_info["close_reason"] = reason
                        funding_arbitrage_info["duration_seconds"] = max(0, int(self.current_timestamp - funding_arbitrage_info.get("open_timestamp", self.current_timestamp)))

                        if token not in self.stopped_funding_arbitrages:
                            self.stopped_funding_arbitrages[token] = []
                        self.stopped_funding_arbitrages[token].append(funding_arbitrage_info)

                        # Cooldown & Quarantine Ledger
                        self.token_last_closed_timestamp[token] = self.current_timestamp
                        self.inversion_tick_counter[token] = 0
                        self._evaluate_token_quarantine(token, float(total_net_pnl))

                        # Module 1: Post-Take-Profit Anti-Churning Lockout (止盈防高位追高锁定)
                        if "Take Profit" in reason:
                            cooldown_h = float(getattr(self.config, "post_tp_cooldown_hours", 4.0))
                            self.token_post_tp_lockout[token] = self.current_timestamp + cooldown_h * 3600.0
                            self.logger().info(
                                f"🔒 [POST-TP-LOCKOUT] Token '{token}' took profit! Armed {cooldown_h:.1f}h anti-churning lockout until {datetime.fromtimestamp(self.token_post_tp_lockout[token], timezone.utc).strftime('%H:%M:%S UTC')}."
                            )

                        del self.active_funding_arbitrages[token]
                        stop_executor_actions.extend([StopExecutorAction(executor_id=executor.id) for executor in executors])
                except Exception as e:
                    self.logger().warning(f"Error evaluating live stop condition for {token}: {e}")
        return stop_executor_actions

    def did_complete_funding_payment(self, funding_payment_completed_event: FundingPaymentCompletedEvent):
        """
        Based on the funding payment event received (in live mode), check if one of the active arbitrages matches.
        """
        raw_token = funding_payment_completed_event.trading_pair.split("-")[0]
        token = self.get_canonical_token(raw_token)
        if token in self.active_funding_arbitrages:
            self.active_funding_arbitrages[token]["funding_payments"].append(funding_payment_completed_event)

    def get_position_executors_config(self, token, connector_1, connector_2, trade_side, position_size_quote=None):
        size = Decimal(str(position_size_quote)) if position_size_quote is not None else self.config.position_size_quote
        price_1 = self.market_data_provider.get_price_by_type(
            connector_name=connector_1,
            trading_pair=self.get_trading_pair_for_connector(token, connector_1),
            price_type=PriceType.MidPrice
        )
        amount_1 = size / price_1

        price_2 = self.market_data_provider.get_price_by_type(
            connector_name=connector_2,
            trading_pair=self.get_trading_pair_for_connector(token, connector_2),
            price_type=PriceType.MidPrice
        )
        amount_2 = size / price_2

        eff_lev = self.get_effective_leverage(token, size)
        position_executor_config_1 = PositionExecutorConfig(
            timestamp=self.current_timestamp,
            connector_name=connector_1,
            trading_pair=self.get_trading_pair_for_connector(token, connector_1),
            side=trade_side,
            amount=amount_1,
            leverage=eff_lev,
            triple_barrier_config=TripleBarrierConfig(open_order_type=OrderType.MARKET),
        )
        position_executor_config_2 = PositionExecutorConfig(
            timestamp=self.current_timestamp,
            connector_name=connector_2,
            trading_pair=self.get_trading_pair_for_connector(token, connector_2),
            side=TradeType.BUY if trade_side == TradeType.SELL else TradeType.SELL,
            amount=amount_2,
            leverage=eff_lev,
            triple_barrier_config=TripleBarrierConfig(open_order_type=OrderType.MARKET),
        )

        return position_executor_config_1, position_executor_config_2

    def format_status(self) -> str:
        original_status = super().format_status()
        funding_rate_status = []
        mode_str = "DRY-RUN SIMULATION (Paper Trading - No Real Orders)" if self.config.dry_run else "LIVE TRADING (Real Orders Active)"
        funding_rate_status.append(f"\n==================== [ {mode_str} ] ====================")

        if self.ready_to_trade:
            all_funding_info = []
            all_best_paths = []
            for token in self.config.tokens:
                token_info = {"token": token}
                best_paths_info = {"token": token}
                funding_info_report = self.get_funding_info_by_token(token)
                best_combination = self.get_most_profitable_combination(funding_info_report)
                for connector_name, info in funding_info_report.items():
                    token_info[f"{connector_name} Rate (%)"] = self.get_normalized_funding_rate_in_seconds(funding_info_report, connector_name) * self.funding_profitability_interval * 100
                if best_combination:
                    connector_1, connector_2, side, funding_rate_diff = best_combination
                    profitability_after_fees = self.get_current_profitability_after_fees(token, connector_1, connector_2, side)
                    best_paths_info["Best Path"] = f"{connector_1}_{connector_2}"
                    best_paths_info["Rate Diff (%)"] = funding_rate_diff * 100
                    best_paths_info["Trade Net Spread (%)"] = profitability_after_fees * 100
                    best_paths_info["Days Trade Prof"] = (- profitability_after_fees / funding_rate_diff) if funding_rate_diff > 0 else 0
                    best_paths_info["Days to TP"] = ((self.config.profitability_to_take_profit - profitability_after_fees) / funding_rate_diff) if funding_rate_diff > 0 else 0

                    time_to_c1 = max(0, funding_info_report[connector_1].next_funding_utc_timestamp - self.current_timestamp)
                    time_to_c2 = max(0, funding_info_report[connector_2].next_funding_utc_timestamp - self.current_timestamp)
                    best_paths_info["Min to F1"] = time_to_c1 / 60
                    best_paths_info["Min to F2"] = time_to_c2 / 60

                all_funding_info.append(token_info)
                all_best_paths.append(best_paths_info)

            funding_rate_status.append(f"Min Funding Profitability: {self.config.min_funding_rate_profitability:.2%}")
            funding_rate_status.append(f"Take Profit Target: {self.config.profitability_to_take_profit:.2%}")
            funding_rate_status.append("Funding Rate Comparison (Daily Equivalent):")
            funding_rate_status.append(format_df_for_printout(df=pd.DataFrame(all_funding_info), table_format="psql"))
            funding_rate_status.append(format_df_for_printout(df=pd.DataFrame(all_best_paths), table_format="psql"))

            # Active Arbitrages status
            if self.active_funding_arbitrages:
                funding_rate_status.append("\n--- Active Arbitrage Positions ---")
                for token, arb in self.active_funding_arbitrages.items():
                    long_c = arb["connector_1"] if arb["side"] == TradeType.BUY else arb["connector_2"]
                    short_c = arb["connector_2"] if arb["side"] == TradeType.BUY else arb["connector_1"]
                    funding_rate_status.append(f"Token: {token} | Long: {long_c} | Short: {short_c}")
                    if arb.get("is_virtual"):
                        funding_pnl = sum(p["amount"] for p in arb.get("funding_payments", []))
                        elapsed_hours = (self.current_timestamp - arb.get("open_timestamp", self.current_timestamp)) / 3600
                        funding_rate_status.append(
                            f"  [Sandbox] Open Entry Prices: {arb['connector_1']}={arb.get('price_1', 0):.4f}, "
                            f"{arb['connector_2']}={arb.get('price_2', 0):.4f} | Size: {arb.get('position_size_quote')} Quote"
                        )
                        funding_rate_status.append(
                            f"  [Sandbox] Duration: {elapsed_hours:.1f}h | Collected Funding Income: {funding_pnl:+.4f} Quote "
                            f"({len(arb.get('funding_payments', []))} settlements)"
                        )
                    else:
                        funding_rate_status.append(f"  Funding Payments: {len(arb.get('funding_payments', []))}")
                        funding_rate_status.append(f"  Executors: {arb.get('executors_ids')}")
                    funding_rate_status.append("-" * 50)
            else:
                funding_rate_status.append("\nNo active arbitrage positions currently held.")

            # History of closed positions
            closed_count = sum(len(positions) for positions in self.stopped_funding_arbitrages.values())
            if closed_count > 0:
                funding_rate_status.append(f"\n--- Closed Arbitrage History ({closed_count} closed) ---")
                for token, pos_list in self.stopped_funding_arbitrages.items():
                    for pos in pos_list:
                        if pos.get("is_virtual"):
                            funding_rate_status.append(
                                f"Token: {token} | Reason: {pos.get('close_reason')} | "
                                f"Trading PnL: {pos.get('trading_pnl', 0):+.4f} | "
                                f"Funding PnL: {pos.get('funding_pnl', 0):+.4f} | "
                                f"Total Net: {pos.get('total_net_pnl', 0):+.4f}"
                            )

            # Quarantined assets status
            active_quarantined = {
                tok: q for tok, q in self.token_quarantine_ledger.items()
                if q.get("quarantine_until", 0) > self.current_timestamp
            }
            if active_quarantined:
                funding_rate_status.append(f"\n--- 🛡️ Quarantined Toxic Assets ({len(active_quarantined)} active) ---")
                for tok, q in active_quarantined.items():
                    rem_min = (q["quarantine_until"] - self.current_timestamp) / 60
                    funding_rate_status.append(
                        f"  🚫 {tok}: Remaining {rem_min:.1f}m | Reason: {q.get('reason')} | Losses: {q.get('consecutive_losses', 1)}"
                    )

        return original_status + "\n" + "\n".join(funding_rate_status)
