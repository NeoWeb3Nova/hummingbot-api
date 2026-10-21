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


if __name__ == "__main__":
    unittest.main()
