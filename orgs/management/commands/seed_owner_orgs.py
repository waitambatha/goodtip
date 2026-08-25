from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from catalog.models import Charity, GroupType, OrganisationType, State, SubCategory
from orgs.models import Group, OrgCharitySelection, OrgMember, Organisation
from orgs.services import (
    add_member,
    create_charity_election,
    create_group,
    join_group,
    schedule_charity_election,
    set_org_charity,
)

User = get_user_model()

# Accounts that already exist in this DB, orgless, clearly meant as this
# owner's own staff (a masterclass.co.ke domain) or otherwise personal-looking
# addresses created ad hoc and never finished — reused before creating
# anything new. Two obviously-unrelated test accounts (ui-review@, test22@)
# are deliberately left alone.
REUSABLE_ORGLESS_EMAILS = [
    "daniel5mbatha@gmail.com",
    "erickwaita6@gmail.com",
    "mail2ambrose@gmail.com",
    "benkim388@gmail.com",
    "mosesilla651@gmail.com",
    "ewaita5b@gmail.com",
]
# Clearly-fake addresses, matching the @goodtip.example convention already
# used by tipping/management/commands/seed_demo.py.
NEW_STAFF = [
    ("Priya Nair", "priya.nair@goodtip.demo"),
    ("Marcus Webb", "marcus.webb@goodtip.demo"),
    ("Chloe Dawson", "chloe.dawson@goodtip.demo"),
    ("Jordan Pak", "jordan.pak@goodtip.demo"),
    ("Fatima Haidari", "fatima.haidari@goodtip.demo"),
    ("Liam O'Rourke", "liam.orourke@goodtip.demo"),
    ("Grace Okafor", "grace.okafor@goodtip.demo"),
    ("Tane Ropata", "tane.ropata@goodtip.demo"),
]


