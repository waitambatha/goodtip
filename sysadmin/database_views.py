"""DATABASES (client, 25 Oct 2026): "instead of tables and models... I see two
databases. Live and staging. When I click on live, I should see all the
tables... in staging two things: testing or backup... and the super admin
called Ian is the one that can only delete" real data.

Four screens, on the pitch (admin/hub/gx_base.html):
  databases_hub     the two databases as cards, the last backup, approvals
  database_detail   one database's tables, by category, with their counts
  database_table    one table's rows, each labelled Real / Backup / Testing,
                    with Delete — or "Ask Ian" where the rule says so
  deletion_requests what is waiting for Ian, and what has been decided

The rules themselves live in sysadmin.backup. On staging's admin the Live
database is read from the backup (staging's BACKUP rows, as of the last run)
— the staging site never opens a connection to live.
"""
from django.apps import apps
from django.conf import settings
from django.contrib import admin, messages
from django.core.paginator import Paginator
from django.db import models
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import backup, hub
from .models import DeletionRequest

DATABASES = {
    "live": {
        "key": "live", "label": "Live", "host": "goodtip.com.au",
        "lead": "Everything real — what members, organisations and the waiting list have created on "
                "goodtip.com.au. Read from the backup, as of its last run.",
    },
    "staging": {
        "key": "staging", "label": "Staging", "host": "staging.goodtip.com.au",
        "lead": "The team's database. Every table holds two kinds of row: Backup — real data copied "
                "from live — and Testing — made here.",
    },
}

DATE_FIELDS = ("created_at", "created", "date_joined", "joined_at", "requested_at", "submitted_at", "sent_at",
               "posted_at", "date", "kickoff", "started_at", "updated_at")


def _ctx(request, title, **extra):
    ctx = admin.site.each_context(request)
    ctx.update(title=title, **extra)
    return ctx


def _allowed_labels(request) -> set[str]:
    """Tables this administrator is allowed to see at all — Django has
    already filtered available_apps by permission."""
    out = set()
    for app in admin.site.each_context(request).get("available_apps", []):
        for m in app.get("models", []):
            out.add(f"{app['app_label']}.{m['object_name'].lower()}")
    return out


def _db(key):
    if key not in DATABASES:
        raise Http404("No such database")
    return DATABASES[key]


def _tables(request, db_key):
    """Every table this person may see, as the given database has it."""
    allowed = _allowed_labels(request)
    rows = []
    for model in apps.get_models():
        label = backup.label_of(model)
        if label not in allowed:
            continue
        kind = backup.kind_of(model)
        if db_key == "live" and kind == "skip":
            continue                                     # staging's own, never on the backup
        meta = hub.BY_REF.get(label)
        c = backup.counts(model)
        row = {
            "label": label, "name": str(model._meta.verbose_name_plural).capitalize(),
            "blurb": meta.blurb if meta else "", "icon": meta.icon if meta else "ic-doc",
            "kind": kind, "url": reverse("admin:hq_database_table", args=[db_key, label]),
        }
        if db_key == "live":
            row["rows"] = c["backup"] if kind in backup.TRACKED else c["total"]
        else:
            row.update(rows=c["total"], backup=c["backup"] if kind in backup.TRACKED else None,
                       testing=c["testing"] if kind in backup.TRACKED else None)
        rows.append(row)
    return rows


def _grouped(rows):
    """The tables in hub.CATEGORIES' order and wording; anything unclaimed
    collects under "Everything else"."""
    by_label = {r["label"]: r for r in rows}
    groups, claimed = [], set()
    for cat in hub.CATEGORIES:
        items = [by_label[t.ref] for t in cat.tables if t.ref in by_label]
        if items:
            groups.append({"key": cat.key, "label": cat.label, "blurb": cat.blurb, "tables": items})
            claimed.update(r["label"] for r in items)
    rest = sorted((r for r in rows if r["label"] not in claimed), key=lambda r: r["name"])
    if rest:
        groups.append({"key": "more", "label": "Everything else", "blurb": "", "tables": rest})
    return groups


def _pending_count():
    return DeletionRequest.objects.filter(status=DeletionRequest.PENDING).count()


