"""Find — and optionally clear — image references whose file is not on disk.

WHY THIS EXISTS
---------------
Uploads live in MEDIA_ROOT, which is gitignored, so they exist only on the
server they were uploaded to and travel with nothing. On 4 Sep 2026 that
directory was found missing from the production checkout entirely: the
database still named 15 files — avatars, charity logos, organisation logos and
news images — and not one of them was on disk. Staging was the same, with more
rows, and the client's console was a wall of 404s.

WHY BLANKING THE ROW IS THE FIX AND NOT A WORKAROUND
----------------------------------------------------
Every template in the product already handles "no image" properly: the news
cards draw a branded panel, the story reader rotates match-day photographs,
an avatar falls back to the person's initial. None of that runs for a row that
NAMES a file, because nothing checks whether the file is there — `{% if
p.image %}` is true for a dangling reference, so the page renders an <img>
that 404s and the designed empty state never appears.

A reference to a file that does not exist is not information. Clearing it is
what makes the fallbacks — which are already written, already designed and
already correct — actually run.

The alternative considered and rejected: a template filter that stats the file
at render time, applied at all 47 truthiness checks across 26 templates. It
would self-heal if the files ever came back, which sounds better until you
notice the files are confirmed gone with no backup, so there is nothing to
heal from — and it buys that by putting a filesystem call in every image on
every page, forever, plus 47 chances to miss one.

DRY RUN BY DEFAULT. This edits rows on whatever database the environment
points at, and on production that is member avatars. It reports and changes
nothing unless it is told to.
"""
from django.apps import apps
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.db.models import FileField


class Command(BaseCommand):
    help = "Report image/file references whose file is missing; --apply clears them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually blank the dangling references. Without this, reports only.",
        )
        parser.add_argument(
            "--model", default="",
            help="Limit to one model, e.g. accounts.User. Default: every model.",
        )

    def handle(self, *args, **options):
        apply_it = options["apply"]
        only = (options["model"] or "").lower()

        # `default_storage.exists` rather than a path join: it is the same
        # question the storage backend would answer when serving the file, and
        # it keeps working if MEDIA_ROOT is ever swapped for object storage —
        # which, given how this happened, it should be.
        total_rows = total_missing = 0
        cleared = []

        for model in apps.get_models():
            label = model._meta.label
            if only and label.lower() != only:
                continue
            fields = [f for f in model._meta.get_fields() if isinstance(f, FileField)]
            if not fields:
                continue

            for field in fields:
                name = field.name
                qs = (
                    model.objects
                    .exclude(**{name: ""})
                    .exclude(**{f"{name}__isnull": True})
                )
                rows = missing = 0
                for obj in qs.iterator():
                    stored = getattr(obj, name).name
                    if not stored:
                        continue
                    rows += 1
                    if default_storage.exists(stored):
                        continue
                    missing += 1
                    cleared.append(f"{label}#{obj.pk}.{name} -> {stored}")
                    if apply_it:
                        setattr(obj, name, "")
                        # update_fields, so this cannot re-save anything else
                        # on the row — a full save() here would rewrite every
                        # column from an object that may be stale.
                        obj.save(update_fields=[name])
                total_rows += rows
                total_missing += missing
                if rows:
                    self.stdout.write(
                        f"{label}.{name:<12} {rows:>4} referenced, "
                        f"{rows - missing:>4} on disk, {missing:>4} missing"
                    )

        self.stdout.write("")
        for line in cleared:
            self.stdout.write(f"  {line}")
        self.stdout.write("")
        self.stdout.write(
            f"{total_rows} file reference(s), {total_missing} with nothing behind them."
        )
        if not total_missing:
            self.stdout.write(self.style.SUCCESS("Nothing to do."))
        elif apply_it:
            self.stdout.write(self.style.SUCCESS(
                f"Cleared {total_missing}. Those records now render their "
                "designed empty state instead of a broken image."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "Dry run — nothing changed. Re-run with --apply to clear them."
            ))
