from django.db import migrations
from django.utils.text import slugify


# Australian states/territories, roughly by population for display order.
STATES = [
    ("NSW", "New South Wales"),
    ("VIC", "Victoria"),
    ("QLD", "Queensland"),
    ("WA", "Western Australia"),
    ("SA", "South Australia"),
    ("TAS", "Tasmania"),
    ("ACT", "Australian Capital Territory"),
    ("NT", "Northern Territory"),
]

GROUP_TYPES = [
    ("workplace", "Workplace"),
    ("community", "Community group"),
]

# Starter taxonomy — matches the Good List mockup, plus common Australian
# workplace sectors. The GoodTip team can edit this list in admin at any time.
INDUSTRIES = [
    "Professional Services",
    "Hospitality",
    "Construction",
    "Retail",
    "Finance",
    "Healthcare",
    "Education",
    "Technology",
    "Manufacturing",
    "Transport & Logistics",
    "Government & Public Sector",
    "Not-for-profit",
    "Other",
]


def seed(apps, schema_editor):
    State = apps.get_model("catalog", "State")
    for order, (code, name) in enumerate(STATES):
        State.objects.get_or_create(code=code, defaults={"name": name, "sort_order": order})

    GroupType = apps.get_model("catalog", "GroupType")
    for order, (slug, name) in enumerate(GROUP_TYPES):
        GroupType.objects.get_or_create(slug=slug, defaults={"name": name, "sort_order": order})

    Industry = apps.get_model("catalog", "Industry")
    for name in INDUSTRIES:
        Industry.objects.get_or_create(slug=slugify(name), defaults={"name": name})

    GoodListConfig = apps.get_model("catalog", "GoodListConfig")
    GoodListConfig.objects.get_or_create(
        pk=1, defaults={"privacy_min_groups": 5, "credibility_min_groups": 10},
    )


def unseed(apps, schema_editor):
    apps.get_model("catalog", "State").objects.filter(
        code__in=[c for c, _ in STATES]
    ).delete()
    apps.get_model("catalog", "GroupType").objects.filter(
        slug__in=[s for s, _ in GROUP_TYPES]
    ).delete()
    apps.get_model("catalog", "Industry").objects.filter(
        slug__in=[slugify(n) for n in INDUSTRIES]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0006_goodlistconfig_grouptype_industry_state"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
