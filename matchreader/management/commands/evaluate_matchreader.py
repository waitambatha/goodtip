"""Compare candidate MatchReader feature sets, honestly.

Two things this does that a bare accuracy number does not.

THREE SPLITS, NOT TWO. Comparing N variants on the test seasons and keeping
the best is selection on the test set: the winner carries the luck of that
particular draw and the quoted accuracy is optimistically biased. So the
variant is chosen on a validation season and only then scored once on
seasons that had no part in choosing it. When this was first run the biased
pass put margin form at 72.5% for AFL; the honest pass put the same family
at 69.0%, and picked a different variant.

McNEMAR, NOT A GAP IN PERCENTAGES. Two models scored on the same games are
paired, so the question is not "is A's accuracy higher" but "on the games
where they disagree, is A right more often than chance explains". A +3pp gap
on 376 games is routinely nothing. Every comparison here reports the
discordant counts and a p-value, against always-picking-home and against the
model currently shipping.

    manage.py evaluate_matchreader --series AFL
    manage.py evaluate_matchreader --series NRL --window 5,10,15
"""
from __future__ import annotations

import math

from django.core.management.base import BaseCommand

from matchreader.models import HistoricalMatch

# Feature sets to try. "wins" is what ships; the others were the candidates
# for improving it.
KINDS = ("wins", "margin", "both")


def _mcnemar(a_right: list[bool], b_right: list[bool]) -> tuple[int, int, float]:
    """Paired sign test on the games where two predictors disagree.

    Returns (a-only-right, b-only-right, two-sided p). The continuity
    correction is the standard -1 on the numerator.
    """
    b = sum(1 for x, y in zip(a_right, b_right) if x and not y)
    c = sum(1 for x, y in zip(a_right, b_right) if y and not x)
    n = b + c
    if n == 0:
        return b, c, 1.0
    z = (abs(b - c) - 1) / math.sqrt(n)
    return b, c, math.erfc(z / math.sqrt(2))