def databases_hub(request):
    live_rows = _tables(request, "live")
    staging_rows = _tables(request, "staging")
    tracked = [r for r in staging_rows if r.get("backup") is not None]
    def biggest(rows):
        # What each card previews: its four fullest tables, each a link in.
        return sorted((r for r in rows if r["kind"] in backup.TRACKED and r["rows"]),
                      key=lambda r: -r["rows"])[:4]

    backed = sum(r["backup"] or 0 for r in tracked)
    testing = sum(r["testing"] or 0 for r in tracked)
    return render(request, "admin/hub/databases.html", _ctx(
        request, "Databases",
        live={**DATABASES["live"], "tables": len(live_rows), "top": biggest(live_rows),
              "rows": sum(r["rows"] or 0 for r in live_rows if r["kind"] in backup.TRACKED)},
        staging={**DATABASES["staging"], "tables": len(staging_rows), "top": biggest(staging_rows),
                 "backup": backed, "testing": testing,
                 "backup_pc": round(backed * 100 / (backed + testing)) if backed + testing else 0},
        last_run=backup.last_run(),
        sync=sync_status(), every=int(BACKUP_EVERY.total_seconds()),
        sync_tables=_sync_summary(),
        pending=_pending_count(),
        is_ian=backup.is_ian(request.user),
        is_staging=getattr(settings, "IS_STAGING", False),
    ))


def database_detail(request, db):
    info = _db(db)
    rows = _tables(request, db)
    return render(request, "admin/hub/database.html", _ctx(
        request, info["label"],
        db=info, groups=_grouped(rows),
        total_rows=sum((r["rows"] or 0) for r in rows if r["kind"] in backup.TRACKED),
        total_backup=sum(r.get("backup") or 0 for r in rows),
        total_testing=sum(r.get("testing") or 0 for r in rows),
        table_count=len(rows),
        last_run=backup.last_run(),
        sync=sync_status(), every=int(BACKUP_EVERY.total_seconds()),
        pending=_pending_count(),
        back_url=reverse("admin:hq_tables"), back_label="databases",
    ))


def _model(label, request):
    if label not in _allowed_labels(request):
        raise Http404("No such table")
    try:
        return apps.get_model(label)
    except (LookupError, ValueError):
        raise Http404("No such table")


def _date_of(model):
    names = {f.name for f in model._meta.concrete_fields}
    return next((n for n in DATE_FIELDS if n in names), None)


def _search(model, qs, q):
    """Search the table the way its own admin does, or across its text fields."""
    if not q:
        return qs
    ma = admin.site._registry.get(model)
    fields = list(getattr(ma, "search_fields", None) or [])
    if not fields:
        fields = [f.name for f in model._meta.concrete_fields
                  if isinstance(f, (models.CharField, models.TextField, models.EmailField))][:6]
    cond = Q()
    for f in fields:
        f = f.lstrip("^=@")
        cond |= Q(**{f"{f}__icontains": q})
    if q.isdigit():
        cond |= Q(pk=q)
    return qs.filter(cond) if fields or q.isdigit() else qs


def _series_path(model):
    """How a row reaches its competition (catalog.Series), or None.

    The Competition column (client, 27 Oct 2026): a match, a round, a tip, a
    ladder row all belong to AFL, AFLW, the NRL... — found by following the
    first link that leads there, so a table added later is covered too."""
    from catalog.models import Series

    from tipping.models import Match, Round
    for f in model._meta.get_fields():
        if not (f.is_relation and f.many_to_one and f.concrete):
            continue
        if f.related_model is Series:
            return f.name
        if f.related_model is Round:
            return f"{f.name}__series"
        if f.related_model is Match:
            return f"{f.name}__round__series"
    return None


