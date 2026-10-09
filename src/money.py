"""Money display formatting — one place, so every screen shows amounts alike.

Amounts are stored as floats and rounded (``math.ceil`` in places) by the
services. This module only *formats* an already-computed number for display and
must never change its value; a cents/rounding change is a separate, deliberate
project (see CLAUDE.md). Before this existed, call sites disagreed on decimals
and separators — ``1234567.00`` in one label, ``1,234,567`` in the next.
"""


def format_money(value, decimals=0, symbol=""):
    """Format an amount for display: thousands-separated, ``decimals`` places.

    ``symbol`` (e.g. ``"KES "``) is prefixed when supplied, so a currency label
    can be turned on in one place later. Non-numeric or ``None`` values format
    as zero rather than raising, because this runs in table-painting loops where
    a crash would blank the screen.

    This is display only: the stored value is untouched.
    """
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    text = f"{number:,.{decimals}f}"
    return f"{symbol}{text}" if symbol else text
