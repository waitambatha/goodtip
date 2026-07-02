from django.db import migrations, models
import django.db.models.deletion


def set_categories_and_seed_origin(apps, schema_editor):
    Sport = apps.get_model("catalog", "Sport")
    Series = apps.get_model("catalog", "Series")

    # Backfill the category from the legacy is_womens flag.
    Series.objects.filter(is_womens=True).update(category="womens")
    Series.objects.filter(is_womens=False).update(category="mens")

    # State of Origin: a representative series under Rugby League (the "NRL"
    # umbrella). Scored at a premium (4 pts) via the round stage — see tipping.
    nrl = Sport.objects.filter(slug="nrl").first()
    if nrl is not None:
        Series.objects.get_or_create(
            slug="state-of-origin",
            defaults={
                "sport": nrl,
                "name": "State of Origin",
                "is_womens": False,
                "category": "representative",
                "representation_type": "full",
            },
        )


def remove_origin(apps, schema_editor):
    apps.get_model("catalog", "Series").objects.filter(slug="state-of-origin").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0002_charity"),
        # tipping.0002 builds Team/Round FKs to catalog.competition; the rename to
        # Series must run after those FKs exist (matters on a fresh DB replay).
        ("tipping", "0002_use_catalog_lookups"),
    ]

    operations = [
        migrations.RenameModel(old_name="Competition", new_name="Series"),
        migrations.AlterModelOptions(
            name="series",
            options={"ordering": ["name"], "verbose_name_plural": "series"},
        ),
        migrations.AlterField(
            model_name="series",
            name="sport",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="series",
                to="catalog.sport",
            ),
        ),
        migrations.AddField(
            model_name="series",
            name="category",
            field=models.CharField(
                choices=[("mens", "Men's"), ("womens", "Women's"), ("representative", "Representative")],
                default="mens",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="series",
            name="representation_type",
            field=models.CharField(
                choices=[("full", "Fully integrated — no opt-out")],
                default="full",
                max_length=10,
            ),
        ),
        migrations.RunPython(set_categories_and_seed_origin, remove_origin),
    ]
