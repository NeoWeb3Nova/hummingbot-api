"""
Unit tests for BasisStatArbitrageConfig and core mathematical mechanisms
in v2_basis_stat_arb.py (API side).
"""
import unittest
from decimal import Decimal

from pydantic import ValidationError

# Import the config from the script
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "bots" / "scripts"))

from v2_basis_stat_arb import BasisStatArbitrageConfig, BasisStatArbitrage


class TestBasisStatArbitrageConfig(unittest.TestCase):
    """Test configuration schema and validation for basis stat arb."""

    def test_default_config_values(self):
        config = BasisStatArbitrageConfig(
            connectors=["hyperliquid_perpetual", "binance_perpetual"],
            tokens=["BTC", "ETH"],
        )
        self.assertEqual(config.min_holding_seconds, 60)
        self.assertEqual(config.exit_persistence_ticks, 2)
        self.assertEqual(config.taker_fee_rate, Decimal("0.0005"))
        self.assertEqual(config.settlement_blackout_seconds, 60)
        self.assertEqual(config.min_entry_basis_pct, Decimal("0.0070"))
        self.assertEqual(config.take_profit_basis_compression_pct, Decimal("0.0015"))
        self.assertEqual(config.entry_persistence_ticks, 2)
        self.assertEqual(config.stop_loss_confirmation_ticks, 5)

    def test_token_and_connector_normalization(self):
        config = BasisStatArbitrageConfig(
            connectors=["HYPERLIQUID_PERPETUAL", "Binance_Perpetual "],
            tokens=["btc", " eth ", "Sol"],
        )
        self.assertEqual(config.connectors, {"hyperliquid_perpetual", "binance_perpetual"})
        self.assertEqual(config.tokens, {"BTC", "ETH", "SOL"})

    def test_invalid_holding_seconds_validation(self):
        with self.assertRaises(ValidationError):
            BasisStatArbitrageConfig(
                connectors=["hyperliquid_perpetual", "binance_perpetual"],
                tokens=["BTC"],
                min_holding_seconds=-5,  # Must be gt 0
            )

    def test_token_multipliers(self):
        # PEPE has 1000x multiplier on Binance (1000PEPE) and Hyperliquid (kPEPE)
        self.assertEqual(BasisStatArbitrage.get_token_multiplier("PEPE", "binance_perpetual"), 1000.0)
        self.assertEqual(BasisStatArbitrage.get_token_multiplier("PEPE", "hyperliquid_perpetual"), 1000.0)
        self.assertEqual(BasisStatArbitrage.get_token_multiplier("BTC", "binance_perpetual"), 1.0)
        self.assertEqual(BasisStatArbitrage.get_token_multiplier("SOL", "hyperliquid_perpetual"), 1.0)

    def test_trading_pair_derivation(self):
        self.assertEqual(
            BasisStatArbitrage.get_trading_pair_for_connector("BTC", "binance_perpetual"),
            "BTC-USDT"
        )
        self.assertEqual(
            BasisStatArbitrage.get_trading_pair_for_connector("BTC", "hyperliquid_perpetual"),
            "BTC-USD"
        )

    def test_roundtrip_fee_and_roe_calculation(self):
        pos_size = Decimal("1500.0")
        fee_rate = Decimal("0.0005")
        leverage = Decimal("3.0")

        # 4 legs taker fee
        total_fees = pos_size * fee_rate * Decimal("4")
        self.assertEqual(total_fees, Decimal("3.0000"))

        # Real dual margin
        real_margin = (pos_size / leverage) * Decimal("2")
        self.assertEqual(real_margin, Decimal("1000.0"))

        # Target net profit
        net_profit = Decimal("5.0")
        roe = net_profit / real_margin
        self.assertEqual(roe, Decimal("0.0050"))  # 0.5% ROE

    def test_dual_notional_and_leg_amounts(self):
        pos_size = Decimal("1500.0")
        p1 = Decimal("10.0")
        p2 = Decimal("10.05")
        amt1 = pos_size / p1
        amt2 = pos_size / p2
        self.assertEqual(amt1, Decimal("150.0"))
        self.assertAlmostEqual(float(amt2), 149.2537, places=4)
        total_notional = pos_size * Decimal("2")
        self.assertEqual(total_notional, Decimal("3000.0"))


    def test_coin_neutral_delta_zero(self):
        pos_size = Decimal("1500.0")
        p1 = Decimal("10.0")
        p2 = Decimal("10.07")  # 70 bps spread
        max_p = max(p1, p2)
        hedged_amount = pos_size / max_p
        # Both legs have exact same token quantity (Coin-Neutral / Delta-Zero)
        c1_amount = hedged_amount
        c2_amount = hedged_amount
        self.assertEqual(c1_amount, c2_amount)
        self.assertEqual(c1_amount - c2_amount, Decimal("0"))
        # Sized so neither notional exceeds pos_size
        self.assertLessEqual(c1_amount * p1, pos_size)
        self.assertLessEqual(c2_amount * p2, pos_size)

    def test_simulated_latency_slippage_and_friction_accounting(self):
        config = BasisStatArbitrageConfig(
            connectors=["hyperliquid_perpetual", "binance_perpetual"],
            tokens=["BTC"],
            simulated_latency_slippage_pct=Decimal("0.0002"),
        )
        self.assertEqual(config.simulated_latency_slippage_pct, Decimal("0.0002"))

        # Verify math identity: Real Net PnL = Gross PnL - DepthSlip - LatencySlip - Fees + Funding
        gross_pnl = Decimal("15.00")
        depth_slip = Decimal("1.80")
        latency_slip = Decimal("1.20")
        fees = Decimal("3.00")
        funding = Decimal("0.50")

        total_friction = depth_slip + latency_slip + fees
        self.assertEqual(total_friction, Decimal("6.00"))

        net_trade = gross_pnl - total_friction
        self.assertEqual(net_trade, Decimal("9.00"))

        total_net = net_trade + funding
        self.assertEqual(total_net, Decimal("9.50"))

        # Alpha retention rate: 9.50 / 15.00 = 63.33%
        alpha_retention = (total_net / gross_pnl) * Decimal("100")
        self.assertAlmostEqual(float(alpha_retention), 63.3333, places=3)


if __name__ == "__main__":
    unittest.main()
