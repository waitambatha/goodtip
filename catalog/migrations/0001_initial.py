from django.db import migrations, models
import django.db.models.deletion


SPORTS = [
    ("AFL", "afl"),
    ("NRL", "nrl"),
]

COMPETITIONS = [
    # (name, slug, sport_name, is_womens)
    ("AFL", "afl", "AFL", False),
    ("AFLW", "aflw", "AFL", True),
    ("NRL", "nrl", "NRL", False),
    ("NRLW", "nrlw", "NRL", True),
]

SEASONS = [
    (2026, "2026"),
]


def seed_reference_data(apps, schema_editor):
    Sport = apps.get_model("catalog", "Sport")
    Competition = apps.get_model("catalog", "Competition")
    Season = apps.get_model("catalog", "Season")

    sports = {}
    for name, slug in SPORTS:
        sports[name], _ = Sport.objects.get_or_create(name=name, defaults={"slug": slug})

    for name, slug, sport_name, is_womens in COMPETITIONS:
        Competition.objects.get_or_create(
            name=name,
            defaults={"slug": slug, "sport": sports[sport_name], "is_womens": is_womens},
        )

    for year, label in SEASONS:
        Season.objects.get_or_create(year=year, defaults={"label": label})


def unseed_reference_data(apps, schema_editor):
    apps.get_model("catalog", "Competition").objects.all().delete()
    apps.get_model("catalog", "Sport").objects.all().delete()
    apps.get_model("catalog", "Season").objects.all().delete()


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Sport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=50, unique=True)),
                ("slug", models.SlugField(max_length=50, unique=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="Season",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("year", models.IntegerField(unique=True)),
                ("label", models.CharField(blank=True, max_length=50)),
            ],
            options={"ordering": ["-year"]},
        ),
        migrations.CreateModel(
            name="Competition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=50, unique=True)),
                ("slug", models.SlugField(max_length=50, unique=True)),
                ("is_womens", models.BooleanField(default=False)),
                ("sport", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="competitions", to="catalog.sport")),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.RunPython(seed_reference_data, unseed_reference_data),
    ]
