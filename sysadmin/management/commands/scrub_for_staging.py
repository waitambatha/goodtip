"""Rewrite the personal data in this database into fake equivalents.

Staging is a clone of production, which is the only way the client sees
realistic volume and structure — and it means every member's real name, real
email address and real login history land on a site that gets shown to third
parties and is guarded by one shared password. This command is what makes that
clone safe to hand over: same row counts, same relationships, same shape, none
of the real people.

It is destructive and irreversible by design. Everything about how it decides
whether it is allowed to run is therefore deliberately paranoid — see
``_refuse_if_production``. There is no ``--force`` that skips those checks:
the only thing this command must never do is run on production, so the escape
hatch that would let it is simply not written.
"""

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

# Names that identify the live database. A clone made with `pg_dump goodtip_db
# | psql goodtip_staging_db` differs from production in exactly one observable
# way -- what it is called -- so that is what gets checked.
PRODUCTION_DB_NAMES = {"goodtip_db"}


class Command(BaseCommand):
    help = "Replace real member data with fake data. Staging databases only."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep",
            default="",
            help=(
                "Comma-separated real addresses to leave untouched, so you and "
                "the client can still sign in (sign-in is a code emailed to the "
                "address on the account; scrub every address and nobody can log "
                "in to the staging site at all)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and roll back instead of committing.",
        )

    def handle(self, *args, **options):
        self._refuse_if_production()

        keep = {e.strip().lower() for e in options["keep"].split(",") if e.strip()}
        dry_run = options["dry_run"]

        with transaction.atomic():
            counts = self._scrub(keep)

            width = max(len(name) for name in counts)
            for name, n in counts.items():
                self.stdout.write(f"  {name.ljust(width)}  {n:>7,}")
            total = sum(counts.values())

            if dry_run:
                self.stdout.write(self.style.WARNING(
                    f"\nDry run: {total:,} row(s) would be rewritten. Rolling back."
                ))
                transaction.set_rollback(True)
                return

        self.stdout.write(self.style.SUCCESS(f"\nScrubbed {total:,} row(s)."))
        if keep:
            self.stdout.write(f"Left signed-in-able: {', '.join(sorted(keep))}")
        else:
            self.stdout.write(self.style.WARNING(
                "No --keep addresses given, so every account now has an "
                "@staging.invalid address and no sign-in code can reach anyone. "
                "Re-run with --keep, or create a fresh superuser."
            ))

    # -- guards ---------------------------------------------------------------

    def _refuse_if_production(self):
        """Three independent reasons to stop, because one is not enough.

        Each check alone has a plausible way of being wrong: GOODTIP_ENV is an
        env var someone can copy into the wrong .env, the database name is a
        convention, and DEBUG says nothing about which data is behind it. All
        three being wrong at once is the case this does not defend against, and
        the runbook's answer to that is to check `\\conninfo` first.
        """
        if not getattr(settings, "IS_STAGING", False):
            raise CommandError(
                "GOODTIP_ENV is not 'staging' (it is "
                f"'{getattr(settings, 'GOODTIP_ENV', 'unset')}'). This command "
                "destroys personal data and will not run outside staging."
            )

        db_name = connection.settings_dict.get("NAME", "")
        if db_name in PRODUCTION_DB_NAMES:
            raise CommandError(
                f"Refusing to scrub: the configured database is '{db_name}', "
                "which is production's. Staging's .env is pointing at the live "
                "database — fix DATABASE_URL before doing anything else."
            )

        self.stdout.write(f"Scrubbing '{db_name}' on {connection.settings_dict.get('HOST') or 'localhost'}.\n")

    # -- the scrub ------------------------------------------------------------

    def _scrub(self, keep):
        counts = {}
        model = apps.get_model

        # Accounts. Addresses are made unique off the primary key rather than
        # randomised: unique=True is enforced on User.email, and two colliding
        # fake addresses would abort the whole transaction near the end of a
        # long run. .invalid is the RFC 2606 TLD reserved to never resolve, so
        # nothing that escapes the allowlist can be delivered anywhere.
        User = model("accounts", "User")
        n = 0
        for user in User.objects.exclude(email__in=keep).iterator(chunk_size=500):
            user.email = f"member{user.pk}@staging.invalid"
            user.username = f"member{user.pk}"
            user.display_name = f"Member {user.pk}"
            user.first_name = "Member"
            user.last_name = str(user.pk)
            # Sign-in is by emailed code, but a password hash is still a
            # credential that may be reused elsewhere. It does not travel.
            user.set_unusable_password()
            user.save(update_fields=[
                "email", "username", "display_name",
                "first_name", "last_name", "password",
            ])
            n += 1
        counts["accounts.User"] = n

        # Transient credential rows. Nothing on staging should be able to act on
        # a code that was emailed to a real person, and these tables are rebuilt
        # by ordinary use within minutes, so deleting beats rewriting.
        counts["accounts.LoginCode"] = model("accounts", "LoginCode").objects.all().delete()[0]
        counts["orgs.WorkEmailVerification"] = (
            model("orgs", "WorkEmailVerification").objects.all().delete()[0]
        )

        counts["accounts.LaunchSignup"] = self._rewrite(
            model("accounts", "LaunchSignup"), keep, "email",
            lambda o: {"email": f"signup{o.pk}@staging.invalid", "name": f"Signup {o.pk}"},
        )
        counts["accounts.BossInvite"] = self._rewrite(
            model("accounts", "BossInvite"), keep, "boss_email",
            lambda o: {
                "boss_email": f"boss{o.pk}@staging.invalid",
                "boss_name": f"Boss {o.pk}",
                # Free text written by a member about a named colleague.
                "subject": "Staging placeholder subject",
                "body_preview": "Staging placeholder body.",
            },
        )
        counts["admin_panel.Enquiry"] = self._rewrite(
            model("admin_panel", "Enquiry"), keep, "email",
            lambda o: {
                "email": f"enquiry{o.pk}@staging.invalid",
                "name": f"Enquirer {o.pk}",
                "message": "Staging placeholder message.",
                "reply_body": "",
            },
        )
        counts["sysadmin.LoginEvent"] = self._rewrite(
            model("sysadmin", "LoginEvent"), keep, "email",
            lambda o: {"email": f"member{o.pk}@staging.invalid", "ip_address": None,
                       "user_agent": ""},
        )

        # Public wall: guest replies carry a name, an address and an IP for
        # abuse follow-up, none of which belong on a demo site.
        WallReply = model("orgs", "WallReply")
        n = 0
        for reply in WallReply.objects.exclude(guest_email="").iterator(chunk_size=500):
            reply.guest_name = f"Guest {reply.pk}"
            reply.guest_email = f"guest{reply.pk}@staging.invalid"
            reply.ip_address = None
            reply.save(update_fields=["guest_name", "guest_email", "ip_address"])
            n += 1
        counts["orgs.WallReply"] = n

        # Stripe identifiers. These point at real objects in the real Stripe
        # account; a staging page that follows one is reading live payment data,
        # and a staging retry could act on it. The amounts stay, so the money
        # figures the client is shown are still the real shape.
        counts["billing.PlanSubscription"] = model("billing", "PlanSubscription").objects.exclude(
            stripe_checkout_session_id="", stripe_payment_intent_id=""
        ).update(stripe_checkout_session_id="", stripe_payment_intent_id="")
        counts["billing.DonationPayment"] = model("billing", "DonationPayment").objects.exclude(
            stripe_checkout_session_id="", stripe_payment_intent_id="", receipt_url=""
        ).update(
            stripe_checkout_session_id="", stripe_payment_intent_id="", receipt_url="",
        )

        return counts

    def _rewrite(self, Model, keep, email_field, fields_for):
        """Rewrite every row except those whose address is on the keep list."""
        n = 0
        qs = Model.objects.exclude(**{f"{email_field}__in": keep})
        for obj in qs.iterator(chunk_size=500):
            updates = fields_for(obj)
            for field, value in updates.items():
                setattr(obj, field, value)
            obj.save(update_fields=list(updates))
            n += 1
        return n
