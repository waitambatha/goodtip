"""One row for one feed game in one round.

Adding several competitions to an organisation at once started a background
sync for each, and they raced: the same ESPN game was written two to four times
in a round, milliseconds apart. Members saw it twice and could tip it twice, and
only one copy ever got its result. Each game keeps the copy with the result (and
failing that the most tips), tips move across to it, and the constraint makes a
second copy impossible (the next migration: Postgres will not build an index in
the transaction that changed its rows).
"""
from django.db import migrations
from django.db.models import Count


def merge_duplicates(apps, schema_editor):
    Match = apps.get_model("tipping", "Match")
    Tip = apps.get_model("tipping", "Tip")
    groups = (
        Match.objects.exclude(external_id="").values("round_id", "external_id")
        .annotate(n=Count("id")).filter(n__gt=1)
    )
    for group in list(groups):
        copies = list(
            Match.objects.filter(round_id=group["round_id"], external_id=group["external_id"])
            .annotate(tip_count=Count("tips")).order_by("id")
        )
        keep = max(copies, key=lambda m: (m.result is not None, m.tip_count, -m.id))
        for other in copies:
            if other.pk == keep.pk:
                continue
            for tip in Tip.objects.filter(match_id=other.pk):
                held = Tip.objects.filter(match_id=keep.pk, user_id=tip.user_id, org_id=tip.org_id,
                                          group_id=tip.group_id).first()
                if held is None:
                    Tip.objects.filter(pk=tip.pk).update(match_id=keep.pk)
                elif held.is_correct is None and tip.is_correct is not None:
                    # The marked tip is the one that scored; it replaces the unmarked one.
                    held.delete()
                    Tip.objects.filter(pk=tip.pk).update(match_id=keep.pk)
            Match.objects.filter(pk=other.pk).delete()


class Migration(migrations.Migration):
    dependencies = [("tipping", "0017_match_created_at")]

    operations = [
        migrations.RunPython(merge_duplicates, migrations.RunPython.noop),
    ]
