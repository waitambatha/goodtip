"""Move organisations onto the new type/sub-category taxonomy (categories doc,
7 Jul 2026): add ``sub_categories`` (M2M — Education orgs can hold Primary AND
Secondary), the Informal self-description label, and the admin-only charity
partner flag. Existing ``industry`` values are folded into Business
sub-categories, then the old FK is dropped.
"""
from django.db import migrations, models


# Old Industry slug -> Business sub-category slug. Sectors the spec's Business
# list doesn't carry (Hospitality, Healthcare…) fold into "Other"; the GoodTip
# team can re-file those orgs in admin, or add new sub-categories, at any time.
INDUSTRY_TO_BUSINESS_SUBCAT = {
    "finance": "finance",
    "technology": "tech",
    "retail": "retail",
    "manufacturing": "manufacturing",
    "professional-services": "professional-services",
}


def industry_to_sub_categories(apps, schema_editor):
    Organisation = apps.get_model("orgs", "Organisation")
    SubCategory = apps.get_model("catalog", "SubCategory")

    subcats = {s.slug: s for s in SubCategory.objects.filter(group_type__slug="business")}
    for org in Organisation.objects.filter(industry__isnull=False).select_related("industry", "group_type"):
        # Industry was a workplace-sector field; it only maps cleanly for orgs
        # that landed under Business (ex-"workplace"). Anything else keeps no
        # sub-category rather than getting a wrong one.
        if org.group_type is None or org.group_type.slug != "business":
            continue
        target = subcats.get(INDUSTRY_TO_BUSINESS_SUBCAT.get(org.industry.slug, "other"))
        if target is not None:
            org.sub_categories.add(target)


class Migration(migrations.Migration):

    dependencies = [
        ("orgs", "0014_organisation_group_type_organisation_industry_and_more"),
        ("catalog", "0008_subcategory_five_group_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="organisation",
            name="sub_categories",
            field=models.ManyToManyField(blank=True, related_name="organisations", to="catalog.subcategory"),
        ),
        migrations.AddField(
            model_name="organisation",
            name="informal_label",
            field=models.CharField(blank=True, max_length=60),
        ),
        migrations.AddField(
            model_name="organisation",
            name="is_charity_partner",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(industry_to_sub_categories, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="organisation",
            name="industry",
        ),
    ]
