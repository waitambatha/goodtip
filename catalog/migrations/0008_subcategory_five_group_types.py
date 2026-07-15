"""Organisation categories restructure (Ian's categories doc, 7 Jul 2026).

Adds the ``SubCategory`` lookup (sub-categories scoped to their organisation
type) and reshapes ``GroupType`` from the old two-way workplace/community split
into the five spec'd types: Community, Business, Education, Charities,
Informal — in that display order.

The old ``workplace`` row is repurposed in place into ``business`` so every
existing workplace org lands under Business without touching the orgs table.
The old ``community`` row is kept (renamed "Community group" → "Community").
"""
from django.db import migrations, models
import django.db.models.deletion


# (slug, name, sort_order) — order matters for the sign-up dropdown and the
# Good List (categories doc: "Order matters").
GROUP_TYPES = [
    ("community", "Community", 0),
    ("business", "Business", 1),
    ("education", "Education", 2),
    ("charities", "Charities", 3),
    ("informal", "Informal", 4),
]

# type slug -> [(slug, name), ...] in display order. Charities and Informal
# deliberately have none (Informal self-describes on the org record).
SUB_CATEGORIES = {
    "community": [
        ("sports-club", "Sports Club"),
        ("social-club", "Social Club"),
        ("workplace-team", "Workplace Team"),
        ("scout-group", "Scout Group"),
        ("other", "Other"),
    ],
    "business": [
        ("finance", "Finance"),
        ("tech", "Tech"),
        ("retail", "Retail"),
        ("manufacturing", "Manufacturing"),
        ("professional-services", "Professional Services"),
        ("networking-groups", "Networking Groups"),
        ("other", "Other"),
    ],
    "education": [
        ("primary-school", "Primary School"),
        ("secondary-school", "Secondary School"),
        ("university", "University"),
        ("tafe", "TAFE"),
        ("other", "Other"),
    ],
}


def seed(apps, schema_editor):
    GroupType = apps.get_model("catalog", "GroupType")
    SubCategory = apps.get_model("catalog", "SubCategory")

    # Repurpose the old "workplace" row into "business" in place, so existing
    # workplace orgs (FK by id) are remapped for free.
    workplace = GroupType.objects.filter(slug="workplace").first()
    if workplace is not None and not GroupType.objects.filter(slug="business").exists():
        workplace.slug = "business"
        workplace.name = "Business"
        workplace.save(update_fields=["slug", "name"])

    for slug, name, order in GROUP_TYPES:
        gt = GroupType.objects.filter(slug=slug).first()
        if gt is None:
            # Name clashes (unique) are possible on odd datasets; fall back to name.
            gt = GroupType.objects.filter(name=name).first()
        if gt is None:
            GroupType.objects.create(slug=slug, name=name, sort_order=order)
        else:
            gt.slug, gt.name, gt.sort_order = slug, name, order
            gt.save(update_fields=["slug", "name", "sort_order"])

    for type_slug, subs in SUB_CATEGORIES.items():
        gt = GroupType.objects.get(slug=type_slug)
        for order, (slug, name) in enumerate(subs):
            SubCategory.objects.update_or_create(
                group_type=gt, slug=slug,
                defaults={"name": name, "sort_order": order},
            )


def unseed(apps, schema_editor):
    # SubCategory rows go with the table; GroupType reshaping is not reversed
    # (no way to know which Business orgs were originally "workplace").
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0007_seed_goodlist_data"),
    ]

    operations = [
        migrations.CreateModel(
            name="SubCategory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=80)),
                ("slug", models.SlugField(max_length=80)),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                ("group_type", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sub_categories", to="catalog.grouptype")),
            ],
            options={
                "ordering": ["group_type__sort_order", "sort_order", "name"],
                "verbose_name_plural": "sub-categories",
            },
        ),
        migrations.AddConstraint(
            model_name="subcategory",
            constraint=models.UniqueConstraint(fields=("group_type", "slug"), name="uniq_subcategory_slug_per_type"),
        ),
        migrations.AddConstraint(
            model_name="subcategory",
            constraint=models.UniqueConstraint(fields=("group_type", "name"), name="uniq_subcategory_name_per_type"),
        ),
        migrations.RunPython(seed, unseed),
    ]
