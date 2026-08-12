"""Fit MatchReader for a series and record the result.

Refit periodically as new results come in — once a season is the cadence the
deck suggests. Each fit is stored, so a refit that makes things worse is a
flag flip to undo rather than a retrain in reverse.
"""

from django.core.management.base import BaseCommand

from matchreader.models import ModelVersion
from matchreader.training import fit_and_score


class Command(BaseCommand):
    help = "Fit MatchReader on historical results and store the coefficients."

    def add_arguments(self, parser):
        parser.add_argument("--series", default="AFL")
        parser.add_argument("--test-seasons", type=int, default=1)
        parser.add_argument(
            "--activate", action="store_true",
            help="Make this the live model, but only if it beats the home-side baseline.",
        )

    def handle(self, *args, **o):
        try:
            r = fit_and_score(o["series"], test_seasons=o["test_seasons"])
        except (ValueError, Exception) as e:
            self.stderr.write(self.style.ERROR(str(e)))
            return

        version = ModelVersion.objects.create(
            series=r["series"], intercept=r["intercept"], form_coef=r["form_coef"],
            exp_coef=r["exp_coef"], train_seasons=r["train_seasons"],
            test_seasons=r["test_seasons"], train_samples=r["train_samples"],
            test_samples=r["test_samples"], accuracy=r["accuracy"],
            baseline_accuracy=r["baseline_accuracy"],
        )

        beats = r["accuracy"] > r["baseline_accuracy"]
        self.stdout.write(f"  train  : {r['train_seasons']} ({r['train_samples']} games)")
        self.stdout.write(f"  test   : {r['test_seasons']} ({r['test_samples']} games, never seen)")
        self.stdout.write(f"  z = {r['intercept']:.4f} + {r['form_coef']:.4f}*dForm")
        self.stdout.write(f"  accuracy      : {r['accuracy']:.1%}")
        self.stdout.write(f"  always-home   : {r['baseline_accuracy']:.1%}")

        if o["activate"] and beats:
            ModelVersion.objects.filter(series=r["series"]).exclude(pk=version.pk).update(is_active=False)
            version.is_active = True
            version.save(update_fields=["is_active"])
            self.stdout.write(self.style.SUCCESS("  activated."))
        elif o["activate"]:
            self.stdout.write(self.style.WARNING(
                "  NOT activated: it does not beat always-picking-home, so it "
                "would be worse than a rule anyone can apply for free."
            ))
        else:
            self.stdout.write("  stored, not activated (pass --activate).")
