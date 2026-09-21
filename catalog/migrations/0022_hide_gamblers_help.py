"""Take Gamblers Help off the charity pickers.

CLIENT, 17 SEP 2026: "please hide 'gamblers help' from the charities — is
there a way we can do this without deleting them?"

There is, and 0021 added it: ``Charity.is_hidden`` drops a charity out of every
picker and ballot while leaving the row, its logo and any donation history
exactly where they are. This is the flag being set for the one charity that
was asked about.

WHY A MIGRATION AND NOT A TICK IN THE ADMIN. A tick in the admin is one
database. There are two here plus every developer's copy, and a request made
once should not have to be remembered three times — nor discovered missing on
live in the middle of a demo because somebody ticked it on staging.

It matches on the NAME rather than on an id, because the id differs between
those databases. Narrow enough not to catch anything else: the string is
matched whole, case-insensitively, rather than as a substring, so a future
"Gamblers Help NSW" is a decision somebody makes deliberately rather than one
this migration makes for them.

REVERSIBLE, and the reverse is honest: it only un-hides what this hid, by the
same name match. Anything an admin hides by hand is untouched by both
directions.
"""
from django.db import migrations

CHARITY_NAME = "Gamblers Help"


def hide(apps, schema_editor):
    Charity = apps.get_model("catalog", "Charity")
    Charity.objects.filter(name__iexact=CHARITY_NAME).update(is_hidden=True)


def unhide(apps, schema_editor):
    Charity = apps.get_model("catalog", "Charity")
    Charity.objects.filter(name__iexact=CHARITY_NAME).update(is_hidden=False)


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0021_charity_is_hidden"),
    ]

    operations = [
        migrations.RunPython(hide, unhide),
    ]
