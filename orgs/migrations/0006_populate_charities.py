from django.db import migrations
from django.utils.text import slugify


# GoodTip-approved donation partners (from the customer-journey deck).
APPROVED_PARTNERS = [
    ("Gamblers Help", "https://gamblershelp.com.au"),
    ("Beyond Blue", "https://www.beyondblue.org.au"),
    ("headspace", "https://headspace.org.au"),
    ("ReachOut", "https://au.reachout.com"),
    ("Lifeline", "https://www.lifeline.org.au"),
]


def _unique_slug(Charity, name):
    base = slugify(name)[:200] or "charity"
    slug = base
    i = 2
    while Charity.objects.filter(slug=slug).exists():
        slug = f"{base}-{i}"
        i += 1
    return slug


def forwards(apps, schema_editor):
    Charity = apps.get_model("catalog", "Charity")
    Organisation = apps.get_model("orgs", "Organisation")

    # Seed the approved partner list.
    for name, url in APPROVED_PARTNERS:
        Charity.objects.get_or_create(
            name=name,
            defaults={"slug": _unique_slug(Charity, name), "website": url, "is_approved": True},
        )

    # Convert each league's free-text charity into a Charity row and link it.
    for org in Organisation.objects.all():
        name = (org.charity_name or "").strip()
        if not name:
            continue
        charity = Charity.objects.filter(name__iexact=name).first()
        if charity is None:
            charity = Charity.objects.create(
                name=name,
                slug=_unique_slug(Charity, name),
                website=(org.charity_url or "").strip(),
                is_approved=True,  # already in live use
            )
        org.charity = charity
        org.save(update_fields=["charity"])


def backwards(apps, schema_editor):
    Organisation = apps.get_model("orgs", "Organisation")
    for org in Organisation.objects.select_related("charity").all():
        if org.charity_id and not org.charity_name:
            org.charity_name = org.charity.name
            org.charity_url = org.charity.website
            org.save(update_fields=["charity_name", "charity_url"])


class Migration(migrations.Migration):

    dependencies = [
        ("orgs", "0005_organisation_charity"),
        ("catalog", "0002_charity"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