class Command(BaseCommand):
    help = "Fit/select/report comparison of MatchReader feature sets."

    def add_arguments(self, parser):
        parser.add_argument("--series", default="AFL")
        parser.add_argument("--window", default="5,10,15",
                            help="Comma-separated form windows to try.")
        parser.add_argument("--report-seasons", type=int, default=2,
                            help="Trailing seasons held back for the final score.")

    # -- data -------------------------------------------------------------

    def _history(self, series_name):
        # id as a tiebreak: several fixtures share a kickoff time, and without
        # it the walk order — and so every form window — shifts between runs
        # of the same query.
        return list(
            HistoricalMatch.objects.filter(series__name=series_name)
            .only("home_team_id", "away_team_id", "home_score", "away_score",
                  "season", "kickoff_at")
            .order_by("kickoff_at", "id")
        )

    def _margins(self, history, team_id, index, window):
        """That team's last `window` results before `index`, as their own
        margin, most recent first. Walks backwards so nothing after the game
        being predicted can reach the feature."""
        out = []
        for past in reversed(history[:index]):
            if past.home_team_id == team_id:
                out.append(past.home_score - past.away_score)
            elif past.away_team_id == team_id:
                out.append(past.away_score - past.home_score)
            else:
                continue
            if len(out) == window:
                break
        return out

    def _features(self, history, kind, window, scale):
        """0.5 means no evidence, for a side with no games behind it.

        The margin form squashes through tanh so one 60-point thrashing cannot
        outweigh four close losses, and divides by the mean absolute margin of
        the FITTING seasons so the same code works for a game decided by 6
        points and one decided by 40.
        """
        def wins(ms):
            return 0.5 if not ms else sum(1 for m in ms if m > 0) / len(ms)

        def margin(ms):
            if not ms:
                return 0.5
            return sum(0.5 + 0.5 * math.tanh(m / scale) for m in ms) / len(ms)

        rows = []
        for i, m in enumerate(history):
            h = self._margins(history, m.home_team_id, i, window)
            a = self._margins(history, m.away_team_id, i, window)
            if kind == "wins":
                rows.append([wins(h) - wins(a)])
            elif kind == "margin":
                rows.append([margin(h) - margin(a)])
            else:
                rows.append([wins(h) - wins(a), margin(h) - margin(a)])
        return rows

    def _fit_predict(self, X, y, fit_mask, eval_mask):
        from sklearn.linear_model import LogisticRegression

        Xtr = [x for x, k in zip(X, fit_mask) if k]
        ytr = [v for v, k in zip(y, fit_mask) if k]
        Xev = [x for x, k in zip(X, eval_mask) if k]
        yev = [v for v, k in zip(y, eval_mask) if k]
        model = LogisticRegression().fit(Xtr, ytr)
        preds = [int(p) for p in model.predict(Xev)]
        return model, preds, yev

    # -- the run ----------------------------------------------------------

    def handle(self, *a, **o):
        series_name = o["series"]
        windows = [int(w) for w in o["window"].split(",") if w.strip()]
        history = self._history(series_name)
        if not history:
            self.stderr.write(self.style.ERROR(
                f"No history for {series_name}. Run backfill_history first."))
            return

        seasons = sorted({m.season for m in history})
        need = o["report_seasons"] + 2
        if len(seasons) < need:
            self.stderr.write(self.style.ERROR(
                f"{series_name} has seasons {seasons}; need {need} for a "
                "fit/select/report split."))
            return

        report_s = set(seasons[-o["report_seasons"]:])
        select_s = {seasons[-o["report_seasons"] - 1]}
        y = [1 if m.home_score > m.away_score else 0 for m in history]
        fit_mask = [m.season not in report_s and m.season not in select_s for m in history]
        sel_mask = [m.season in select_s for m in history]
        rep_mask = [m.season in report_s for m in history]
        final_fit = [x or z for x, z in zip(fit_mask, sel_mask)]

        def scale_over(mask):
            vals = [abs(m.home_score - m.away_score) for m, k in zip(history, mask) if k]
            return sum(vals) / len(vals)

        self.stdout.write(
            f"{series_name}: fit {sorted(set(seasons) - select_s - report_s)}  "
            f"select {sorted(select_s)}  report {sorted(report_s)}")

        # ---- selection: validation season only --------------------------
        self.stdout.write("\n  selection (validation season, the only thing "
                          "allowed to pick the variant)")
        sel_scale = scale_over(fit_mask)
        best = None
        for window in windows:
            for kind in KINDS:
                X = self._features(history, kind, window, sel_scale)
                _, preds, yev = self._fit_predict(X, y, fit_mask, sel_mask)
                acc = sum(1 for p, v in zip(preds, yev) if p == v) / len(yev)
                self.stdout.write(f"    {kind:7} w={window:<3} acc {acc:.1%}")
                if best is None or acc > best[0]:
                    best = (acc, kind, window)
        _, kind, window = best
        self.stdout.write(self.style.WARNING(f"  -> chosen: {kind} w={window}"))

        # ---- report: seasons that had no say ----------------------------
        rep_scale = scale_over(final_fit)
        X = self._features(history, kind, window, rep_scale)
        model, preds, yev = self._fit_predict(X, y, final_fit, rep_mask)
        acc = sum(1 for p, v in zip(preds, yev) if p == v) / len(yev)
        base = sum(yev) / len(yev)

        right = [p == v for p, v in zip(preds, yev)]
        home_right = [v == 1 for v in yev]
        b, c, p_base = _mcnemar(right, home_right)

        # ...and against what ships today: win-rate form over five.
        Xs = self._features(history, "wins", 5, rep_scale)
        _, ship_preds, _ = self._fit_predict(Xs, y, final_fit, rep_mask)
        ship_right = [p == v for p, v in zip(ship_preds, yev)]
        ship_acc = sum(ship_right) / len(ship_right)
        b2, c2, p_ship = _mcnemar(right, ship_right)

        coefs = ", ".join(f"{v:+.4f}" for v in model.coef_[0])
        self.stdout.write(f"\n  report ({len(yev)} games never used to choose)")
        self.stdout.write(f"    z = {model.intercept_[0]:+.4f} + [{coefs}]  scale={rep_scale:.2f}")
        self.stdout.write(f"    candidate      {acc:.1%}")
        self.stdout.write(f"    always-home    {base:.1%}")
        self.stdout.write(f"    shipping (w=5) {ship_acc:.1%}")

        def verdict(label, b, c, p):
            style = self.style.SUCCESS if p < 0.05 else self.style.ERROR
            word = "beats it" if p < 0.05 else "cannot be told apart from it"
            self.stdout.write(style(
                f"    vs {label}: {b} / {c} on disagreements, p={p:.4f} -> {word}"))

        verdict("always-home  ", b, c, p_base)
        verdict("shipping     ", b2, c2, p_ship)

        if p_ship >= 0.05:
            self.stdout.write(self.style.WARNING(
                "\n  Nothing to ship: the candidate is not distinguishable from "
                "the model already live. Changing the algorithm on this "
                "evidence would be changing it on noise."))
