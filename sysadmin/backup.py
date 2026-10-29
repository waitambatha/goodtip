"""The two databases: what is REAL, what is BACKUP, what is TESTING, and who
may delete which (client, 25 Oct 2026).

  LIVE      goodtip.com.au's own database. Everything in it is REAL.
  STAGING   the team's database. Two kinds of row in every table:
              BACKUP   a copy of a live row, made by the backup job
              TESTING  made on staging itself

The staging site never connects to live (CLAUDE.md: the most dangerous line
in either environment). So on staging's admin, the "Live" database is read
from the backup — staging's BACKUP rows, as of the last run of the backup
job — and anything that has to happen ON live (an approved deletion) is
queued here and carried out by that job, which is the one process allowed a
connection to live.

WHO MAY DELETE
  TESTING on staging   any administrator who could delete it anyway
  BACKUP on staging    only Ian; anyone else asks him (DeletionRequest)
  anything on live     only Ian; anyone else asks him; carried out by the job
"Ian" is whoever holds AdminAccess.can_delete_real (sysadmin.access).

HOW EVERY TABLE IS BACKED UP (copy_everything)
Six models carry their own origin label and are copied by sync_live_backup's
own hand-written steps. Every other table is copied generically here:
  * REFERENCE tables (sports, seasons, competitions, teams, charities...)
    are loaded on both sides from the feeds, so they are never copied — a
    live row is MATCHED to staging's by its natural key (slug, code, year)
    and foreign keys pointing at it are translated through that match.
  * SKIPPED tables are environment-local: admin internals, sync logs,
    sign-in codes, the match predictor, and the blog (which has its own
    two-way sync).
  * Everything else is COPIED: each live row becomes a staging row with its
    own staging id, recorded in BackupRecord, foreign keys remapped through
    the rows already copied. Rows are written with bulk_create/update, so no
    save signal fires — copying a live Wall post must never notify anyone
    from staging — and live's own timestamps are kept, not stamped "now".
  * Only columns that exist on live are read (main never gets staging's
    migrations), and a table that does not exist on live is skipped.
  * Deleting on live never deletes the backup. That is the point of it.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from django.apps import apps
from django.db import IntegrityError, connections, models, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

REAL = "real"        # on live
BACKUP = "backup"    # on staging, copied from live
TESTING = "testing"  # on staging, made there

LABELS = {REAL: "Real", BACKUP: "Backup", TESTING: "Testing"}

#: The six that carry goodtip.origin.OriginMixin and have their own sync.
ORIGIN_MODELS = {
    "accounts.user", "accounts.launchsignup", "orgs.organisation",
    "orgs.orgmember", "orgs.group", "orgs.groupmember",
}

#: Reference tables, matched by natural key. Order matters: a key may itself
#: point at an earlier reference table (a competition's season).
REFERENCE_KEYS = {
    "catalog.sport": ["slug"],
    "catalog.season": ["year"],
    "catalog.state": ["code"],
    "catalog.country": ["code"],
    "catalog.organisationtype": ["slug"],
    "catalog.series": ["slug"],
    "catalog.competition": ["slug", "season"],
    "catalog.subcategory": ["organisation_type", "slug"],
    "catalog.grouptype": ["organisation_type", "slug"],
    "catalog.charity": ["slug"],
    "tipping.team": ["slug", "series"],
}

SKIP_APPS = {"admin", "auth", "contenttypes", "sessions", "sysadmin", "data_sync", "matchreader"}
#: The blog (client, 26 Oct 2026: "when he creates a blog on staging it
#: should be in live database as well... in staging label it as backed up,
#: because it's basically real blogs"). sync_newsposts keeps the two sides'
#: posts the same, both ways, every two minutes — so every post on staging is
#: real, and is labelled Backup here; on Live they are Real.
BLOG_MODELS = {"admin_panel.newspost"}

TRACKED = ("origin", "copy", "blog")

SKIP_MODELS = {
    "accounts.logincode",            # one-time sign-in codes: secrets, and per environment
    "orgs.workemailverification",    # the same, for work emails
    "accounts.waitlistcampaign",     # each environment's own launch email
    "catalog.goodlistconfig",        # site setting
    "admin_panel.newsmedia",
    "admin_panel.newsposttombstone",
}


def label_of(model) -> str:
    return model._meta.label_lower


def is_tracked(model) -> bool:
    """Has Backup/Testing labels (and Real rows on the Live database)."""
    return kind_of(model) in TRACKED


def has_origin(model) -> bool:
    return label_of(model) in ORIGIN_MODELS


def kind_of(model) -> str:
    """"origin", "reference", "skip" or "copy" — how the backup treats it."""
    label = label_of(model)
    if label in ORIGIN_MODELS:
        return "origin"
    if label in BLOG_MODELS:
        return "blog"
    if label in REFERENCE_KEYS:
        return "reference"
    if model._meta.app_label in SKIP_APPS or label in SKIP_MODELS or model._meta.proxy \
            or not model._meta.managed or model._meta.auto_created:
        return "skip"
    return "copy"


# ---------------------------------------------------------------------------
# Labels and counts (the Databases screens)
# ---------------------------------------------------------------------------

def backup_pks(model, pks) -> set[str]:
    """Which of `pks` (staging keys) are BACKUP rows."""
    pks = [str(p) for p in pks]
    if label_of(model) in BLOG_MODELS:
        return set(pks)
    if has_origin(model):
        from goodtip.origin import ORIGIN_LIVE
        return {str(p) for p in model._base_manager.filter(pk__in=pks, origin=ORIGIN_LIVE).values_list("pk", flat=True)}
    from .models import BackupRecord
    return set(BackupRecord.objects.filter(model_label=label_of(model), staging_pk__in=pks)
               .values_list("staging_pk", flat=True))


def label_for(model, obj) -> str:
    return BACKUP if str(obj.pk) in backup_pks(model, [obj.pk]) else TESTING


def backup_queryset(model):
    """Staging's BACKUP rows of `model` — what the Live database shows."""
    qs = model._base_manager.all()
    if label_of(model) in BLOG_MODELS:
        return qs
    if has_origin(model):
        from goodtip.origin import ORIGIN_LIVE
        return qs.filter(origin=ORIGIN_LIVE)
    from .models import BackupRecord
    pks = BackupRecord.objects.filter(model_label=label_of(model)).values_list("staging_pk", flat=True)
    pk_field = model._meta.pk
    if isinstance(pk_field, (models.AutoField, models.BigAutoField, models.IntegerField)):
        return qs.filter(pk__in=[int(p) for p in pks if p.lstrip("-").isdigit()])
    return qs.filter(pk__in=list(pks))