def database_table(request, db, label):
    info = _db(db)
    model = _model(label, request)
    kind = backup.kind_of(model)
    if db == "live" and kind == "skip":
        raise Http404("Not on the live database")
    show = request.GET.get("show") or "all"
    q = (request.GET.get("q") or "").strip()

    tracked = kind in backup.TRACKED
    if db == "live":
        qs = backup.backup_queryset(model) if tracked else model._base_manager.all()
    elif tracked and show == "backup":
        qs = backup.backup_queryset(model)
    elif tracked and show == "testing":
        qs = backup.testing_queryset(model)
    else:
        show = "all"
        qs = model._base_manager.all()
    # ?where=org:12 — "everything in this table that points at that row",
    # the link the record page's Connected tab uses.
    where = (request.GET.get("where") or "").strip()
    where_text = ""
    if where and ":" in where:
        fname, fval = where.split(":", 1)
        try:
            f = model._meta.get_field(fname)
            if f.is_relation and not f.many_to_many:
                qs = qs.filter(**{f.attname: fval})
                target = f.related_model._base_manager.filter(pk=fval).first()
                where_text = f"{f.verbose_name}: {target}" if target else ""
        except Exception:  # noqa: BLE001 — a bad filter is ignored, not a 500
            where = ""
    # The Competition column, and filtering by it.
    spath = _series_path(model)
    comp = (request.GET.get("comp") or "").strip()
    comps = []
    if spath:
        comps = list(qs.exclude(**{f"{spath}__isnull": True})
                     .values_list(f"{spath}__slug", f"{spath}__name").distinct().order_by(f"{spath}__name"))
        if comp:
            qs = qs.filter(**{f"{spath}__slug": comp})
    qs = _search(model, qs, q).order_by("-pk")
    # A hundred at a time, in a box that shows about fifteen and scrolls
    # (client, 26 Oct 2026: "show till 15 then bring in the scrolling").
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    objs = list(page.object_list)
    backups = backup.backup_pks(model, [o.pk for o in objs]) if tracked else set()
    comp_of = dict(model._base_manager.filter(pk__in=[o.pk for o in objs])
                   .values_list("pk", f"{spath}__name")) if spath else {}
    date_field = _date_of(model)

    def admin_url(obj):
        try:
            return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_change", args=[obj.pk])
        except NoReverseMatch:
            return ""

    ma = admin.site._registry.get(model)
    rows = []
    for obj in objs:
        if db == "live":
            lab = backup.REAL
        elif not tracked:
            lab = None
        else:
            lab = backup.BACKUP if str(obj.pk) in backups else backup.TESTING
        rule = backup.delete_rule(request.user, db, lab or backup.TESTING)
        may_delete_here = bool(ma and ma.has_delete_permission(request, None)) if db == "staging" else True
        rows.append({
            "obj": obj, "pk": obj.pk, "text": str(obj)[:140], "label": lab,
            "comp": comp_of.get(obj.pk) or "",
            "when": getattr(obj, date_field, None) if date_field else None,
            "open": reverse("admin:hq_database_record", args=[db, label, obj.pk]),
            "rule": rule if (tracked or db == "staging") and may_delete_here else "no",
        })
    c = backup.counts(model)
    meta = hub.BY_REF.get(label)
    return render(request, "admin/hub/database_table.html", _ctx(
        request, str(model._meta.verbose_name_plural).capitalize(),
        db=info, label=label, kind=kind, tracked=tracked, rows=rows, page=page, show=show, q=q,
        counts=c, blurb=meta.blurb if meta else "", date_field=date_field, date_label=(date_field or "").replace("_", " ").capitalize(),
        has_comp=bool(spath), comps=comps, comp=comp, where=where, where_text=where_text,
        is_ian=backup.is_ian(request.user),
        back_url=reverse("admin:hq_database", args=[db]), back_label=f"the {info['label']} database",
    ))


@require_POST
def database_delete(request, db, label, pk):
    """Delete a row, or ask Ian to — whichever the rule says for this person."""
    _db(db)
    model = _model(label, request)
    back = redirect(reverse("admin:hq_database_table", args=[db, label]) + f"?{request.META.get('QUERY_STRING', '')}")
    obj = model._base_manager.filter(pk=pk).first()
    if obj is None:
        messages.error(request, "That row is already gone.")
        return back
    ma = admin.site._registry.get(model)
    if db == "staging" and not (ma and ma.has_delete_permission(request, None)):
        messages.error(request, "You can't delete rows from this table.")
        return back
    lab = backup.REAL if db == "live" else backup.label_for(model, obj)
    rule = backup.delete_rule(request.user, db, lab)
    reason = (request.POST.get("reason") or "").strip()[:2000]
    name = str(obj)[:120]

    if db == "live":
        req = backup.queue_live_deletion(model, obj, by=request.user, reason=reason, approved=(rule == "delete"))
        if rule == "delete":
            messages.success(request, f"“{name}” will be deleted from live on the next backup run. "
                                      "The backup copy here is kept.")
        else:
            _tell_ian(req)
            messages.success(request, f"Asked Ian to delete “{name}” from live. You'll see his answer under Approvals.")
        return back

    if rule == "delete":
        backup.delete_on_staging(model, obj)
        messages.success(request, f"Deleted “{name}” from staging.")
        return back
    req = DeletionRequest.objects.create(
        database=DeletionRequest.DB_STAGING, model_label=label, object_pk=str(obj.pk), object_repr=name,
        reason=reason, requested_by=request.user,
    )
    _tell_ian(req)
    messages.success(request, f"“{name}” is a backup of real data — asked Ian to approve deleting it.")
    return back


