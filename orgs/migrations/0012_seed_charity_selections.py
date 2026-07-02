from django.db import migrations


def seed_selections(apps, schema_editor):
    """Start the charity timeline for orgs that already have a charity set.

    Existing leagues predate the timeline table; give each a single 'initial'
    row so their current charity is on the record from the start.
    """
    Organisation = apps.get_model("orgs", "Organisation")
    OrgCharitySelection = apps.get_model("orgs", "OrgCharitySelection")
    for org in Organisation.objects.filter(charity__isnull=False).iterator():
        if not OrgCharitySelection.objects.filter(org=org).exists():
            OrgCharitySelection.objects.create(
                org=org,
                season_id=org.season_id,
                charity_id=org.charity_id,
                source="initial",
                effective_from=org.created_at,
            )


def unseed(apps, schema_editor):
    OrgCharitySelection = apps.get_model("orgs", "OrgCharitySelection")
    OrgCharitySelection.objects.filter(source="initial").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("orgs", "0011_orgcharityselection"),
    ]

    operations = [
        migrations.RunPython(seed_selections, unseed),
    ]
