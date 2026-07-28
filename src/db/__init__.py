"""Data-access layer.

Phase 2 moves SQL out of services, views and the 2,500-line ``DatabaseManager``
into one repository per aggregate, so that every statement touching a table
lives in exactly one place.

The migration is incremental by design: ``DatabaseManager`` keeps its public
API and delegates to a repository as each one lands, so no caller changes and
the suite stays green at every step. Repositories added so far:

* :class:`~src.db.fund.FundRepository` — ``savings``, ``christmas_savings``,
  ``benevolent_ledger`` and ``benevolent_accounts``.
* :class:`~src.db.journal.JournalRepository` — the double-entry general ledger.
* :class:`~src.db.ledger.LedgerRepository` — the ``ledger`` subledger.
* :class:`~src.db.loan.LoanRepository` — ``loans`` and ``loan_suspensions``.
* :class:`~src.db.member.MemberRepository` — the ``individuals`` table.
* :class:`~src.db.settings.SettingsRepository` — the ``settings`` table.

See docs/db/repository-layer.md for the full sequencing.
"""
from src.db.base import Repository
from src.db.fund import FundRepository
from src.db.journal import JournalRepository
from src.db.ledger import LedgerRepository
from src.db.loan import LoanRepository
from src.db.member import MemberRepository
from src.db.settings import SettingsRepository

__all__ = ["FundRepository", "JournalRepository", "LedgerRepository",
           "LoanRepository", "MemberRepository", "Repository",
           "SettingsRepository"]