def _tell_ian(req):
    """An email to whoever can approve — best-effort, the request is saved."""
    from accounts.models import User
    from goodtip.mail import send_template, site_url

    from .access import access_for

    try:
        to = [u.email for u in User.objects.filter(is_staff=True, is_active=True).exclude(email="")
              if backup.is_ian(u) and access_for(u) is not None]
        if to:
            send_template(
                "deletion_requested", subject=f"Approve a deletion? {req.object_repr[:60]}", to=to,
                context={"req": req, "link": site_url(reverse("admin:hq_deletions"))},
            )
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("Deletion request %s saved, but Ian's email failed", req.pk)


def deletion_requests(request):
    is_ian = backup.is_ian(request.user)
    qs = DeletionRequest.objects.select_related("requested_by", "decided_by")
    if not is_ian:
        qs = qs.filter(requested_by=request.user)
    pending = [r for r in qs if r.status == DeletionRequest.PENDING]
    decided = [r for r in qs if r.status != DeletionRequest.PENDING][:60]
    return render(request, "admin/hub/deletions.html", _ctx(
        request, "Approvals",
        pending=pending, decided=decided, is_ian=is_ian,
        waiting_live=sum(1 for r in qs if r.status == DeletionRequest.APPROVED),
        back_url=reverse("admin:hq_tables"), back_label="databases",
    ))


@require_POST
def deletion_decide(request, pk):
    if not backup.is_ian(request.user):
        raise Http404("Only Ian approves deletions.")
    req = get_object_or_404(DeletionRequest, pk=pk, status=DeletionRequest.PENDING)
    here = redirect("admin:hq_deletions")
    req.decided_by, req.decided_at = request.user, timezone.now()
    req.decision_note = (request.POST.get("note") or "").strip()[:2000]
    if request.POST.get("action") != "approve":
        req.status = DeletionRequest.DECLINED
        req.save()
        messages.success(request, f"Declined — “{req.object_repr}” stays.")
        return here
    if req.database == DeletionRequest.DB_LIVE:
        req.status = DeletionRequest.APPROVED
        req.save()
        messages.success(request, f"Approved — “{req.object_repr}” goes from live on the next backup run.")
        return here
    try:
        model = apps.get_model(req.model_label)
        obj = model._base_manager.filter(pk=req.object_pk).first()
        if obj is not None:
            backup.delete_on_staging(model, obj)
        req.status, req.done_at = DeletionRequest.DONE, timezone.now()
        messages.success(request, f"Approved and deleted “{req.object_repr}” from staging.")
    except Exception as exc:  # noqa: BLE001
        req.status, req.error = DeletionRequest.FAILED, str(exc)[:2000]
        messages.error(request, f"Approved, but it could not be deleted: {exc}")
    req.save()
    return here


# ---------------------------------------------------------------------------
# The backup: when it runs next, and what each run brought across (client,
# 26 Oct 2026: "I'd like to see a timer... if I click it, let me see the
# history of the backup... in this hour we have backed up this number of
# people, this number of items").
# ---------------------------------------------------------------------------

#: How often the backup checks live. It WATCHES now (client, 26 Oct 2026:
#: "when it is created in live we immediately have it also backed up") —
#: `sync_live_backup --watch 15`, run by its own long-lived service — so the
#: countdown is to the next check, counted from when the last one finished.
BACKUP_EVERY = timezone.timedelta(seconds=int(getattr(settings, "BACKUP_WATCH_SECONDS", 15)))


def _model_name(label, n=2):
    try:
        m = apps.get_model(label)
    except (LookupError, ValueError):
        return label
    return str(m._meta.verbose_name_plural if n != 1 else m._meta.verbose_name)


