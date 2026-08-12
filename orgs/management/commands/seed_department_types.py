"""Seed the department picker.

Idempotent: matches on (group_type, slug) and updates the name and ordering,
so re-running after an edit here reshapes the list rather than duplicating it.
Nothing is ever deleted, because a department already created against a row
would lose its type.

The list is deliberately short. It exists so the common case is one tap, not
so it covers every department in the country. Anything missing is typed into
the free-text field beside it and lives on the org as ``department_label``.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import DepartmentType, GroupType


# group_type slug (None = offered to everyone) -> [(name, sort_order), ...]
DEPARTMENTS = {
    None: [
        "IT", "Finance", "People & Culture", "Operations", "Sales",
        "Marketing", "Legal", "Customer Service", "Admin", "Executive",
    ],
    GroupType.SLUG_BUSINESS: [
        "Engineering", "Product", "Risk & Compliance", "Procurement",
        "Data & Analytics", "Field Services", "Warehouse", "Retail Floor",
        "Call Centre", "Branch Network",
    ],
    GroupType.SLUG_EDUCATION: [
        "Teaching Staff", "Support Staff", "Faculty", "Student Services",
        "Grounds & Maintenance", "Year Level",
    ],
    GroupType.SLUG_COMMUNITY: [
        "Committee", "Volunteers", "Coaching Staff", "Juniors", "Seniors",
        "Social Club",
    ],
    GroupType.SLUG_CHARITIES: [
        "Fundraising", "Programs", "Volunteers", "Partnerships",
    ],
}


def _slugify(name: str) -> str:
    return name.lower().replace("&", "and").replace(" ", "-").replace("--", "-")


class Command(BaseCommand):
    help = "Seed or refresh the department types offered when creating a department."

    @transaction.atomic
    def handle(self, *args, **options):
        types_by_slug = {g.slug: g for g in GroupType.objects.all()}
        created = updated = skipped = 0

        for gt_slug, names in DEPARTMENTS.items():
            group_type = None
            if gt_slug is not None:
                group_type = types_by_slug.get(gt_slug)
                if group_type is None:
                    # The GroupType table is seeded elsewhere; if a type is not
                    # there yet, skip its departments rather than invent one.
                    skipped += len(names)
                    self.stdout.write(
                        self.style.WARNING(f"  no GroupType '{gt_slug}', skipped {len(names)}")
                    )
                    continue

            for order, name in enumerate(names, start=1):
                obj, was_created = DepartmentType.objects.update_or_create(
                    group_type=group_type,
                    slug=_slugify(name),
                    defaults={"name": name, "sort_order": order, "is_active": True},
                )
                created += was_created
                updated += not was_created

        self.stdout.write(self.style.SUCCESS(
            f"Department types: {created} created, {updated} updated, {skipped} skipped."
        ))
