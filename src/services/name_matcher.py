"""Fuzzy name matching for spreadsheet imports.

Productises the token-anchored matcher used to reconcile spreadsheet names
against existing members: titles are stripped, names normalised, and each
candidate scored by how well every one of *its* tokens matches some source
token (so a 3-token sheet name still aligns with a 2-token DB name, and an
exact first-name hit can't outrank a better full-name variant).
"""
import re
import difflib

_PREFIX = re.compile(r'^(mr|mrs|ms|miss|dr)\.?\s+', re.I)


def normalize(name):
    n = _PREFIX.sub('', (name or '').strip()).replace('.', ' ').replace("'", '').lower()
    return re.sub(r'\s+', ' ', n).strip()


def tokens(name):
    return normalize(name).split()


def _ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def score(source_name, db_tokens):
    """(mean DB-anchored similarity, aligned-token count) for one candidate."""
    st = tokens(source_name)
    if not st or not db_tokens:
        return 0.0, 0
    per = [max(_ratio(d, s) for s in st) for d in db_tokens]
    aligned = sum(1 for p in per if p >= 0.80)
    return sum(per) / len(per), aligned


class NameMatcher:
    """Match source names against a fixed candidate roster of (id, name)."""

    def __init__(self, candidates):
        self._cands = [(i, n, tokens(n)) for i, n in candidates]

    def best(self, source_name):
        results = []
        for i, n, dt in self._cands:
            if not dt:
                continue
            sc, al = score(source_name, dt)
            results.append((sc, al, i, n))
        if not results:
            return None
        results.sort(reverse=True)
        sc, al, i, n = results[0]
        second = None
        if len(results) > 1:
            s2 = results[1]
            second = {"id": s2[2], "name": s2[3], "score": s2[0]}
        return {"id": i, "name": n, "score": sc, "aligned": al, "second": second}

    def classify(self, source_name):
        """Return (verdict, best) where verdict is 'match' | 'review' | 'none'.

        - match: both name parts align (high confidence)
        - review: only one token aligns but the overall score is decent
        - none: no confident candidate
        """
        b = self.best(source_name)
        if not b:
            return "none", None
        if b["aligned"] >= 2:
            return "match", b
        if b["aligned"] == 1 and b["score"] >= 0.66:
            return "review", b
        return "none", b
