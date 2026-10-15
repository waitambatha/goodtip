"""Copy real accounts, organisations, groups and the waiting list onto
staging, labelled 'real' (client, 14 Oct 2026: "we will use the staging
database as backup").

ONE-WAY, LIVE TO STAGING, NEVER THE REVERSE. This only ever runs on staging
(``_refuse_unless_staging``, same three-check pattern as scrub_for_staging),
and it only ever writes to staging's own 'default' connection — live is
opened read-only, under a second connection alias that exists for the
duration of this one process and nowhere else.

NOT IN .ENV, ON PURPOSE. Staging's own settings and `.env` never hold a
connection string to goodtip_db — CLAUDE.md calls that "the single most
dangerous line in either environment." LIVE_BACKUP_DATABASE_URL is read
straight from the process environment instead, set only in this command's
own systemd timer unit, so no other staging process — gunicorn, a shell,
another management command — ever sees it.

IDS NEVER CROSS THE WIRE UNCHANGED. Live's user #50 and staging's existing
user #50 are two unrelated people — both databases hand out ids
independently. Every synced row keeps its OWN staging-local id and records
live's id in `live_id` (goodtip.origin.OriginMixin), which is what this
matches on next time so a repeat run updates rather than duplicates, and
what the handful of relationships between these models (a Group's
Organisation, a membership's User) are remapped through.

BEST-EFFORT ON CATALOG LOOKUPS. An organisation's type, country, state,
competitions and charity are matched to staging's own catalog by natural key
(slug/code/year), not by id — the two databases do not promise matching
catalog ids. Where no match exists the field is simply left blank; nothing
here fails the whole sync over a decorative attribute. The three core facts
— who, which organisation, which group — always sync in full.
"""

import os

import dj_database_url
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, connections, transaction

from accounts.models import LaunchSignup, User
from catalog.models import Competition, Country, OrganisationType, State
from goodtip.origin import ORIGIN_LIVE
from orgs.models import Group, GroupMember, Organisation, OrgMember

LIVE_ALIAS = "live_backup_source"
PRODUCTION_DB_NAMES = {"goodtip_db"}