def testing_queryset(model):
    qs = model._base_manager.all()
    if label_of(model) in BLOG_MODELS:
        return qs.none()
    if has_origin(model):
        from goodtip.origin import ORIGIN_LIVE
        return qs.exclude(origin=ORIGIN_LIVE)
    return qs.exclude(pk__in=backup_queryset(model).values("pk"))


def counts(model) -> dict:
    """{"total", "backup", "testing"} for one table, on this database."""
    try:
        total = model._base_manager.count()
        backup = backup_queryset(model).count()
    except Exception:  # noqa: BLE001 — a table mid-migration costs a number, not the page
        return {"total": None, "backup": None, "testing": None}
    return {"total": total, "backup": backup, "testing": max(0, total - backup)}


def run_is_quiet(run) -> bool:
    """Nothing new and nothing changed — a check that found live as it was."""
    for label, c in (run.counts or {}).items():
        if label.startswith("_") or not isinstance(c, dict):
            continue
        if c.get("new") or c.get("updated") or c.get("error"):
            return False
    return True


def fold_quiet_run(run) -> None:
    """Quiet checks run every few seconds when the backup watches live, and a
    history of thousands of identical "nothing new" lines tells nobody
    anything. So a quiet run that follows another quiet one is folded into
    it: that one row becomes a "quiet stretch" — from when it started to now,
    and how many checks it holds."""
    from .models import BackupRun

    if not run.ok or not run_is_quiet(run):
        return
    prev = BackupRun.objects.filter(started_at__lt=run.started_at).exclude(pk=run.pk).order_by("-started_at").first()
    totals = {k: v.get("live") for k, v in (run.counts or {}).items()
              if isinstance(v, dict) and v.get("live") is not None}
    if prev and prev.ok and (prev.counts or {}).get("_quiet"):
        prev.finished_at = run.finished_at
        prev.counts["_checks"] = int(prev.counts.get("_checks", 1)) + 1
        prev.counts["_live"] = totals
        prev.save(update_fields=["finished_at", "counts"])
        run.delete()
    else:
        run.counts = {"_quiet": True, "_checks": 1, "_live": totals}
        run.save(update_fields=["counts"])


