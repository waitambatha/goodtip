"""Delete the child organisations that were being used as departments.

Departments were child Organisation rows. Groups replace them (see
orgs.models.Group), and the two cannot both be the answer, so the old ones go.

Six existed in production and every one was test data — one member each, under
parents named "Test League 1" and "Masterclass solution league", one of them
literally called "new organistion test". Between them they held 70 tips and
4 wall posts. They were reviewed row by row before this was written, and the
call to delete rather than convert was made on that basis.

The cascade is large but entirely theirs: a child organisation carried its own
copy of every round and fixture, so the six had accumulated roughly 298 rounds,
2,416 matches and 22,873 sync-run records. None of that is shared with anything
that survives — rounds and matches are stored per organisation.

This deliberately does NOT touch `Organisation.parent`. A child organisation is
still a legitimate thing for a genuine multi-site business (National Tiles
Mitcham under National Tiles), with its own charity and its own comp, and
family roll-up is built around it. What is being removed is its use as a
stand-in for a department.

Irreversible: reverse() would have to invent rows that no longer exist. The
data was serialised to JSON before this ran, and restoring is `loaddata`, not
a migration.
"""
from django.db import migrations


def delete_department_child_orgs(apps, schema_editor):
    Organisation = apps.get_model("orgs", "Organisation")
    children = Organisation.objects.filter(parent__isnull=False)
    count = children.count()
    if not count:
        return
    # Let the ORM collect the cascade rather than hand-writing the order.
    children.delete()
    print(f"\n    removed {count} department-style child organisation(s)")


class Migration(migrations.Migration):

    dependencies = [
        ("orgs", "0033_organisation_groups_enabled_group_groupmember_and_more"),
        # The tips have to be deletable before their organisations are, and
        # that app's migration adds a column to the same table.
        ("tipping", "0012_alter_tip_unique_together_tip_group_and_more"),
    ]

    operations = [
        migrations.RunPython(
            delete_department_child_orgs,
            migrations.RunPython.noop,
        ),
    ]
