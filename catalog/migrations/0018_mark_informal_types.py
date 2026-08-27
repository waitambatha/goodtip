"""Which of the existing organisation types are informal.

Only "Informal" is. Community is a formal type despite the friendly name — a
sports club or a community organisation is a registered entity with an address
and, usually, a domain; the categories doc has always treated it that way, and
it carries sub-categories that an informal group has no use for.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    OrganisationType = apps.get_model("catalog", "OrganisationType")
    OrganisationType.objects.update(is_formal=True)
    OrganisationType.objects.filter(slug="informal").update(is_formal=False)


def backwards(apps, schema_editor):
    apps.get_model("catalog", "OrganisationType").objects.update(is_formal=True)


class Migration(migrations.Migration):

    dependencies = [("catalog", "0017_organisationtype_is_formal")]

    operations = [migrations.RunPython(forwards, backwards)]
