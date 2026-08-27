"""Fold New Zealand into the Australia segment, for now.

Client instruction of 2026-08-26: "NZ leaderboard folds into the main
Australia ladder for now. No separate NZ leaderboard needed at this stage
(they'll get their own charity shortlist and leaderboard down the track, but
that's a later item)."

WHAT THIS DOES NOT DO. New Zealand remains its own COUNTRY, and the Good
List's country breakdown still lists it on its own row — that board is a
breakdown by country, and rolling NZ money into the Australia row would label
New Zealand's giving as Australian, which is a different and worse claim than
"one ladder for now".

A segment is the thing a ladder or a charity shortlist would later be SPLIT
on. Groups competing against each other should share one, so while NZ groups
play on the main ladder they carry the main ladder's segment.

Reversible, and the reverse is the whole point: when New Zealand gets its own
board, this migration is rolled back and nothing else has to move.
"""
from django.db import migrations


def fold(apps, schema_editor):
    apps.get_model("catalog", "Country").objects.filter(code="NZ").update(
        segment="australia",
    )


def unfold(apps, schema_editor):
    apps.get_model("catalog", "Country").objects.filter(code="NZ").update(
        segment="new_zealand",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0018_mark_informal_types"),
    ]

    operations = [
        migrations.RunPython(fold, unfold),
    ]
