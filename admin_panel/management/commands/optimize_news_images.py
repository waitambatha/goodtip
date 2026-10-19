"""Re-compress every news picture already on disk — the other half of
admin_panel.image_opt. That module only ever touches a NEW upload; every
picture posted before it existed is still the original, unshrunk file
(client, 19 Oct 2026: "the blog is loading too long... even the images").

Safe to run more than once: a file already under the size floor is left
alone rather than re-encoded for no reason, and re-optimizing an already-
optimized JPEG is a harmless no-op either way.
"""

from django.core.management.base import BaseCommand

from admin_panel.image_opt import optimize_image
from admin_panel.models import NewsMedia, NewsPost

#: Below this, a file is already small enough that re-encoding it is not
#: worth the risk of a visible quality drop for a few more KB.
SKIP_UNDER_BYTES = 200_000


class Command(BaseCommand):
    help = "Resize and recompress every existing NewsPost/NewsMedia image on disk."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        total_before = total_after = 0
        touched = 0

        fields = [(p, "image") for p in NewsPost.objects.exclude(image="")]
        fields += [(m, "file") for m in NewsMedia.objects.exclude(file="")]

        for obj, field_name in fields:
            field_file = getattr(obj, field_name)
            try:
                if not field_file.storage.exists(field_file.name):
                    continue
                before_size = field_file.size
            except Exception as e:
                self.stderr.write(f"skipping {field_file.name}: {e}")
                continue

            if before_size < SKIP_UNDER_BYTES:
                continue

            field_file.open("rb")
            optimized = optimize_image(field_file, name=field_file.name)
            field_file.close()
            if optimized is None:
                self.stderr.write(f"could not read {field_file.name}, left as-is")
                continue

            total_before += before_size
            total_after += optimized.size
            touched += 1

            if dry_run:
                self.stdout.write(
                    f"{field_file.name}: {before_size:,} -> {optimized.size:,} bytes"
                )
                continue

            # optimize_image() was given field_file.name itself above, which
            # already carries the upload_to folder — so optimized.name is a
            # full relative path already, not a bare filename to re-prepend
            # the folder onto.
            old_name = field_file.name
            new_path = field_file.storage.save(optimized.name, optimized)
            # .update(), not .save() — on a NewsPost this would otherwise
            # bump updated_at (auto_now), which sync_newsposts reads as "an
            # edit happened here" and would copy this post's other fields
            # across based on nothing more than its picture having been
            # shrunk. A re-encode is not an edit.
            type(obj).objects.filter(pk=obj.pk).update(**{field_name: new_path})
            field_file.storage.delete(old_name)

        saved = total_before - total_after
        self.stdout.write(
            f"{'would touch' if dry_run else 'touched'} {touched} file(s), "
            f"{total_before:,} -> {total_after:,} bytes "
            f"({saved:,} bytes saved)"
        )