class Command(BaseCommand):
    help = "Copy real (live) accounts, orgs, groups and the waiting list onto staging. Staging only."

    def handle(self, *args, **options):
        self._refuse_unless_staging()
        self._open_live_connection()
        try:
            counts = {}
            with transaction.atomic():
                counts["organisations"] = self._sync_organisations()
                counts["users"] = self._sync_users()
                counts["groups"] = self._sync_groups()
                counts["org memberships"] = self._sync_org_members()
                counts["group memberships"] = self._sync_group_members()
                counts["waiting list"] = self._sync_launch_signups()
        finally:
            self._close_live_connection()

        width = max(len(name) for name in counts)
        for name, n in counts.items():
            self.stdout.write(f"  {name.ljust(width)}  {n:>6,}")

    # ---- safety -------------------------------------------------------

    def _refuse_unless_staging(self):
        """Three independent reasons to stop, same shape as scrub_for_staging's
        — one check alone has a plausible way of being wrong."""
        if not getattr(settings, "IS_STAGING", False):
            raise CommandError(
                "GOODTIP_ENV is not 'staging' — this only ever copies "
                "live onto staging, never the other way, and will not run anywhere else."
            )
        if connection.settings_dict.get("NAME") in PRODUCTION_DB_NAMES:
            raise CommandError(
                "Refusing: this process's own database is named goodtip_db — "
                "that is live's name, and this command must never write there."
            )
        if not os.environ.get("LIVE_BACKUP_DATABASE_URL"):
            raise CommandError(
                "LIVE_BACKUP_DATABASE_URL is not set in this process's environment "
                "— refusing to guess at a connection to live. Set it on this "
                "command's own systemd unit, not in .env."
            )

    def _open_live_connection(self):
        live_url = os.environ["LIVE_BACKUP_DATABASE_URL"]
        # dj_database_url.parse(), NOT .config() — .config() reads
        # os.environ["DATABASE_URL"] itself and only falls back to its
        # `default=` argument when that var is ABSENT. It is always present
        # (staging's own connection string), so .config(default=live_url)
        # silently opened staging's own database under this alias instead
        # of live's — caught by the check below, which is why it exists.
        config = dj_database_url.parse(live_url, conn_max_age=0)
        config["OPTIONS"] = {**config.get("OPTIONS", {}), "options": "-c default_transaction_read_only=on"}
        # ConnectionHandler.configure_settings fills in these defaults for
        # every alias declared in settings.DATABASES, the one time it runs —
        # an alias added directly to connections.databases afterwards, like
        # this one, never goes through it, and Django's own connection code
        # assumes TIME_ZONE in particular is always a key, not just a value.
        config.setdefault("ATOMIC_REQUESTS", False)
        config.setdefault("AUTOCOMMIT", True)
        config.setdefault("CONN_HEALTH_CHECKS", False)
        config.setdefault("TIME_ZONE", None)
        config.setdefault("TEST", {})
        connections.databases[LIVE_ALIAS] = config
        if connections[LIVE_ALIAS].settings_dict.get("NAME") not in PRODUCTION_DB_NAMES:
            self._close_live_connection()
            raise CommandError(
                "LIVE_BACKUP_DATABASE_URL does not point at a database named "
                "goodtip_db — refusing, in case this is pointed at staging's own "
                "database by mistake and about to copy it onto itself."
            )

    def _close_live_connection(self):
        if LIVE_ALIAS in connections.databases:
            connections[LIVE_ALIAS].close()
            del connections.databases[LIVE_ALIAS]

    # ---- catalog matching (best-effort, by natural key) ----------------

    @staticmethod
    def _match(model_cls, **kwargs):
        if any(v is None for v in kwargs.values()):
            return None
        return model_cls.objects.filter(**kwargs).first()

    def _match_organisation_type(self, live_type):
        if not live_type:
            return None
        return self._match(OrganisationType, slug=live_type.slug)

    def _match_country(self, live_country):
        if not live_country:
            return None
        return self._match(Country, code=live_country.code)

    def _match_state(self, live_state):
        if not live_state:
            return None
        return self._match(State, code=live_state.code)

    def _match_competition(self, live_competition):
        if not live_competition:
            return None
        return self._match(Competition, slug=live_competition.slug)

    def _match_charity(self, live_charity):
        if not live_charity:
            return None
        # Charity data itself is out of scope for this backup (not one of the
        # four things asked for) — matched only against staging's existing
        # catalog, never created here.
        from catalog.models import Charity
        return self._match(Charity, slug=live_charity.slug)

    def _local_user(self, live_user):
        if not live_user:
            return None
        return User.objects.filter(live_id=live_user.id).first()

    # ---- the six syncs, in dependency order -----------------------------

    def _sync_organisations(self):
        n = 0
        # Parent is deliberately not set in this first pass at all (see the
        # second pass below), so fetch order here doesn't matter — nothing
        # depends on a parent already existing locally yet.
        rows = list(Organisation.objects.using(LIVE_ALIAS).select_related(
            "organisation_type", "country", "state", "season", "charity",
            "created_by", "approved_by", "terms_accepted_by", "public_consent_by",
        ))
        # Season is a required FK (PROTECT, not nullable) — an org whose
        # season doesn't exist on staging's catalog cannot be saved at all,
        # so it's skipped and reported rather than crashing the whole sync.
        from catalog.models import Season
        for row in rows:
            season = self._match(Season, year=row.season.year) if row.season_id else None
            if row.season_id and season is None:
                self.stderr.write(
                    f"skipping organisation live_id={row.id} ({row.name!r}): "
                    f"no Season {row.season.year} on staging's catalog"
                )
                continue
            org, _ = Organisation.objects.update_or_create(
                live_id=row.id,
                defaults=dict(
                    origin=ORIGIN_LIVE,
                    organisation_type=self._match_organisation_type(row.organisation_type),
                    country=self._match_country(row.country),
                    state=self._match_state(row.state),
                    season=season,
                    charity=self._match_charity(row.charity),
                    created_by=self._local_user(row.created_by),
                    approved_by=self._local_user(row.approved_by),
                    terms_accepted_by=self._local_user(row.terms_accepted_by),
                    public_consent_by=self._local_user(row.public_consent_by),
                    **self._plain_fields(row, Organisation, skip={
                        "id", "origin", "live_id", "parent", "organisation_type",
                        "country", "state", "season", "charity", "created_by",
                        "approved_by", "terms_accepted_by", "public_consent_by",
                    }),
                ),
            )
            n += 1
            # sub_categories / competitions: M2M, best-effort by slug.
            org.sub_categories.set(
                sc for sc in (
                    self._match(row.sub_categories.model, organisation_type__slug=s.organisation_type.slug, slug=s.slug)
                    for s in row.sub_categories.all()
                ) if sc
            )
            org.competitions.set(
                c for c in (self._match_competition(c) for c in row.competitions.all()) if c
            )
        # Second pass: parents now exist locally, so remap self-FK.
        for row in rows:
            if not row.parent_id:
                continue
            local = Organisation.objects.filter(live_id=row.id).first()
            parent = Organisation.objects.filter(live_id=row.parent_id).first()
            if local and parent and local.parent_id != parent.id:
                local.parent = parent
                local.save(update_fields=["parent"])
        return n

    def _sync_users(self):
        # email is globally unique, and staging's database already HAD a
        # straight clone of live's users before this feature existed — every
        # one of them sits here right now with a matching email and no
        # live_id yet. Matching on live_id alone would miss every one of
        # those and create a second row for the same person, which then
        # collides on email's own uniqueness. Matching on email first adopts
        # the existing row (sets its live_id, promotes it to real) instead.
        n = 0
        skip = {"id", "origin", "live_id", "password", "last_login", "groups", "user_permissions"}
        for row in User.objects.using(LIVE_ALIAS).all():
            local = User.objects.filter(live_id=row.id).first() or User.objects.filter(email=row.email).first()
            fields = dict(origin=ORIGIN_LIVE, live_id=row.id, password=row.password,
                          **self._plain_fields(row, User, skip=skip))
            if local:
                for k, v in fields.items():
                    setattr(local, k, v)
                local.save()
            else:
                User.objects.create(**fields)
            n += 1
        return n

    def _sync_groups(self):
        n = 0
        from catalog.models import GroupType
        for row in Group.objects.using(LIVE_ALIAS).select_related(
            "org", "kind", "country", "charity", "created_by", "approved_by",
        ):
            org = Organisation.objects.filter(live_id=row.org_id).first()
            if org is None:
                self.stderr.write(
                    f"skipping group live_id={row.id} ({row.name!r}): "
                    f"its organisation (live_id={row.org_id}) did not sync"
                )
                continue
            kind = self._match(GroupType, slug=row.kind.slug) if row.kind_id else None
            Group.objects.update_or_create(
                live_id=row.id,
                defaults=dict(
                    origin=ORIGIN_LIVE,
                    org=org,
                    kind=kind,
                    country=self._match_country(row.country),
                    charity=self._match_charity(row.charity),
                    created_by=self._local_user(row.created_by),
                    approved_by=self._local_user(row.approved_by),
                    **self._plain_fields(row, Group, skip={
                        "id", "origin", "live_id", "org", "kind", "country",
                        "charity", "created_by", "approved_by",
                    }),
                ),
            )
            n += 1
        return n

    def _sync_org_members(self):
        n = 0
        for row in OrgMember.objects.using(LIVE_ALIAS).select_related("user", "org", "invited_by"):
            user = self._local_user(row.user)
            org = Organisation.objects.filter(live_id=row.org_id).first()
            if user is None or org is None:
                continue
            OrgMember.objects.update_or_create(
                live_id=row.id,
                defaults=dict(
                    origin=ORIGIN_LIVE,
                    user=user,
                    org=org,
                    invited_by=self._local_user(row.invited_by),
                    **self._plain_fields(row, OrgMember, skip={
                        "id", "origin", "live_id", "user", "org", "invited_by",
                    }),
                ),
            )
            n += 1
        return n

    def _sync_group_members(self):
        n = 0
        for row in GroupMember.objects.using(LIVE_ALIAS).select_related("user", "group"):
            user = self._local_user(row.user)
            group = Group.objects.filter(live_id=row.group_id).first()
            if user is None or group is None:
                continue
            GroupMember.objects.update_or_create(
                live_id=row.id,
                defaults=dict(
                    origin=ORIGIN_LIVE,
                    user=user,
                    group=group,
                    **self._plain_fields(row, GroupMember, skip={
                        "id", "origin", "live_id", "user", "group",
                    }),
                ),
            )
            n += 1
        return n

    def _sync_launch_signups(self):
        # Same reasoning as _sync_users: email is unique, and the handful
        # already copied onto staging by hand before this command existed
        # have no live_id yet — match on email too, or they'd duplicate and
        # then collide on that same uniqueness.
        n = 0
        skip = {"id", "origin", "live_id"}
        for row in LaunchSignup.objects.using(LIVE_ALIAS).all():
            local = (
                LaunchSignup.objects.filter(live_id=row.id).first()
                or LaunchSignup.objects.filter(email=row.email).first()
            )
            fields = dict(origin=ORIGIN_LIVE, live_id=row.id, **self._plain_fields(row, LaunchSignup, skip=skip))
            if local:
                for k, v in fields.items():
                    setattr(local, k, v)
                local.save()
            else:
                LaunchSignup.objects.create(**fields)
            n += 1
        return n

    # ---- shared ---------------------------------------------------------

    @staticmethod
    def _plain_fields(row, model_cls, skip):
        """Every concrete, non-relational field on `row` except `skip`."""
        out = {}
        for f in model_cls._meta.concrete_fields:
            if f.name in skip or f.is_relation:
                continue
            out[f.name] = getattr(row, f.attname)
        return out
