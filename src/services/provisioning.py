"""Loan-loss provisioning (SASRA classification & impairment).

Classifies the active loan portfolio by days in arrears, computes the required
provision per the configured bands, and books the impairment into the general
ledger as:

    Dr  Loan Loss Provision (expense)
    Cr  Allowance for Loan Losses (contra-asset)

Because the allowance is a contra-asset, booking it reduces net loans on the
balance sheet, and the expense flows through the income statement — both fall
out of the existing GLService statements with no extra wiring.
"""
from collections import defaultdict
from datetime import datetime

from src.config import (
    SASRA_PROVISION_BANDS, PAR_THRESHOLD_DAYS, PROVISION_NET_OF_SAVINGS,
    GL_LOAN_LOSS_EXPENSE, GL_ALLOWANCE_LOAN_LOSS,
)


class ProvisioningService:
    """Portfolio classification and loan-loss provisioning."""

    def __init__(self, db_manager, gl_service):
        self.db = db_manager
        self.gl = gl_service

    @staticmethod
    def _classify(days_overdue):
        """Return (bucket_label, rate) for a number of days in arrears."""
        for label, lo, hi, rate in SASRA_PROVISION_BANDS:
            if days_overdue >= lo and (hi is None or days_overdue <= hi):
                return label, rate
        # Falls through only if bands don't start at 0; treat as performing.
        return SASRA_PROVISION_BANDS[0][0], SASRA_PROVISION_BANDS[0][3]

    @staticmethod
    def _days_overdue(next_due_date, as_of):
        if not next_due_date:
            return 0
        try:
            due = datetime.strptime(str(next_due_date)[:10], "%Y-%m-%d")
            ref = datetime.strptime(as_of, "%Y-%m-%d")
        except ValueError:
            return 0
        return max((ref - due).days, 0)

    def classify_loans(self, as_of=None, net_of_savings=None):
        """Classify each active loan. Returns a list of per-loan dicts.

        When netting is enabled, each member's savings reduce their loan
        exposure (allocated pro-rata across that member's loans by balance)
        before the provision rate is applied. ``balance`` is the gross
        outstanding; ``net_exposure`` is what the provision is computed on.
        """
        as_of = as_of or datetime.now().strftime("%Y-%m-%d")
        if net_of_savings is None:
            net_of_savings = PROVISION_NET_OF_SAVINGS

        # Group active, positive-balance loans by member.
        by_member = defaultdict(list)
        for loan in self.db.get_all_active_loans():
            balance = round(float(loan.get('balance') or 0), 2)
            if balance <= 0:
                continue
            by_member[loan.get('individual_id')].append((loan, balance))

        out = []
        for ind_id, mloans in by_member.items():
            member_total = round(sum(b for _, b in mloans), 2)
            savings = 0.0
            if net_of_savings and member_total > 0:
                savings = max(round(float(self.db.get_savings_balance(ind_id) or 0), 2), 0.0)

            for loan, balance in mloans:
                # Allocate the member's savings across their loans pro-rata.
                allocated = round(savings * (balance / member_total), 2) if member_total else 0.0
                net_exposure = max(round(balance - allocated, 2), 0.0)

                # Suspended loans are not in arrears (deductions paused by
                # agreement), so they classify as performing.
                if loan.get('is_suspended', 0):
                    days = 0
                else:
                    days = self._days_overdue(loan.get('next_due_date'), as_of)
                bucket, rate = self._classify(days)
                out.append({
                    'ref': loan.get('ref'),
                    'individual_id': ind_id,
                    'balance': balance,
                    'net_exposure': net_exposure,
                    'days_overdue': days,
                    'bucket': bucket,
                    'rate': rate,
                    'provision': round(net_exposure * rate, 2),
                })
        return out

    def get_provisioning_summary(self, as_of=None):
        """Aggregate the portfolio by classification band.

        Returns a dict with one row per band (in band order), portfolio totals,
        the required provision, and the Portfolio-at-Risk ratio.
        """
        as_of = as_of or datetime.now().strftime("%Y-%m-%d")
        loans = self.classify_loans(as_of)

        order = [b[0] for b in SASRA_PROVISION_BANDS]
        rate_of = {b[0]: b[3] for b in SASRA_PROVISION_BANDS}
        agg = {label: {'count': 0, 'gross': 0.0, 'net': 0.0, 'provision': 0.0} for label in order}

        total_gross = 0.0
        total_net = 0.0
        total_provision = 0.0
        par_gross = 0.0
        for ln in loans:
            a = agg[ln['bucket']]
            a['count'] += 1
            a['gross'] = round(a['gross'] + ln['balance'], 2)
            a['net'] = round(a['net'] + ln['net_exposure'], 2)
            a['provision'] = round(a['provision'] + ln['provision'], 2)
            total_gross = round(total_gross + ln['balance'], 2)
            total_net = round(total_net + ln['net_exposure'], 2)
            total_provision = round(total_provision + ln['provision'], 2)
            if ln['days_overdue'] > PAR_THRESHOLD_DAYS:
                par_gross = round(par_gross + ln['balance'], 2)

        bands = [{
            'bucket': label,
            'rate': rate_of[label],
            'count': agg[label]['count'],
            'gross': agg[label]['gross'],
            'net': agg[label]['net'],
            'provision': agg[label]['provision'],
        } for label in order]

        par_ratio = round(par_gross / total_gross, 4) if total_gross else 0.0
        return {
            'as_of': as_of,
            'bands': bands,
            'total_gross': total_gross,
            'total_net': total_net,
            'total_provision': total_provision,
            'par_ratio': par_ratio,
        }

    def book_provision(self, as_of=None, created_by=None):
        """Adjust the allowance to the required provision and return a summary.

        Posts the delta between the currently-booked allowance and the required
        provision, so calling it repeatedly converges the allowance to the
        correct level (and is a no-op when already correct).
        """
        as_of = as_of or datetime.now().strftime("%Y-%m-%d")
        summary = self.get_provisioning_summary(as_of)
        required = summary['total_provision']
        current = self.gl.get_account_balance(GL_ALLOWANCE_LOAN_LOSS, as_of)
        delta = round(required - current, 2)

        entry_id = None
        if abs(delta) >= 0.005:
            if delta > 0:
                lines = [
                    {'account': GL_LOAN_LOSS_EXPENSE, 'debit': delta},
                    {'account': GL_ALLOWANCE_LOAN_LOSS, 'credit': delta},
                ]
            else:
                # Portfolio improved — release part of the allowance.
                lines = [
                    {'account': GL_ALLOWANCE_LOAN_LOSS, 'debit': -delta},
                    {'account': GL_LOAN_LOSS_EXPENSE, 'credit': -delta},
                ]
            entry_id = self.gl.post_journal(
                as_of, lines,
                memo=f"Loan loss provision to required level ({required:,.2f})",
                source="provision", created_by=created_by,
            )

        return {
            'entry_id': entry_id,
            'required': required,
            'previous_allowance': round(current, 2),
            'change': delta,
            'summary': summary,
        }
