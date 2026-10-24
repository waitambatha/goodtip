"""Seed the standing custom packages shown on the Plans page.

CLIENT, 19 OCT 2026: a handful of example packages "hard coded... but it
shouldn't come off as uh, test mode, or test design" — not labelled as a
demo, just a real-looking set of packages for local review before Ian
starts creating his own from the Plans admin hub.

Idempotent: matches on name, updates the rest, so re-running after an edit
here reshapes the four rather than duplicating them. Nothing is deleted —
delivered the same way billing.models.CustomPackage.is_active describes:
hide a package by unchecking it, from the admin, not by removing the row.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from billing.models import CustomPackage

PACKAGES = [
    {
        "name": "Schools Plan",
        "description": "For a single school running its own house or year-level comp.",
        "price_aud": Decimal("449"),
        "seat_limit": 250,
        "child_org_limit": 1,
        "sort_order": 1,
    },
    {
        "name": "Multi-Campus Education",
        "description": "One subscription across several campuses or year groups, each with its own ladder.",
        "price_aud": Decimal("1699"),
        "seat_limit": 1200,
        "child_org_limit": 8,
        "sort_order": 2,
    },
    {
        "name": "Franchise Network",
        "description": "A head office plan covering every branch or store, billed once.",
        "price_aud": Decimal("2499"),
        "seat_limit": 2000,
        "child_org_limit": 20,
        "sort_order": 3,
    },
    {
        "name": "Community & Not-for-Profit",
        "description": "A reduced rate for registered charities and community groups outside the five standard plans.",
        "price_aud": Decimal("349"),
        "seat_limit": 150,
        "child_org_limit": 1,
        "sort_order": 4,
    },
]


class Command(BaseCommand):
    help = "Seed or refresh the example custom packages offered on the Plans page."

    def handle(self, *args, **options):
        created = updated = 0
        for fields in PACKAGES:
            name = fields["name"]
            defaults = {k: v for k, v in fields.items() if k != "name"}
            defaults["is_active"] = True
            _obj, was_created = CustomPackage.objects.update_or_create(
                name=name, defaults=defaults,
            )
            created += was_created
            updated += not was_created
        self.stdout.write(self.style.SUCCESS(
            f"Custom packages: {created} created, {updated} updated."
        ))
