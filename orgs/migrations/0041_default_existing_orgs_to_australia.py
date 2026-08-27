"""Existing organisations are Australian.

Every organisation on the platform when country was introduced was an
Australian one — GoodTip launched into Australian workplaces, and NZ is the
first market being added. Leaving them NULL would have dropped all of them off
the country boards on day one, which reads as "nobody is anywhere" rather than
as a field nobody has been asked yet.

Groups are deliberately NOT backfilled. A group with no country falls back to
its organisation's (Group.effective_country), so writing Australia onto every
existing group would replace an inherited answer that is already correct with
a hardcoded one that stops following the org if it is ever corrected.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Country = apps.get_model("catalog", "Country")
    Organisation = apps.get_model("orgs", "Organisation")
    au = Country.objects.filter(code="AU").first()
    if au is None:
        return
    Organisation.objects.filter(country__isnull=True).update(country=au)


def backwards(apps, schema_editor):
    Organisation = apps.get_model("orgs", "Organisation")
    Organisation.objects.update(country=None)


class Migration(migrations.Migration):

    dependencies = [
        ("orgs", "0040_group_country_organisation_country"),
        ("catalog", "0016_seed_countries"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