def live_totals() -> dict:
    """Live's row count per table, as of the last pass that read it."""
    from .models import BackupRun

    run = BackupRun.objects.filter(ok=True, finished_at__isnull=False).order_by("-started_at").first()
    if run is None:
        return {}
    counts = run.counts or {}
    if counts.get("_quiet"):
        return dict(counts.get("_live") or {})
    return {k: v.get("live") for k, v in counts.items()
            if isinstance(v, dict) and v.get("live") is not None}


def record_origin_backups() -> int:
    """Give the six hand-synced tables' backup rows a BackupRecord too, so
    the Sync screen's feed of what came across includes new people and
    organisations, not only Wall posts and tips. Their label still comes
    from their own origin field; this is the feed's memory of when."""
    from .models import BackupRecord

    made = 0
    now = timezone.now()
    for label in ORIGIN_MODELS:
        model = apps.get_model(label)
        have = set(BackupRecord.objects.filter(model_label=label).values_list("staging_pk", flat=True))
        rows = [BackupRecord(model_label=label, staging_pk=str(pk), live_pk=str(live), synced_at=now)
                for pk, live in model._base_manager.filter(live_id__isnull=False).values_list("pk", "live_id")
                if str(pk) not in have]
        if rows:
            BackupRecord.objects.bulk_create(rows, ignore_conflicts=True)
            made += len(rows)
    return made


def last_run():
    from .models import BackupRun
    return BackupRun.objects.filter(finished_at__isnull=False).order_by("-started_at").first()


# ---------------------------------------------------------------------------
# Who may delete what
# ---------------------------------------------------------------------------

def is_ian(user) -> bool:
    from . import access
    return access.can_delete_real(user)


def delete_rule(user, database: str, label: str) -> str:
    """"delete" (may delete it now), "ask" (must ask Ian) or "no"."""
    if database == "live" or label in (REAL, BACKUP):
        return "delete" if is_ian(user) else "ask"
    return "delete"


def queue_live_deletion(model, obj, *, by, reason="", approved: bool):
    """A deletion ON LIVE, of the live row this staging BACKUP row copies.
    Carried out by the backup job — the staging site never writes to live."""
    from .models import DeletionRequest

    return DeletionRequest.objects.create(
        database=DeletionRequest.DB_LIVE, model_label=label_of(model),
        object_pk=str(live_pk_of(model, obj.pk) or ""), object_repr=str(obj)[:300], reason=reason,
        requested_by=by,
        status=DeletionRequest.APPROVED if approved else DeletionRequest.PENDING,
        decided_by=by if approved else None, decided_at=timezone.now() if approved else None,
    )


def live_pk_of(model, staging_pk):
    if has_origin(model):
        return model._base_manager.filter(pk=staging_pk).values_list("live_id", flat=True).first()
    from .models import BackupRecord
    return BackupRecord.objects.filter(model_label=label_of(model), staging_pk=str(staging_pk)) \
        .values_list("live_pk", flat=True).first()


def delete_on_staging(model, obj) -> None:
    """Delete a staging row, and forget its backup record if it had one."""
    from .models import BackupRecord

    pk = str(obj.pk)
    with transaction.atomic():
        obj.delete()
        BackupRecord.objects.filter(model_label=label_of(model), staging_pk=pk).delete()


