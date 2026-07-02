from django.db import migrations, models
import django.db.models.deletion


# Relabel the top Sport level to the brief's "code" names (slide 7).
SPORT_RELABEL = {
    "afl": ("Australian Rules", "australian-rules"),
    "nrl": ("Rugby League", "rugby-league"),
}

# Competitions to seed per season: name, slug, sport slug, [series slugs].
COMPETITIONS = [
    ("AFL", "afl", "australian-rules", ["afl", "aflw"]),
    ("NRL", "nrl", "rugby-league", ["nrl", "nrlw", "state-of-origin"]),
]


def seed_codes_and_competitions(apps, schema_editor):
    Sport = apps.get_model("catalog", "Sport")
    Series = apps.get_model("catalog", "Series")
    Season = apps.get_model("catalog", "Season")
    Competition = apps.get_model("catalog", "Competition")

    # 1. Relabel existing Sport rows to the code names (rows + their Series FKs
    #    are preserved — only the display name/slug changes).
    for old_slug, (new_name, new_slug) in SPORT_RELABEL.items():
        Sport.objects.filter(slug=old_slug).update(name=new_name, slug=new_slug)

    # 2. Netball — the third code (Super Netball lands May 2027). No series yet.
    Sport.objects.get_or_create(slug="netball", defaults={"name": "Netball"})

    # 3. One Competition per (sport, season) for every existing season.
    sports = {s.slug: s for s in Sport.objects.all()}
    series = {s.slug: s for s in Series.objects.all()}
    for season in Season.objects.all():
        for name, slug, sport_slug, series_slugs in COMPETITIONS:
            sport = sports.get(sport_slug)
            if sport is None:
                continue
            comp, _ = Competition.objects.get_or_create(
                slug=slug, season=season, defaults={"name": name, "sport": sport},
            )
            comp.series.set([series[s] for s in series_slugs if s in series])


def unseed(apps, schema_editor):
    apps.get_model("catalog", "Competition").objects.all().delete()
    Sport = apps.get_model("catalog", "Sport")
    Sport.objects.filter(slug="netball").delete()
    for old_slug, (new_name, new_slug) in SPORT_RELABEL.items():
        old_name = old_slug.upper()
        Sport.objects.filter(slug=new_slug).update(name=old_name, slug=old_slug)


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0003_series_origin"),
    ]

    operations = [
        # Migration 0003 renamed the Competition model to Series, but Postgres
        # kept the old auto-generated index/constraint names (catalog_competition_*)
        # on the series table. Free those names before re-creating a Competition
        # table, or CreateModel collides. Idempotent — skips anything already fixed.
        migrations.RunSQL(
            sql="""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='catalog_competition_pkey') THEN
                ALTER TABLE catalog_series RENAME CONSTRAINT catalog_competition_pkey TO catalog_series_pkey;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='catalog_competition_name_key') THEN
                ALTER TABLE catalog_series RENAME CONSTRAINT catalog_competition_name_key TO catalog_series_name_key;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='catalog_competition_slug_key') THEN
                ALTER TABLE catalog_series RENAME CONSTRAINT catalog_competition_slug_key TO catalog_series_slug_key;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_class WHERE relname='catalog_competition_name_ac1e3b5e_like' AND relkind='i') THEN
                ALTER INDEX catalog_competition_name_ac1e3b5e_like RENAME TO catalog_series_name_like;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_class WHERE relname='catalog_competition_slug_c86f16c8_like' AND relkind='i') THEN
                ALTER INDEX catalog_competition_slug_c86f16c8_like RENAME TO catalog_series_slug_like;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_class WHERE relname='catalog_competition_sport_id_b849eb8d' AND relkind='i') THEN
                ALTER INDEX catalog_competition_sport_id_b849eb8d RENAME TO catalog_series_sport_id;
              END IF;
            END $$;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.CreateModel(
            name="Competition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=50)),
                ("slug", models.SlugField(max_length=50)),
                ("season", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="competitions", to="catalog.season")),
                ("sport", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="competitions", to="catalog.sport")),
                ("series", models.ManyToManyField(blank=True, related_name="competitions", to="catalog.series")),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddConstraint(
            model_name="competition",
            constraint=models.UniqueConstraint(fields=["slug", "season"], name="uniq_competition_slug_per_season"),
        ),
        migrations.RunPython(seed_codes_and_competitions, unseed),
    ]
