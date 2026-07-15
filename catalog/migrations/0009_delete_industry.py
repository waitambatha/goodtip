"""Retire the flat Industry lookup — superseded by per-type SubCategory
(categories doc, 7 Jul 2026). Runs after orgs has moved its data across and
dropped the FK.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0008_subcategory_five_group_types"),
        ("orgs", "0015_org_categories"),
    ]

    operations = [
        migrations.DeleteModel(name="Industry"),
    ]
