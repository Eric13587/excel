"""What a grid cell can say, beyond a bare figure.

A cell used to hold one number, which on a fund tab could only ever be one
deposit (or, typed negative, one withdrawal). Anything else — a withdrawal in a
month that also had its deposit, a top-up, a buy-off — meant leaving the grid
for the member's ledger. This is the small language that lets the grid say
those things instead.

THE SYNTAX
----------
A cell is a list of **entries**, separated by commas (or semicolons)::

    2,000                    the tab's default — a deposit, a contribution,
                             a loan deduction
    @W=500                   a named operation and its figure
    2,000, @W=500            both, in the same month
    +@W=500                  added to whatever the month already holds

* An operation is ``@`` and a word. Any unambiguous start of the word will do
  and case does not matter: ``@w``, ``@W``, ``@withdraw`` are the same thing.
* ``=`` is optional (``@w 500`` works), and so are thousands separators. A comma
  separates entries when a space or an ``@`` follows it — which is why
  ``2,000`` stays two thousand. A figure whose commas are not thousands
  (``1,00``) is refused rather than guessed at: this is money.
* Some operations take a second figure after a slash: ``@T=20,000/12`` is a
  top-up of 20,000 over 12 months. Some take none: ``@B`` settles a loan.
* A zero entry is kept, so the grid can tell ``@W=0`` ("no withdrawal this
  month") from not mentioning withdrawals at all.

When a cell holds anything other than one default entry, it is shown with every
entry prefixed — ``@D=2,000, @W=500`` — so what is in the month can be read
back exactly, edited in place, and pasted into another cell.

This module only parses and formats. What an entry *does* — which rows it
writes, which rules it must pass — belongs to the grid that owns the ledger
(:mod:`src.services.fund_grid`, :mod:`src.services.loan_grid`).
"""
import re
from dataclasses import dataclass

#: An entry boundary: a semicolon, a comma followed by a space or an ``@``, or a
#: space that starts an ``@`` entry. ``2,000`` is never split.
_SPLIT = re.compile(r"\s*(?:;|,(?=\s|\+?\s*@)|\s+(?=\+?\s*@))\s*")
#: A figure: optional sign, digits with or without *correct* thousands commas.
_NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|[+-]?\.\d+")
_OP = re.compile(r"@\s*([A-Za-z][A-Za-z\-]*)\s*[=:]?\s*(.*)$")
_DASHES = ("—", "–")

#: What an operation's figure is.
MONEY = "money"
MONTHS = "months"


class OpError(ValueError):
    """Text a cell cannot accept, explained in a sentence for the status line."""


@dataclass(frozen=True)
class Op:
    """One thing a cell can ask for."""

    code: str                   # shown in the cell: the D in @D=2,000
    name: str                   # "Withdrawal"
    words: tuple                # accepted after @, matched by prefix
    help: str                   # one line for the help panel
    value: str = MONEY          # MONEY, MONTHS, or None for no figure
    arg: str = None             # a second figure after "/" — MONTHS for a top-up

    def matches(self, word):
        word = word.lower().replace("-", "")
        return any(w.startswith(word) for w in self.words)

    @property
    def syntax(self):
        """How the help panel writes this operation."""
        if self.value is None:
            return f"@{self.code}"
        figure = "amount" if self.value == MONEY else "months"
        if self.arg:
            figure += "/months"
        return f"@{self.code}={figure}"


@dataclass(frozen=True)
class Entry:
    """One thing a cell says: an operation and its figure(s)."""

    op: Op
    amount: float = 0.0         # money, or a count of months for a MONTHS op
    arg: int = None


def _money(text, what):
    compact = text.replace(" ", "")
    if not _NUMBER.fullmatch(compact):
        if re.fullmatch(r"[+-]?[\d,]+(?:\.\d+)?", compact):
            raise OpError(f"{text!r} has commas in the wrong places{what} — write "
                          f"2,000 or 2000, and separate entries with \", \".")
        raise OpError(f"{text!r} is not a number{what}.")
    return round(float(compact.replace(",", "")), 2)


def _months(text, what):
    cleaned = re.sub(r"\s*(m|mo|months?)$", "", text.strip(), flags=re.IGNORECASE)
    if not cleaned.isdigit():
        raise OpError(f"{text!r} is not a whole number of months{what}.")
    return int(cleaned)


