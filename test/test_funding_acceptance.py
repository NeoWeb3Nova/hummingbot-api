import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from funding_paper_acceptance import inspect_ledger


def sample():
    return dict(is_dry_run=True,schema_version=2,updated_at=100,
        watchlist=['BTC'],market_comparison=[{'token':'BTC'}],
        audit=dict(valid_valuation=True,mqtt_delivery='published',public_connectors={
            c:dict(public_only=True,configured_pairs=1,books_ready=['BTC'],rules_ready=['BTC'])
            for c in ['binance_perpetual','hyperliquid_perpetual']}),
        paper_account=dict(initial_balance=10000,equity=10005),
        performance=dict(global_pnl_quote=5),active_positions=[],closed_trades=[
            dict(position_id='1',total_net_pnl=5,trading_pnl=-2,funding_pnl=7,funding_pending=True,
                 funding_payments=[dict(amount='7',amount_estimated=True)])])


class AcceptanceTests(unittest.TestCase):
    def test_positive_profit_cannot_hide_accounting_mismatch(self):
        data=sample();data['paper_account']['equity']=10004
        result=inspect_ledger(data,101)
        self.assertFalse(result['healthy'])
        self.assertIn('account/global/position PnL do not reconcile',result['errors'])
        self.assertEqual(result['pending_funding_records'],1)
        self.assertEqual(result['estimated_funding_events'],1)

    def test_missing_public_rules_do_not_pass_as_healthy(self):
        data=sample();data['audit']['public_connectors']['binance_perpetual']['rules_ready']=[]
        result=inspect_ledger(data,101)
        self.assertFalse(result['healthy'])
        self.assertTrue(any('incomplete fresh books/rules' in x for x in result['errors']))

    def test_duplicate_settlement_is_rejected_even_when_totals_balance(self):
        data=sample();row=data['closed_trades'][0]
        row['funding_payments']=[dict(event_id='BN:1',amount='7'),dict(event_id='BN:1',amount='7')]
        row['funding_pnl']=14;row['total_net_pnl']=12
        data['performance']['global_pnl_quote']=12;data['paper_account']['equity']=10012
        result=inspect_ledger(data,101)
        self.assertFalse(result['healthy'])
        self.assertIn('duplicate funding event within a position/tranche',result['errors'])

    def test_missing_ledger_records_gap_instead_of_zero_profit(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);script=Path(__file__).resolve().parents[1]/'tools/funding_paper_acceptance.py'
            result=subprocess.run([sys.executable,str(script),'--ledger',str(root/'missing.json'),
                '--output',str(root/'report'),'--hours','.00001','--interval','1'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads((root/'report/acceptance.json').read_text())
            self.assertEqual(report['engineering'],'needs_investigation')
            self.assertEqual(report['profitability'],'data_unavailable')
            self.assertNotIn('net_pnl',report['latest'])


if __name__=='__main__':
    unittest.main()