# ---------------------------------------------------------------------------
# Copying every table (run by sync_live_backup, staging only)
# ---------------------------------------------------------------------------

class Mapper:
    """live id -> staging id, per table, for translating foreign keys."""

    def __init__(self, source: str):
        self.source = source
        self._maps: dict[str, dict] = {}
        self._live_columns: dict[str, set | None] = {}

    # -- what live actually has --
    def live_columns(self, model) -> set | None:
        table = model._meta.db_table
        if table not in self._live_columns:
            conn = connections[self.source]
            with conn.cursor() as cur:
                if table not in conn.introspection.table_names(cur):
                    self._live_columns[table] = None
                else:
                    self._live_columns[table] = {c.name for c in conn.introspection.get_table_description(cur, table)}
        return self._live_columns[table]

    def live_fields(self, model):
        cols = self.live_columns(model)
        if cols is None:
            return None
        return [f for f in model._meta.concrete_fields if f.column in cols]

    # -- maps --
    def map_for(self, model) -> dict:
        label = label_of(model)
        if label not in self._maps:
            kind = kind_of(model)
            if kind == "origin":
                self._maps[label] = {
                    str(live): pk for pk, live in
                    model._base_manager.filter(live_id__isnull=False).values_list("pk", "live_id")
                }
            elif kind == "reference":
                self._maps[label] = self._reference_map(model)
            else:
                from .models import BackupRecord
                self._maps[label] = {
                    live: staging for staging, live in
                    BackupRecord.objects.filter(model_label=label).values_list("staging_pk", "live_pk")
                }
        return self._maps[label]

    def remember(self, model, live_pk, staging_pk):
        self.map_for(model)[str(live_pk)] = staging_pk

    def _reference_map(self, model) -> dict:
        keys = REFERENCE_KEYS[label_of(model)]
        fields = [model._meta.get_field(k) for k in keys]
        if self.live_columns(model) is None:
            return {}
        live_vals = model._base_manager.using(self.source).order_by().values_list("pk", *[f.attname for f in fields])
        local = {}
        for row in model._base_manager.values_list("pk", *[f.attname for f in fields]):
            local[tuple(str(v) for v in row[1:])] = row[0]
        out = {}
        for row in live_vals:
            key = []
            for f, v in zip(fields, row[1:]):
                if f.is_relation and v is not None:
                    v = self.map_for(f.related_model).get(str(v))
                key.append(str(v))
            hit = local.get(tuple(key))
            if hit is not None:
                out[str(row[0])] = hit
        return out

    def translate(self, field, value):
        if value is None:
            return None
        target = field.related_model
        if kind_of(target) == "skip":
            return None
        return self.map_for(target).get(str(value))


def copy_order(models_):
    """Parents before children, by required foreign keys; cycles broken by
    nullable ones (those are filled in a second pass)."""
    labels = {label_of(m): m for m in models_}
    deps = {label: set() for label in labels}
    for label, m in labels.items():
        for f in m._meta.concrete_fields:
            if f.is_relation and not f.null:
                t = label_of(f.related_model)
                if t in labels and t != label:
                    deps[label].add(t)
    order, done = [], set()
    while deps:
        ready = sorted(l for l, d in deps.items() if d <= done)
        if not ready:                      # a cycle of required keys: take the rest as they come
            ready = sorted(deps)
        for l in ready:
            order.append(labels[l])
            done.add(l)
            deps.pop(l)
    return order


def copyable_models():
    return [m for m in apps.get_models() if kind_of(m) == "copy"]