def sync_status():
    """What the sync bridge on the Databases screens shows."""
    from .models import BackupRun

    last = backup.last_run()
    running = BackupRun.objects.filter(finished_at__isnull=True,
                                       started_at__gte=timezone.now() - timezone.timedelta(minutes=20)).exists()
    out = {"last": last, "running": running, "next_at": None, "last_new": 0, "last_items": [],
           "watching": False, "last_change": None}
    if last:
        out["next_at"] = last.finished_at + BACKUP_EVERY
        # Watching = checked within the last few intervals. If the watcher
        # stops, the bridge says so instead of counting down to nothing.
        out["watching"] = timezone.now() - last.finished_at < BACKUP_EVERY * 4 + timezone.timedelta(seconds=30)
        busy = BackupRun.objects.filter(ok=True, finished_at__isnull=False).exclude(counts__has_key="_quiet") \
            .order_by("-started_at").first()
        if busy:
            items = _run_items(busy)
            out["last_change"] = busy
            out["last_new"] = sum(i["new"] for i in items)
            out["last_items"] = [i for i in items if i["new"]][:3]
    return out


def _run_items(run):
    items = []
    for label, c in (run.counts or {}).items():
        if label.startswith("_"):
            continue                                 # _quiet / _checks: bookkeeping, not tables
        if isinstance(c, int):                       # runs before 26 Oct 2026 kept one number
            c = {"new": 0, "updated": c}
        items.append({"label": label, "name": _model_name(label, c.get("new", 0)).capitalize(),
                      "new": c.get("new", 0), "updated": c.get("updated", 0), "error": c.get("error", "")})
    items.sort(key=lambda i: (-i["new"], -i["updated"], i["name"]))
    return items


def _sync_summary():
    """How many tables agree between the two, for the hub's Sync card."""
    totals = backup.live_totals()
    agree = total = 0
    for label, live_n in totals.items():
        try:
            model = apps.get_model(label)
        except (LookupError, ValueError):
            continue
        if not live_n:
            continue
        total += 1
        if backup.counts(model)["backup"] >= live_n:
            agree += 1
    return {"agree": agree, "total": total}


def backup_history(request):
    from .models import BackupRun

    runs = []
    for run in BackupRun.objects.order_by("-started_at")[:72]:
        items = _run_items(run)
        runs.append({
            "run": run, "items": [i for i in items if i["new"]],
            "quiet": bool((run.counts or {}).get("_quiet")), "checks": (run.counts or {}).get("_checks", 1),
            "updated_items": [i for i in items if i["updated"] and not i["new"]],
            "errors": [i for i in items if i["error"]],
            "new": sum(i["new"] for i in items), "updated": sum(i["updated"] for i in items),
            "took": (run.finished_at - run.started_at).total_seconds() if run.finished_at else None,
        })
    # ---- the two side by side: live's rows beside staging's backup ----
    totals = backup.live_totals()
    compare = []
    for model in apps.get_models():
        if backup.kind_of(model) not in ("origin", "copy"):
            continue
        label = backup.label_of(model)
        live_n = totals.get(label)
        staging_n = backup.counts(model)["backup"]
        if not live_n and not staging_n:
            continue
        if live_n is None:
            state = "unknown"
        elif staging_n == live_n:
            state = "match"
        elif staging_n > live_n:
            state = "kept"                         # live deleted some; the backup keeps them
        else:
            state = "behind"
        compare.append({"label": label, "name": _model_name(label).capitalize(), "live": live_n,
                        "staging": staging_n, "state": state, "diff": abs((staging_n or 0) - (live_n or 0)),
                        "url": reverse("admin:hq_database_table", args=["staging", label]) + "?show=backup"})
    compare.sort(key=lambda r: (r["state"] != "behind", -(r["live"] or 0)))
    in_sync = sum(1 for r in compare if r["state"] in ("match", "kept"))

    # ---- the feed: the real things most recently copied across ----
    from .models import BackupRecord

    feed, by_label = [], {}
    recs = list(BackupRecord.objects.order_by("-synced_at", "-pk")[:40])
    for r in recs:
        by_label.setdefault(r.model_label, []).append(r.staging_pk)
    objs = {}
    for label, pks in by_label.items():
        try:
            m = apps.get_model(label)
            date_field = _date_of(m)
            for o in m._base_manager.filter(pk__in=pks):
                objs[(label, str(o.pk))] = (o, getattr(o, date_field, None) if date_field else None)
        except Exception:  # noqa: BLE001
            continue
    for r in recs:
        hit = objs.get((r.model_label, r.staging_pk))
        if not hit:
            continue
        o, made = hit
        feed.append({"label": r.model_label, "kind": _model_name(r.model_label, 1).capitalize(),
                     "text": str(o)[:90], "made": made, "synced": r.synced_at,
                     "url": reverse("admin:hq_database_record", args=["staging", r.model_label, r.staging_pk])})

    chart = list(reversed(runs[:24]))
    top = max([r["new"] for r in chart] or [0]) or 1
    for r in chart:
        r["h"] = round(r["new"] / top * 100)
    today = timezone.localdate()
    status = sync_status()
    return render(request, "admin/hub/backup_history.html", _ctx(
        request, "Sync",
        runs=runs, chart=chart, status=status, sync=status, every=int(BACKUP_EVERY.total_seconds()),
        compare=compare, in_sync=in_sync, feed=feed,
        today_runs=sum(1 for r in runs if timezone.localtime(r["run"].started_at).date() == today),
        today_new=sum(r["new"] for r in runs if timezone.localtime(r["run"].started_at).date() == today),
        back_url=reverse("admin:hq_tables"), back_label="databases",
    ))


