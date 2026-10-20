"""Bounded local sampler for an existing paper bot; creates no server/orders.

Keeps an append-only sample history and atomically updates acceptance.json.
The bot continues running after this sampler finishes. Unavailable observations
are recorded as gaps, never converted to zero-profit or healthy observations.
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


def number(value):
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError("non-finite financial value")
    return value


def inspect_ledger(data, now):
    errors = []
    if data.get("is_dry_run") is not True or data.get("schema_version") != 2:
        errors.append("not the audited paper schema")
    if not 0 <= now - float(data["updated_at"]) <= 20:
        errors.append("ledger stale or clock shifted")
    audit = data.get("audit", {})
    if audit.get("blocked_reason"):
        errors.append(audit["blocked_reason"])
    if audit.get("valid_valuation") is not True:
        errors.append("position valuation unavailable")
    if audit.get("mqtt_delivery") != "published":
        errors.append("MQTT publication unavailable")
    connectors = audit.get("public_connectors", {})
    if len(connectors) != 2:
        errors.append("public connector health missing")
    for name, state in connectors.items():
        count = int(state["configured_pairs"])
        if not state.get("public_only"):
            errors.append(f"{name}: trading-required connector")
        if len(state.get("books_ready", [])) != count or len(state.get("rules_ready", [])) != count:
            errors.append(f"{name}: incomplete fresh books/rules")
    if len(data.get("market_comparison", [])) != len(data.get("watchlist", [])):
        errors.append("funding metadata incomplete")
    account = data["paper_account"]
    initial, equity = number(account["initial_balance"]), number(account["equity"])
    total = number(data["performance"]["global_pnl_quote"])
    closed, active = data.get("closed_trades", []), data.get("active_positions", [])
    realized = sum((number(r["total_net_pnl"]) for r in closed), Decimal(0))
    floating = sum((number(r["net_pnl"]) for r in active), Decimal(0))
    if abs(total-realized-floating) > Decimal("0.000001") or abs(equity-initial-total) > Decimal("0.000001"):
        errors.append("account/global/position PnL do not reconcile")
    for row in closed:
        if abs(number(row["trading_pnl"])+number(row["funding_pnl"])-number(row["total_net_pnl"])) > Decimal("0.000001"):
            errors.append("closed-trade financial identity failed")
    for row in closed+active:
        events = row.get("funding_payments", [])
        keys = [p.get("event_id") or (p.get("connector"),p.get("timestamp"),p.get("trading_pair")) for p in events]
        if len(set(keys)) != len(keys):
            errors.append("duplicate funding event within a position/tranche")
        funding = sum((number(p["amount"]) for p in events),Decimal(0))
        if abs(funding-number(row.get("funding_pnl",row.get("funding_income",0)))) > Decimal("0.000001"):
            errors.append("funding events do not reconcile to position income")
    if any(number(v)<0 for v in account.get("available",{}).values()):
        errors.append("per-venue paper collateral deficit")
    pending = sum(bool(r.get("funding_pending")) for r in closed+active)
    estimates = sum(bool(p.get("amount_estimated")) for r in closed+active for p in r.get("funding_payments", []))
    return {"healthy": not errors, "errors": errors, "equity": float(equity), "initial_balance": float(initial),
            "net_pnl": float(total), "realized_pnl": float(realized), "unrealized_pnl": float(floating),
            "closed_records": len(closed), "closed_positions": len({r.get("position_id", r.get("trade_id")) for r in closed}),
            "active_positions": len(active), "pending_funding_records": pending, "estimated_funding_events": estimates,
            "rejections": audit.get("rejections", {}), "ledger_updated_at": data["updated_at"]}


def atomic(path, data):
    temporary = path.with_suffix(path.suffix+".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, allow_nan=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--interval", type=float, default=60)
    args = parser.parse_args()
    if not (0 < args.hours <= 72 and 1 <= args.interval <= 60):
        raise SystemExit("hours must be in (0,72], interval in [1,60]")
    args.output.mkdir(parents=True, exist_ok=False)
    started, clock = time.time(), time.monotonic()
    samples, healthy, peak, maximum_dd = 0, 0, None, 0.0
    previous, longest_gap = started, 0.0
    latest = {}
    while True:
        now = time.time()
        elapsed = time.monotonic()-clock
        longest_gap = max(longest_gap, now-previous)
        previous = now
        try:
            data = json.loads(args.ledger.read_text(encoding="utf-8"))
            latest = inspect_ledger(data, now)
            peak = max(peak or latest["initial_balance"], latest["equity"])
            maximum_dd = max(maximum_dd, peak-latest["equity"])
        except Exception as exc:
            latest = {"healthy": False, "errors": [f"unavailable observation: {type(exc).__name__}: {exc}"]}
        samples += 1
        healthy += latest["healthy"]
        complete = elapsed >= args.hours*3600
        snapshot = {"timestamp": now, "utc": datetime.fromtimestamp(now, timezone.utc).isoformat(), **latest}
        with (args.output/"samples.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(snapshot, ensure_ascii=False, allow_nan=False)+"\n")
            stream.flush()
        engineering = "running"
        if complete:
            engineering = "observed_checks_passed" if healthy/samples >= .95 and longest_gap <= max(180,args.interval*3) and latest["healthy"] else "needs_investigation"
        if "net_pnl" not in latest:
            profit = "data_unavailable"
        elif not latest.get("closed_records"):
            profit = "insufficient_closed_trades"
        elif latest.get("pending_funding_records"):
            profit = "pending_funding_reconciliation"
        elif latest.get("net_pnl", 0) <= 0:
            profit = "nonpositive_observed_net_pnl"
        else:
            profit = "positive_observed_sample_not_long_term_proof"
        atomic(args.output/"acceptance.json", {"phase": "complete" if complete else "running",
            "started_at": started, "target_hours": args.hours, "observed_seconds": elapsed,
            "samples": samples, "healthy_samples": healthy, "healthy_ratio": healthy/samples,
            "longest_sample_gap_seconds": longest_gap, "sampled_max_drawdown_usd": maximum_dd,
            "engineering": engineering, "profitability": profit, "latest": snapshot,
            "limitations": "Sampled drawdown may miss intra-minute moves. No-trade runs do not validate funding settlement. Positive short-run PnL is not proof of positive expectation. Verify database snapshots and event histories separately."})
        if complete:
            return
        time.sleep(min(args.interval, max(1, args.hours*3600-elapsed)))


if __name__ == "__main__":
    main()
