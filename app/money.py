"""Money helpers.

All monetary amounts are stored and moved as **integer paise** (1 rupee = 100
paise). Doing arithmetic in the smallest currency unit avoids binary floating
point drift (e.g. 0.1 + 0.2 != 0.3). Conversions at the API boundary use
``Decimal`` with round-half-up so results are deterministic.
"""

from decimal import Decimal, ROUND_HALF_UP

from .constants import ADVANCE_RATE

_CENTS = Decimal("1")


def rupees_to_paise(rupees) -> int:
    """Convert a rupee value (int/float/str/Decimal) to integer paise."""
    amount = Decimal(str(rupees))
    return int((amount * 100).quantize(_CENTS, rounding=ROUND_HALF_UP))


def paise_to_rupees(paise: int) -> float:
    """Convert integer paise back to a rupee float for API responses."""
    return float(Decimal(int(paise)) / Decimal(100))


def advance_amount_paise(earning_paise: int, rate: Decimal = ADVANCE_RATE) -> int:
    """Advance payout for a sale = ``rate`` (default 10%) of its earnings."""
    amount = (Decimal(int(earning_paise)) * rate).quantize(_CENTS, rounding=ROUND_HALF_UP)
    return int(amount)
