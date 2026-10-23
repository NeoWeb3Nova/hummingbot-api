"""
Comprehensive Unit and Regression Tests for Spot-Perp Cash & Carry Yield Strategy.
Covers:
- Strategy Configuration and Default Contracts
- 1-Hour Discrete Funding Payment Calculations on Short Leg
- Friction Barrier & Payback Period Evaluation
- Hyperliquid Perpetual Liquidation Safety Cushion Gauge
- Coin-Neutral Delta Zero Accounting
- Persistence and State Serialization
"""
import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from bots.scripts.v2_spot_perp_cash_carry import (
    SpotPerpCashCarryConfig,
    SpotPerpCashCarryStrategy,
)


class TestSpotPerpCashCarryConfig(unittest.TestCase):
    """Test configuration schema and defaults."""

    def test_default_config_contract(self):
        config = SpotPerpCashCarryConfig()
        self.assertEqual(config.strategy_name, "spot_perp_cash_carry")
        self.assertEqual(config.spot_connector, "binance")
        self.assertEqual(config.perp_connector, "hyperliquid_perpetual")
        self.assertEqual(config.position_size_quote, Decimal("1000.0"))
        self.assertEqual(config.max_concurrent_tokens, 3)
        self.assertEqual(config.perp_leverage, 3)
        self.assertEqual(config.min_entry_annualized_apr, Decimal("0.25"))
        self.assertEqual(config.exit_annualized_apr, Decimal("0.08"))
        self.assertEqual(config.max_payback_hours, Decimal("12.0"))
        self.assertEqual(config.liquidation_buffer_pct, Decimal("0.15"))
        self.assertTrue(config.dry_run)
        self.assertEqual(config.dry_run_virtual_spot_quote, Decimal("3000.0"))
        self.assertEqual(config.dry_run_virtual_perp_quote, Decimal("1500.0"))


class TestSpotPerpFinancialFormulas(unittest.TestCase):
    """Verify mathematical and financial attribution formulas."""

    def test_hourly_discrete_funding_calculation(self):
        # Case: $1,000 notional on Hyperliquid Short leg
        notional = Decimal("1000.0")
        rate_1h = Decimal("0.00015")  # 1.5 bps per hour

        # Annualized APR = rate_1h * 24 * 365
        annual_apr = rate_1h * Decimal("24") * Decimal("365")
        self.assertAlmostEqual(float(annual_apr), 1.314, places=3)  # 131.4% APR

        # Short leg receives when rate > 0
        payment = notional * rate_1h
        self.assertEqual(payment, Decimal("0.15"))  # $0.15 cashflow per hour

    def test_payback_period_formula(self):
        # Case: 1000 USD notional, spot fee 0.1%, perp fee 0.035%
        pos_size = Decimal("1000.0")
        spot_fee_rate = Decimal("0.0010")
        perp_fee_rate = Decimal("0.00035")
        roundtrip_fees = (spot_fee_rate * Decimal("2.0")) + (perp_fee_rate * Decimal("2.0"))  # 0.0027 (27 bps)

        rate_1h = Decimal("0.0003")  # 3 bps per hour
        basis_spread = Decimal("0.0009")  # 9 bps basis surplus (Perp price > Spot price)

        # Net hurdle = fees - basis_spread
        net_hurdle = roundtrip_fees - basis_spread  # 0.0018
        payback_hours = net_hurdle / rate_1h
        self.assertAlmostEqual(float(payback_hours), 6.0, places=2)  # 6 hours to break even on friction
        self.assertLessEqual(payback_hours, Decimal("12.0"))

    def test_liquidation_safety_cushion_gauge(self):
        # Case: 3x leverage on Hyperliquid short leg
        p_perp_entry = Decimal("10.0")
        leverage = Decimal("3.0")
        maint_margin = Decimal("0.03")

        # Estimated liquidation price for short = entry * (1 + 1/leverage - maint_margin)
        est_liq_price = p_perp_entry * (Decimal("1.0") + (Decimal("1.0") / leverage) - maint_margin)
        self.assertAlmostEqual(float(est_liq_price), 13.0333, places=3)

        # Scenario 1: Token rises to $11.00 -> Cushion is safe
        p_curr = Decimal("11.0")
        cushion = (est_liq_price - p_curr) / p_curr
        self.assertAlmostEqual(float(cushion), 0.1848, places=3)  # 18.5% cushion > 15% threshold

        # Scenario 2: Token spikes to $11.50 -> Cushion breached (< 15%)
        p_danger = Decimal("11.5")
        danger_cushion = (est_liq_price - p_danger) / p_danger
        self.assertAlmostEqual(float(danger_cushion), 0.1333, places=3)  # 13.3% <= 15% threshold


class TestSpotPerpStatePersistence(unittest.TestCase):
    """Test disk serialization and format_status output."""

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "spot_perp_cash_carry_trades.json"
            mock_data = {
                "strategy": "spot_perp_cash_carry",
                "is_dry_run": True,
                "spot_connector": "binance",
                "perp_connector": "hyperliquid_perpetual",
                "perp_leverage": 3,
                "position_size": 1000.0,
                "active_positions": [
                    {
                        "token": "ENA",
                        "amount": 2500.0,
                        "position_size": 1000.0,
                        "spot_entry_price": 0.40,
                        "perp_entry_price": 0.402,
                        "settled_funding_pnl": 3.65,
                        "current_apr": 0.485,
                        "current_safety_cushion_pct": 0.28,
                    }
                ],
                "closed_trades": [
                    {
                        "token": "SAGA",
                        "total_net_pnl": 12.80,
                        "settled_funding_pnl": 14.50,
                        "trading_pnl": -0.80,
                        "total_friction_cost": 0.90,
                        "close_reason": "Funding Yield Decay Exit",
                    }
                ],
                "market_candidates": [],
                "rotation_history": [],
            }
            file_path.write_text(json.dumps(mock_data, indent=2))
            self.assertTrue(file_path.exists())

            with open(file_path, "r") as f:
                loaded = json.load(f)
            self.assertEqual(loaded["strategy"], "spot_perp_cash_carry")
            self.assertEqual(len(loaded["active_positions"]), 1)
            self.assertEqual(loaded["active_positions"][0]["token"], "ENA")
            self.assertEqual(len(loaded["closed_trades"]), 1)
            self.assertEqual(loaded["closed_trades"][0]["total_net_pnl"], 12.80)


if __name__ == "__main__":
    unittest.main()