def figure(value):
    """A figure as a cell writes it: whole shillings when whole, else two places."""
    value = round(float(value or 0), 2)
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"


class OpSet:
    """The operations one grid understands, and how to read and write them."""

    def __init__(self, ops, default, negative=None):
        self.ops = tuple(ops)
        self.default = default
        #: The operation a bare negative figure means, if any. On a fund tab
        #: ``-500`` has always meant a withdrawal, and still does.
        self.negative = negative

    def resolve(self, word):
        found = [op for op in self.ops if op.matches(word)]
        exact = [op for op in found if word.lower() in (op.code.lower(), *op.words)]
        if exact:
            return exact[0]
        if len(found) == 1:
            return found[0]
        known = ", ".join(f"@{op.code} ({op.name.lower()})" for op in self.ops)
        if not found:
            raise OpError(f"@{word} is not an operation here. This tab knows {known}.")
        names = " or ".join(op.name.lower() for op in found)
        raise OpError(f"@{word} could mean {names} — type more of the word.")

    def parse(self, text):
        """Read a cell's text into ``(entries, append)``.

        ``append`` is True when the text began with ``+@``: the entries are to
        be added to the month rather than replace it. Raises :class:`OpError`.
        """
        text = (text or "").strip()
        for dash in _DASHES:
            text = text.replace(dash, "")
        text = text.strip()
        append = bool(re.match(r"\+\s*@", text))
        if append:
            text = text.lstrip("+").strip()
        if text in ("", "-"):
            return [], append

        entries = []
        for part in _SPLIT.split(text):
            part = part.strip()
            if part.startswith("+"):
                part = part[1:].strip()     # "+@W" mid-list reads as "@W"
            if not part:
                continue
            entry = self._parse_one(part)
            if entry is not None:
                entries.append(entry)
        return entries, append

    def _parse_one(self, part):
        if not part.startswith("@"):
            amount = _money(part, "")
            if amount < 0:
                if self.negative is None:
                    raise OpError(f"A {self.default.name.lower()} must be more than "
                                  f"zero — clear the cell to remove it.")
                return Entry(self.negative, -amount)
            return Entry(self.default, amount)

        match = _OP.match(part)
        if not match:
            raise OpError(f"{part!r} is not an operation — write it like @W=500.")
        op = self.resolve(match.group(1))
        rest = match.group(2).strip()
        what = f" for @{op.code}"

        if op.value is None:
            if rest:
                raise OpError(f"@{op.code} ({op.name.lower()}) takes no figure — "
                              f"type @{op.code} on its own.")
            return Entry(op)
        if not rest:
            raise OpError(f"@{op.code} needs a figure, like {op.syntax}.")

        arg = None
        if op.arg:
            pieces = [p for p in re.split(r"\s*/\s*", rest) if p]
            if len(pieces) != 2:
                raise OpError(f"@{op.code} needs the months too, like "
                              f"@{op.code}=20,000/12.")
            rest, arg = pieces[0], _months(pieces[1], what)
            if arg <= 0:
                raise OpError(f"@{op.code}: the months must be more than zero.")

        amount = _money(rest, what) if op.value == MONEY else _months(rest, what)
        if amount < 0:
            raise OpError(f"@{op.code} takes a positive figure; the operation "
                          f"already says which way the money moves.")
        return Entry(op, amount, arg)

    def format(self, entries):
        """Write entries back as cell text — the inverse of :meth:`parse`.

        One default entry is written bare, the way the grid always has. Anything
        else gets every entry prefixed, so the month reads back unambiguously.
        """
        if not entries:
            return ""
        if len(entries) == 1 and entries[0].op is self.default:
            return figure(entries[0].amount)
        out = []
        for entry in entries:
            if entry.op.value is None:
                out.append(f"@{entry.op.code}")
                continue
            text = (figure(entry.amount) if entry.op.value == MONEY
                    else str(int(entry.amount)))
            if entry.op.arg and entry.arg:
                text += f"/{entry.arg}"
            out.append(f"@{entry.op.code}={text}")
        return ", ".join(out)

    def help_rows(self):
        """``(syntax, meaning)`` for each operation, default first."""
        rows = [(f"{figure(2000)}  or  {self.default.syntax}", self.default.help)]
        rows += [(op.syntax, op.help) for op in self.ops if op is not self.default]
        return rows