class Command(BaseCommand):
    help = (
        "Seed 4 additional organisations (created_by the given owner), a "
        "handful of groups, and a spread of other users as members/staff — "
        "purely additive, never touches the owner's existing organisation(s). "
        "Safe to re-run: everything is get_or_create'd."
    )

    def add_arguments(self, parser):
        parser.add_argument("--owner-email", default="waitaerick5@gmail.com")

    def handle(self, *args, **options):
        try:
            owner = User.objects.get(email__iexact=options["owner_email"])
        except User.DoesNotExist:
            raise CommandError(
                f"No user with email {options['owner_email']!r} — this seeds data "
                "for a real, already-existing account, not a fresh signup."
            )

        self.masterclass = Organisation.objects.filter(name="masterclass", created_by=owner).first()
        if self.masterclass is None:
            raise CommandError(
                f"No 'masterclass' organisation created by {owner.email!r} was found — "
                "this command reuses its season/competitions for the new orgs."
            )

        with transaction.atomic():
            self._staff_masterclass(owner)
            staff_pool = self._get_or_create_staff()
            pool = iter(staff_pool)
            self._make_aquaflow(owner, pool)
            self._make_realty(owner, pool)
            self._make_retail(owner, pool)
            self._make_facilities(owner, pool)

        self.stdout.write(self.style.SUCCESS(
            f"Done — {owner.email} now has "
            f"{OrgMember.objects.filter(user=owner).count()} organisation memberships."
        ))

    # ---- shared helpers ----------------------------------------------------

    def _get_or_create_staff(self):
        users = []
        for email in REUSABLE_ORGLESS_EMAILS:
            u = User.objects.filter(email__iexact=email).first()
            if u:
                users.append(u)
        for display_name, email in NEW_STAFF:
            u, created = User.objects.get_or_create(
                email=email,
                defaults={"display_name": display_name, "username": email, "is_active": True},
            )
            if created:
                u.set_unusable_password()
                u.save(update_fields=["password"])
            users.append(u)
        return users

    def _sub_category(self, name):
        return SubCategory.objects.filter(
            organisation_type_id=OrganisationType.objects.get(name="Business").id,
            name__iexact=name,
        ).first()

    def _group_type(self, name):
        return GroupType.objects.filter(name__iexact=name).first()

    def _staff_masterclass(self, owner):
        """The three masterclass.co.ke accounts were clearly meant as this
        org's own Finance/IT staff and never added — finish that."""
        finance = Group.objects.filter(org=self.masterclass, name="Finance").first()
        it = Group.objects.filter(org=self.masterclass, name="IT").first()
        assignments = [
            ("robert.kimaiyo@masterclass.co.ke", finance),
            ("tim.mutai@masterclass.co.ke", it),
            ("tali@masterclass.co.ke", finance),
        ]
        for email, group in assignments:
            u = User.objects.filter(email__iexact=email).first()
            if u is None:
                continue
            add_member(u, self.masterclass, role=OrgMember.ROLE_PARTICIPANT)
            if group is not None:
                try:
                    join_group(group, user=u)
                except ValueError:
                    pass

    def _create_org(self, *, owner, name, sub_category_name, state_name, groups_enabled):
        org, created = Organisation.objects.get_or_create(
            name=name,
            defaults={
                "created_by": owner,
                "organisation_type": OrganisationType.objects.get(name="Business"),
                "state": State.objects.get(name=state_name),
                "season_id": self.masterclass.season_id,
                "groups_enabled": groups_enabled,
            },
        )
        if created:
            sub_cat = self._sub_category(sub_category_name)
            if sub_cat:
                org.sub_categories.add(sub_cat)
            org.competitions.set(self.masterclass.competitions.all())
        OrgMember.objects.get_or_create(
            user=owner, org=org,
            defaults={"role": OrgMember.ROLE_BOTH, "is_league_owner": True},
        )
        return org

    def _add_group(self, org, owner, name, *, kind_name=None, label=""):
        existing = Group.objects.filter(org=org, name=name).first()
        if existing:
            return existing
        return create_group(
            org, name=name, by_user=owner,
            kind=self._group_type(kind_name) if kind_name else None,
            label=label,
        )

    def _staff_up(self, org, pool, *, manager_count, participant_count, groups=None):
        """Pull `manager_count` + `participant_count` users off the shared
        pool iterator and add them to `org` — never the creator, never
        is_league_owner, so there is always a real non-creator manager to
        exercise the creator-only Manage scoping against."""
        groups = groups or []
        added = []
        for _ in range(manager_count):
            u = next(pool, None)
            if u is None:
                break
            m = add_member(u, org, role=OrgMember.ROLE_MANAGER)
            added.append((u, m))
        for _ in range(participant_count):
            u = next(pool, None)
            if u is None:
                break
            m = add_member(u, org, role=OrgMember.ROLE_PARTICIPANT)
            added.append((u, m))
        if groups:
            for i, (u, _m) in enumerate(added):
                group = groups[i % len(groups)]
                try:
                    join_group(group, user=u)
                except ValueError:
                    pass

    # ---- the 4 orgs ----------------------------------------------------

    def _make_aquaflow(self, owner, pool):
        org = self._create_org(
            owner=owner, name="AquaFlow Water Co",
            sub_category_name="Manufacturing", state_name="Western Australia",
            groups_enabled=True,
        )
        groups = [
            self._add_group(org, owner, "Operations", kind_name="Operations"),
            self._add_group(org, owner, "Fleet & Logistics", kind_name="Field Services"),
            self._add_group(org, owner, "Customer Service", kind_name="Customer Service"),
            self._add_group(org, owner, "Finance", kind_name="Finance"),
        ]
        self._staff_up(org, pool, manager_count=2, participant_count=2, groups=groups)
        if org.charity_id is None:
            set_org_charity(org, Charity.objects.get(name="Red Cross"), source=OrgCharitySelection.SOURCE_INITIAL)

    def _make_realty(self, owner, pool):
        org = self._create_org(
            owner=owner, name="Waita Realty Group",
            sub_category_name="Professional Services", state_name="Victoria",
            groups_enabled=True,
        )
        groups = [
            self._add_group(org, owner, "Sales", kind_name="Sales"),
            self._add_group(org, owner, "Property Management", label="Property Management"),
        ]
        self._staff_up(org, pool, manager_count=1, participant_count=2, groups=groups)
        if not org.charity_votes.exists() and org.charity_id is None:
            candidates = list(Charity.objects.filter(name__in=["Beyond Blue", "headspace", "Lifeline"]))
            if candidates:
                vote = create_charity_election(org, candidates)
                schedule_charity_election(vote, when=timezone.now())

    def _make_retail(self, owner, pool):
        org = self._create_org(
            owner=owner, name="Northgate Retail Co",
            sub_category_name="Retail", state_name="Queensland",
            groups_enabled=False,
        )
        self._staff_up(org, pool, manager_count=1, participant_count=1)
        if org.charity_id is None:
            set_org_charity(org, Charity.objects.get(name="Lifeline"), source=OrgCharitySelection.SOURCE_INITIAL)

    def _make_facilities(self, owner, pool):
        org = self._create_org(
            owner=owner, name="Coastal Facilities Management",
            sub_category_name="Professional Services", state_name="New South Wales",
            groups_enabled=True,
        )
        groups = [
            self._add_group(org, owner, "Maintenance", label="Maintenance"),
            self._add_group(org, owner, "Admin", kind_name="Admin"),
        ]
        self._staff_up(org, pool, manager_count=1, participant_count=2, groups=groups)
        if org.charity_id is None:
            set_org_charity(org, Charity.objects.get(name="headspace"), source=OrgCharitySelection.SOURCE_INITIAL)