def copy_everything(source: str, *, log=None) -> dict:
    """Copy every COPY table from `source` (a connection to live) into this
    process's default database. Returns {table label: {"new": n, "updated": n}}
    — "new" is what the backup history shows as arriving in that run."""
    say = log or (lambda msg: None)
    mapper = Mapper(source)
    results, pending = {}, []
    now = timezone.now()

    for model in copy_order(copyable_models()):
        label = label_of(model)
        # One table at a time, each in its own savepoint: a table that
        # trips on something unexpected is reported and skipped, and every
        # other table still backs up.
        try:
            with transaction.atomic():
                results[label] = _copy_table(model, source, mapper, pending, now, say)
        except Exception as exc:  # noqa: BLE001 — logged, and the run carries on
            logger.exception("backup: %s could not be copied", label)
            say(f"  {label}: NOT COPIED — {exc}")
            results[label] = {"new": 0, "updated": 0, "error": str(exc)[:300]}
            mapper._maps.pop(label, None)

    _finish(mapper, pending)
    return results


def _copy_table(model, source, mapper, pending, now, say) -> int:
    from .models import BackupRecord

    label = label_of(model)
    fields = mapper.live_fields(model)
    if fields is None:
        return {"new": 0, "updated": 0}            # not on live yet
    pk_att = model._meta.pk.attname
    attnames = [f.attname for f in fields]
    if pk_att not in attnames:
        return {"new": 0, "updated": 0}
    mapping = mapper.map_for(model)
    existing_rows = {}
    if mapping:
        for row in model._base_manager.filter(pk__in=list(mapping.values())).values(*attnames):
            existing_rows[str(row[pk_att])] = row
    file_atts = {f.attname for f in fields if isinstance(f, models.FileField)}
    new_objs = []
    changed = skipped = 0
    live_total = 0
    for row in model._base_manager.using(source).order_by().values(*attnames).iterator():
        live_total += 1
        live_pk = row[pk_att]
        data, ok = {}, True
        for f in fields:
            if f.primary_key:
                continue
            v = row[f.attname]
            if f.is_relation and v is not None:
                tv = mapper.translate(f, v)
                if tv is None:
                    if not f.null:
                        ok = False
                        break
                    if kind_of(f.related_model) != "skip":
                        pending.append((model, live_pk, f, v))
                v = tv
            data[f.attname] = v
        if not ok:
            skipped += 1
            continue
        staging_pk = mapping.get(str(live_pk))
        if staging_pk is not None:
            current = existing_rows.get(str(staging_pk))
            if current is None:
                continue                           # deleted on staging since; leave it gone
            # Compared as text: a translated key may come back as "12"
            # where the row holds 12, and that is not a change.
            # A file field keeps staging's own value: live's uploads are not on
            # this server, so its path would replace a working picture with a
            # dead one.
            diff = {k: v for k, v in data.items() if k not in file_atts and str(current.get(k)) != str(v)}
            if diff:
                model._base_manager.filter(pk=staging_pk).update(**diff)
                changed += 1
        else:
            new_objs.append((live_pk, {**data, **dict.fromkeys(file_atts, "")}))

    created = 0
    for live_pk, data in new_objs:
        obj = model(**data)
        try:
            with transaction.atomic():
                model._base_manager.bulk_create([obj])
                # bulk_create stamps auto_now/auto_now_add with "now";
                # put live's own dates back.
                stamps = {f.attname: data[f.attname] for f in fields
                          if getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False)}
                if stamps:
                    model._base_manager.filter(pk=obj.pk).update(**stamps)
                BackupRecord.objects.create(model_label=label, staging_pk=str(obj.pk),
                                            live_pk=str(live_pk), synced_at=now)
            mapper.remember(model, live_pk, obj.pk)
            created += 1
        except IntegrityError as exc:
            skipped += 1
            logger.info("backup: %s live #%s not copied: %s", label, live_pk, exc)

    # Many-to-many tables made by Django itself, copied once both ends exist.
    for m2m in model._meta.many_to_many:
        through = m2m.remote_field.through
        if not through._meta.auto_created or mapper.live_columns(through) is None:
            continue
        src = m2m.m2m_field_name()
        dst = m2m.m2m_reverse_field_name()
        src_f, dst_f = through._meta.get_field(src), through._meta.get_field(dst)
        links = []
        for a, b in through._base_manager.using(source).order_by().values_list(src_f.attname, dst_f.attname):
            la, lb = mapper.translate(src_f, a), mapper.translate(dst_f, b)
            if la is not None and lb is not None:
                links.append(through(**{src_f.attname: la, dst_f.attname: lb}))
        if links:
            through._base_manager.bulk_create(links, ignore_conflicts=True)

    if created or changed or skipped:
        say(f"  {label}: {created} new, {changed} updated, {skipped} skipped")
    # How many rows live had — the Sync screen sets it beside staging's
    # Backup count to say whether the two agree.
    return {"new": created, "updated": changed, "live": live_total}


