"""
Alpaca configuration — read once from the environment.

Paper vs live is a single env var (ALPACA_BASE_URL). Going live is never
automatic: it requires changing that URL and the keys by hand. Absent keys mean
the broker is DISABLED and the book runs on the simulator exactly as before.
"""
import os

PAPER_BASE_URL = "https://paper-api.alpaca.markets"

# Trail width for the native trailing-stop SELL. Kept in lockstep with the
# simulator's paper_trader.exit.TRAILING_STOP_TRAIL_PCT so paper and broker
# execution model the same strategy — if you change one, change the other.
TRAIL_PERCENT = 8.0
# The -12% hard stop, mirrored from paper_trader.exit.STOP_LOSS_PCT, expressed
# as the fraction of entry price at which the resting stop rests.
HARD_STOP_PCT = 12.0


def api_key() -> str | None:
    return os.environ.get("ALPACA_API_KEY")


def secret_key() -> str | None:
    return os.environ.get("ALPACA_SECRET_KEY")


def base_url() -> str:
    """Alpaca host WITHOUT the /v2 suffix — the client appends /v2/... itself.

    Alpaca's dashboard shows the endpoint *including* /v2, so accept that form
    too and strip it; otherwise we'd build /v2/v2/orders and 404 on everything.
    """
    url = os.environ.get("ALPACA_BASE_URL", PAPER_BASE_URL).strip().rstrip("/")
    if url.endswith("/v2"):
        url = url[: -len("/v2")]
    return url


def is_live() -> bool:
    return "paper-api" not in base_url()


def enabled() -> bool:
    """True only when both credentials are present. Everything gates on this."""
    return bool(api_key() and secret_key())