# ---------------------------------------------------------------------------
# ONE RECORD (client, 26 Oct 2026: "when I click a specific thing in that
# table and it opens up, I need a modern UI that we're currently having" —
# in place of Django's own change form). Details, what it is connected to,
# and — on staging, for someone allowed to — an edit form in the same style.
# ---------------------------------------------------------------------------

HIDDEN_FIELDS = ("password", "token", "secret", "code_hash", "otp", "session_key")


def _hidden(name):
    return any(h in name for h in HIDDEN_FIELDS)


def _record_url(db, model, pk):
    return reverse("admin:hq_database_record", args=[db, backup.label_of(model), pk])


def _display(request, db, obj, field, allowed):
    """One field's value, ready for the record page: kind + what to show."""
    from django.db.models.fields.files import FieldFile

    if field.many_to_many:
        rel = list(getattr(obj, field.name).all()[:20])
        target = backup.label_of(field.related_model)
        return {"kind": "many", "items": [
            {"text": str(r), "url": _record_url(db, field.related_model, r.pk) if target in allowed else ""}
            for r in rel]}
    value = getattr(obj, field.name, None)
    if value is None or value == "":
        return {"kind": "empty"}
    if field.is_relation:
        target = backup.label_of(field.related_model)
        return {"kind": "link", "text": str(value),
                "url": _record_url(db, field.related_model, value.pk) if target in allowed else ""}
    if isinstance(value, bool):
        return {"kind": "bool", "value": value}
    if isinstance(value, FieldFile):
        url = ""
        try:
            url = value.url
        except Exception:  # noqa: BLE001
            pass
        is_img = isinstance(field, models.ImageField) or str(value.name).lower().endswith(
            (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"))
        return {"kind": "image" if is_img else "file", "url": url, "text": str(value.name).rsplit("/", 1)[-1]}
    if field.choices:
        return {"kind": "chip", "text": getattr(obj, f"get_{field.name}_display")()}
    if isinstance(field, models.DateTimeField):
        return {"kind": "datetime", "value": value}
    if isinstance(field, models.DateField):
        return {"kind": "date", "value": value}
    if isinstance(field, models.JSONField):
        import json
        return {"kind": "json", "text": json.dumps(value, indent=2, default=str)[:4000]}
    if isinstance(field, models.URLField):
        return {"kind": "url", "text": value}
    if isinstance(field, models.EmailField):
        return {"kind": "email", "text": value}
    if isinstance(field, models.TextField) or (isinstance(value, str) and len(value) > 140):
        return {"kind": "long", "text": value}
    if isinstance(field, (models.DecimalField, models.IntegerField, models.FloatField)):
        return {"kind": "number", "text": value}
    return {"kind": "text", "text": value}


def _sections(fields):
    """Group fields the way a person reads a record: the main facts, its
    pictures and long text, what it points at, the switches, and when."""
    groups = {"main": [], "media": [], "long": [], "links": [], "flags": [], "dates": [], "other": []}
    for f in fields:
        k = f["value"]["kind"]
        if k in ("image", "file"):
            groups["media"].append(f)
        elif k in ("long", "json"):
            groups["long"].append(f)
        elif k in ("link", "many"):
            groups["links"].append(f)
        elif k == "bool":
            groups["flags"].append(f)
        elif k in ("datetime", "date"):
            groups["dates"].append(f)
        elif k == "empty":
            groups["other"].append(f)
        else:
            groups["main"].append(f)
    return groups


def database_record(request, db, label, pk):
    info = _db(db)
    model = _model(label, request)
    kind = backup.kind_of(model)
    obj = model._base_manager.filter(pk=pk).first()
    if obj is None:
        raise Http404("No such row")
    tracked = kind in backup.TRACKED
    if db == "live":
        if not tracked or backup.label_for(model, obj) != backup.BACKUP:
            raise Http404("Not on the live database")
        lab = backup.REAL
    else:
        lab = backup.label_for(model, obj) if tracked else None

    allowed = _allowed_labels(request)
    fields = []
    for f in list(model._meta.concrete_fields) + list(model._meta.many_to_many):
        if f.primary_key or _hidden(f.name) or f.name in ("origin", "live_id"):
            continue
        fields.append({"name": f.name, "label": str(f.verbose_name).capitalize(),
                       "help": str(f.help_text or ""), "value": _display(request, db, obj, f, allowed)})

    # What points AT this row: Wall posts in an organisation, replies to a post...
    connected = []
    for rel in model._meta.related_objects:
        rm = rel.related_model
        rlabel = backup.label_of(rm)
        if rlabel not in allowed or rel.many_to_many or not getattr(rel, "field", None):
            continue
        try:
            qs = rm._base_manager.filter(**{rel.field.name: obj})
            if db == "live" and backup.is_tracked(rm):
                qs = qs.filter(pk__in=backup.backup_queryset(rm).values("pk"))
            n = qs.count()
        except Exception:  # noqa: BLE001
            continue
        if n:
            connected.append({
                "name": str(rm._meta.verbose_name_plural).capitalize(), "label": rlabel, "n": n,
                "via": str(rel.field.verbose_name), "sample": [
                    {"text": str(r)[:90], "url": _record_url(db, rm, r.pk)} for r in qs.order_by("-pk")[:4]],
                "url": reverse("admin:hq_database_table", args=[db, rlabel]) + f"?where={rel.field.name}:{obj.pk}",
            })
    connected.sort(key=lambda c: -c["n"])

    ma = admin.site._registry.get(model)
    can_edit = db == "staging" and bool(ma and ma.has_change_permission(request, obj)) \
        and label not in backup.BLOG_MODELS          # a story is edited in the story editor
    form = _edit_form(request, model, obj, ma) if can_edit else None
    if form is not None and request.method == "POST":
        if form.is_valid():
            saved = form.save()
            try:
                ma.log_change(request, saved, [{"changed": {"fields": form.changed_data}}])
            except Exception:  # noqa: BLE001
                pass
            messages.success(request, f"Saved “{str(saved)[:80]}”."
                             + (" It's a backup of live — the next backup run will put live's version back."
                                if lab == backup.BACKUP else ""))
            return redirect(_record_url(db, model, saved.pk))
        messages.error(request, "Some of that didn't save — the fields marked below need another look.")

    rule = backup.delete_rule(request.user, db, lab or backup.TESTING)
    may_delete = db == "live" or bool(ma and ma.has_delete_permission(request, None))
    meta = hub.BY_REF.get(label)
    date_field = _date_of(model)
    title = str(obj)[:120]
    special = _special(model, obj)
    return render(request, "admin/hub/database_record.html", _ctx(
        request, title,
        db=info, label=label, model_name=str(model._meta.verbose_name).capitalize(),
        table_name=str(model._meta.verbose_name_plural).capitalize(),
        obj=obj, pk=obj.pk, lab=lab, kind=kind, sections=_sections(fields), field_count=len(fields),
        connected=connected, form=form, can_edit=can_edit,
        rule=rule if may_delete and (tracked or db == "staging") else "no",
        icon=meta.icon if meta else "ic-doc", blurb=meta.blurb if meta else "",
        when=getattr(obj, date_field, None) if date_field else None,
        special=special, open_tab="edit" if (form is not None and form.errors) else "",
        prev_pk=model._base_manager.filter(pk__gt=obj.pk).order_by("pk").values_list("pk", flat=True).first()
        if db == "staging" else None,
        next_pk=model._base_manager.filter(pk__lt=obj.pk).order_by("-pk").values_list("pk", flat=True).first()
        if db == "staging" else None,
        back_url=reverse("admin:hq_database_table", args=[db, label]), back_label=str(model._meta.verbose_name_plural),
    ))


def _special(model, obj):
    """A few tables read better as what they ARE than as a list of fields."""
    label = backup.label_of(model)
    if label == "admin_panel.newspost":
        cover = ""
        for name in ("image", "cover_image", "hero_image", "cover"):
            f = getattr(obj, name, None)
            if f:
                try:
                    cover = f.url
                except Exception:  # noqa: BLE001
                    pass
                break
        body = getattr(obj, "body", "") or getattr(obj, "content", "") or ""
        try:
            edit = reverse("admin:hq_news_edit", args=[obj.pk])
        except NoReverseMatch:
            edit = ""
        try:
            view = reverse("news_detail", args=[obj.slug])
        except NoReverseMatch:
            view = ""
        # The story's own HTML, in a document of its own for a sandboxed
        # frame — so the editor's markup can't reach into the admin page.
        doc = ("<!doctype html><html><head><style>body{margin:0;font:16px/1.7 system-ui,-apple-system,"
               "Segoe UI,sans-serif;color:#151C27}img{max-width:100%;height:auto;border-radius:12px}"
               "h2,h3{line-height:1.2}a{color:#2563B8}blockquote{margin:16px 0;padding:8px 16px;"
               "border-left:4px solid #5AA9FF;background:#F3F7FC;border-radius:0 10px 10px 0}</style></head>"
               f"<body>{body}</body></html>")
        return {"kind": "story", "title": getattr(obj, "title", str(obj)), "cover": cover, "body": body, "doc": doc,
                "excerpt": getattr(obj, "excerpt", "") or getattr(obj, "summary", ""),
                "published": getattr(obj, "is_published", None), "edit": edit, "view": view}
    if label == "orgs.wallpost":
        return {"kind": "post", "author": str(getattr(obj, "author", "") or "GoodTip"),
                "org": str(getattr(obj, "org", "")), "body": getattr(obj, "body", "")}
    if label == "orgs.organisation":
        logo = ""
        f = getattr(obj, "logo", None)
        if f:
            try:
                logo = f.url
            except Exception:  # noqa: BLE001
                pass
        return {"kind": "org", "logo": logo, "name": obj.name}
    return None


def _edit_form(request, model, obj, ma):
    """A plain ModelForm over the fields the table's own admin lets you edit,
    with widgets the page styles itself (not Django admin's)."""
    from django import forms
    from django.forms import modelform_factory

    try:
        fieldnames = [f for f in ma.get_fields(request, obj) if isinstance(f, str)]
        readonly = set(ma.get_readonly_fields(request, obj))
    except Exception:  # noqa: BLE001
        return None
    editable = []
    for name in fieldnames:
        if name in readonly or _hidden(name):
            continue
        try:
            f = model._meta.get_field(name)
        except Exception:  # noqa: BLE001
            continue
        if getattr(f, "editable", False) and not f.primary_key and not f.auto_created:
            editable.append(name)
    if not editable:
        return None

    def widget_for(db_field, **kw):
        if isinstance(db_field, models.DateTimeField):
            kw["widget"] = forms.DateTimeInput(attrs={"type": "datetime-local", "data-gx-cal": ""},
                                               format="%Y-%m-%dT%H:%M")
            kw.setdefault("input_formats", ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"])
        elif isinstance(db_field, models.DateField):
            kw["widget"] = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
        return db_field.formfield(**kw)

    Form = modelform_factory(model, fields=editable, formfield_callback=widget_for)
    data = request.POST if request.method == "POST" else None
    files = request.FILES if request.method == "POST" else None
    form = Form(data, files, instance=obj)
    import re as _re

    for name, bf in form.fields.items():
        w = bf.widget
        # HTML held as text (a story body, a *_html field): edited as the
        # words themselves, never as raw tags (client, 26 Oct 2026: "I did
        # see things like <p>... make sure it's human").
        if isinstance(w, forms.Textarea):
            value = str(form.initial.get(name) or "")
            if name.endswith("_html") or name == "body" or _re.search(r"<(p|br|div|h[1-6]|ul|ol|strong|em)\b", value):
                w.attrs["data-gx-rich"] = ""
        if isinstance(w, (forms.Select, forms.SelectMultiple)) and not isinstance(w, forms.CheckboxSelectMultiple):
            w.attrs["class"] = (w.attrs.get("class", "") + " gx-in").strip()
    return form
