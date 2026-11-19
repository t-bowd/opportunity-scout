"""
Verify Alpaca connectivity — run this once after adding your keys, before the
daily job submits any orders.

    python run_broker_check.py

It only READS (account + open positions); it never submits an order. Confirms
the keys work, tells you whether you're pointed at paper or live, and prints
buying power so you know the account is funded/initialised.
"""
from dotenv import load_dotenv

load_dotenv(".env.local")

from broker import alpaca, config, execution


def main() -> int:
    if not config.enabled():
        print("Alpaca DISABLED — set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env.local.")
        print("(The daily run will stay on the simulator until you do.)")
        return 1

    mode = "LIVE  ⚠️" if config.is_live() else "paper"
    print(f"Endpoint: {config.base_url()}  [{mode}]")

    cli = alpaca.client()
    try:
        acct = cli.get_account()
    except alpaca.BrokerError as e:
        print(f"FAILED to reach Alpaca: {e}")
        return 1

    print(f"Account:  {acct.get('account_number')}  status={acct.get('status')}")
    print(f"Cash:     ${float(acct.get('cash', 0)):,.2f}")
    print(f"Buying power: ${float(acct.get('buying_power', 0)):,.2f}")
    print(f"Portfolio value: ${float(acct.get('portfolio_value', 0)):,.2f}")

    positions = cli.get_positions()
    print(f"Open positions at broker: {len(positions)}")
    for p in positions:
        print(f"  {p['symbol']}: {p['qty']} @ ${float(p['avg_entry_price']):.2f} "
              f"(now ${float(p['current_price']):.2f})")

    # Total return on the capital actually put in — the figure every other read
    # here can't give you, because cash/equity/positions are all point-in-time.
    # Printed here as well as in the daily snapshot so it's checkable on demand
    # without waiting on the cron (and without running the entry stage).
    inception = execution.account_since_inception_usd()
    if inception is None:
        print("\nSince inception: unavailable — portfolio history unreadable.")
    elif inception["return_pct"] is None:
        print("\nSince inception: no capital base to measure against.")
    else:
        print(f"\nSince inception {inception['start_date']} ({inception['days']}d): "
              f"${inception['start_equity']:,.2f} in -> ${inception['equity']:,.2f} "
              f"= {inception['return_pct']:+.1f}%")
        if abs(inception["net_flows_usd"]) >= 1:
            print(f"  includes ${inception['net_flows_usd']:+,.2f} transferred after day "
                  f"one, so the base is ${inception['invested_usd']:,.2f}; the % assumes "
                  f"it was there from the start, which daily bars can't confirm")

    print("\nOK — connectivity confirmed, no orders submitted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
