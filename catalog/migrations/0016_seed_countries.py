"""The eight countries GoodTip operates in, and the segment each maps to.

Australia and New Zealand are their own segments; the Pacific nations share
"Global" for now because their individual numbers are too small to be a
leaderboard on their own — which is exactly why the Pacific Nations grouping
exists over the top of them.

`is_pacific` is set explicitly rather than inferred from segment == "global".
Today the two agree, but the moment a country outside the Pacific is added it
would otherwise be swept onto the Pacific Nations board for no better reason
than not being Australian.
"""
from django.db import migrations


# (code, name, segment, is_pacific, sort_order)
COUNTRIES = [
    ("AU", "Australia", "australia", False, 1),
    ("NZ", "New Zealand", "new_zealand", False, 2),
    ("PG", "Papua New Guinea", "global", True, 3),
    ("SB", "Solomon Islands", "global", True, 4),
    ("FJ", "Fiji", "global", True, 5),
    ("WS", "Samoa", "global", True, 6),
    ("TO", "Tonga", "global", True, 7),
    ("VU", "Vanuatu", "global", True, 8),
]


def seed(apps, schema_editor):
    Country = apps.get_model("catalog", "Country")
    for code, name, segment, is_pacific, order in COUNTRIES:
        Country.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "segment": segment,
                "is_pacific": is_pacific,
                "sort_order": order,
                "is_active": True,
            },
        )


def unseed(apps, schema_editor):
    Country = apps.get_model("catalog", "Country")
    Country.objects.filter(code__in=[c[0] for c in COUNTRIES]).delete()


class Migration(migrations.Migration):

    dependencies = [("catalog", "0015_country")]

    operations = [migrations.RunPython(seed, unseed)]