def _finish(mapper, pending):
    from .models import BackupRecord

    # Second pass: nullable keys whose target was copied after them.
    for model, live_pk, f, v in pending:
        staging_pk = mapper.map_for(model).get(str(live_pk))
        tv = mapper.translate(f, v)
        if staging_pk is not None and tv is not None:
            model._base_manager.filter(pk=staging_pk).update(**{f.attname: tv})

    # Records whose staging row has since gone (deleted with Ian's approval).
    for label in {r for r in BackupRecord.objects.values_list("model_label", flat=True).distinct()}:
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        keep = {str(p) for p in model._base_manager.values_list("pk", flat=True)}
        stale = [pk for pk in BackupRecord.objects.filter(model_label=label).values_list("staging_pk", flat=True)
                 if pk not in keep]
        if stale:
            BackupRecord.objects.filter(model_label=label, staging_pk__in=stale).delete()


# ---------------------------------------------------------------------------
# Deleting on live (run by sync_live_backup, with a write connection)
# ---------------------------------------------------------------------------

def run_live_deletions(write_alias: str | None, *, log=None) -> int:
    """Carry out approved deletions on live. Each in its own transaction on
    live, so one that cannot go (rows still pointing at it) fails alone and
    says why. The staging backup copy is kept."""
    from .models import DeletionRequest

    say = log or (lambda msg: None)
    todo = list(DeletionRequest.objects.filter(database=DeletionRequest.DB_LIVE, status=DeletionRequest.APPROVED))
    if not todo:
        return 0
    if not write_alias:
        say(f"  {len(todo)} approved live deletion(s) waiting: no write connection to live is configured")
        return 0
    done = 0
    for req in todo:
        try:
            model = apps.get_model(req.model_label)
            with transaction.atomic(using=write_alias):
                obj = model._base_manager.using(write_alias).only("pk").filter(pk=req.object_pk).first()
                if obj is not None:
                    obj.delete(using=write_alias)
            req.status, req.done_at, req.error = DeletionRequest.DONE, timezone.now(), ""
            done += 1
        except Exception as exc:  # noqa: BLE001 — recorded on the request for Ian to read
            req.status, req.error = DeletionRequest.FAILED, str(exc)[:2000]
        req.save(update_fields=["status", "done_at", "error"])
    say(f"  live deletions: {done} done, {len(todo) - done} could not go")
    return done


# ---------------------------------------------------------------------------
# The rule, on every table in the Django admin
# ---------------------------------------------------------------------------

def guard_admin_deletes(site=None) -> None:
    """Make every registered ModelAdmin refuse to delete a BACKUP row unless
    the person is Ian. Covers the Delete button and the bulk "Delete
    selected" action alike — Django asks has_delete_permission(request, obj)
    for every object either would remove. Called once from SysadminConfig.
    ready(), after the admin has registered everything."""
    from django.contrib import admin as django_admin

    site = site or django_admin.site
    for model, model_admin in list(site._registry.items()):
        if kind_of(model) not in TRACKED or getattr(model_admin, "_backup_guarded", False):
            continue
        original = model_admin.has_delete_permission

        def guarded(request, obj=None, _original=original, _model=model):
            if obj is not None and label_for(_model, obj) == BACKUP and not is_ian(request.user):
                return False
            return _original(request, obj)

        model_admin.has_delete_permission = guarded
        model_admin._backup_guarded = True
