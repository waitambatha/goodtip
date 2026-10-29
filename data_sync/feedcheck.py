"""The feed check: is what every league shows actually what the sport published?

CLIENT, 9 OCT 2026 (Ian Hopkinson): "AFLW still going but not in the site?"
— and then: "fix the issues with fixtures once and for all ... have ways to
know if it has done so, and if not how to correct that."

Every sync records a SyncRun, and every SyncRun can read "ok" while the data
is wrong: a round never asked about, a game whose club name we could not place,
a result that never graded, a kickoff the feed moved. Freshness stamps answer
"did it run". This answers "is it right", by asking the feed itself.

WHAT IT DOES, HOURLY (and over the whole season after each six-hourly backfill)

1. COMPARE. For every series some league tips, the rounds the feed publishes
   around today are read fresh, game by game, and compared with every league
   that tips that series: the round exists, every game is in it, kickoffs
   agree, finished games are graded on the same score.
2. FIX. Every round with a fixable gap is re-synced on the spot — fixtures,
   then results — through the same code the scheduled syncs use.
3. RE-CHECK. The same comparison again. Anything still wrong is "unresolved".
4. TELL SOMEONE. Unresolved errors email the staff who run syncs, once per
   distinct problem (not every hour it persists), with an all-clear when it
   resolves. Everything is on /manage/sync/ either way.

It also watches the watcher: a scheduler that has stopped running, or a feed
that has gone silent while games are being played, is an error in its own
right — neither shows up as a failed sync, because neither produces a sync.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger("data_sync")

#: A feed game this far past kickoff, reported finished, must be graded here.
#: Past the length of any game plus two results polls.
RESULT_GRACE = timedelta(hours=4)
#: Kickoffs further apart than this are a moved game, not rounding.
KICKOFF_TOLERANCE = timedelta(minutes=2)
#: The scheduler kinds that must keep ticking, and how stale is too stale.
SCHEDULER_LIMITS = {"fixtures": timedelta(hours=3), "live": timedelta(minutes=30)}
#: Re-send an unchanged alert after this long, so a fault nobody fixed is not
#: forgotten after one email.
REALERT_AFTER = timedelta(hours=12)

ERROR, WARNING = "error", "warning"

_LABELS = {
    "missing_round": "Round missing",
    "missing_match": "Game missing",
    "kickoff_drift": "Kickoff time differs",
    "result_missing": "Result not graded",
    "score_mismatch": "Score differs",
    "unknown_club": "Club name not recognised",
    "orphan_match": "Game not in the feed",
    "feed_silent": "Feed returned nothing",
    "feed_error": "Feed could not be read",
    "scheduler_stalled": "Scheduler not running",
}
#: Fixed by re-syncing the round. The rest need a person.
_HEALABLE = {"missing_round", "missing_match", "kickoff_drift", "result_missing", "score_mismatch"}


def _issue(kind, *, severity=ERROR, series="", year=None, round_number=None,
           orgs=(), detail="", competition=""):
    return {
        "kind": kind,
        "label": _LABELS.get(kind, kind),
        "severity": severity,
        "competition": competition,
        "series": series,
        "year": year,
        "round": round_number,
        "orgs": sorted(set(orgs)),
        "detail": detail,
    }


def _in_use():
    """{competition: {(series_name, year): [org ids]}} for every league with a season."""
    from orgs.models import Organisation

    from .services import competition_for_series

    use: dict[str, dict[tuple[str, int], list[int]]] = defaultdict(lambda: defaultdict(list))
    orgs = (
        Organisation.objects.exclude(season__isnull=True)
        .select_related("season").prefetch_related("competitions__series")
    )
    for org in orgs:
        for comp in org.competitions.all():
            for s in comp.series.all():
                feed = competition_for_series(s.name)
                if feed:
                    use[feed][(s.name, org.season.year)].append(org.id)
    return use


def _compare_round(svc, competition, series, year, round_number, rows, org_ids, now):
    """Issues for one feed round against every league that tips it."""
    from tipping.models import Match, Round

    issues = []
    resolved = []
    for r in rows:
        home = svc.resolve_team(series, r["home_name"], r["home_external_id"])
        away = svc.resolve_team(series, r["away_name"], r["away_external_id"])
        if not home or not away:
            name = r["away_name"] if home else r["home_name"]
            issues.append(_issue(
                "unknown_club", competition=competition, series=series.name, year=year,
                round_number=round_number, orgs=org_ids,
                detail=f"The feed calls a club {name!r}, which matches no {series.name} club here. "
                       "Add it to the team aliases in data_sync/services.py.",
            ))
            continue
        resolved.append((r, home, away))

    rounds = {
        r.org_id: r for r in Round.objects.filter(
            org_id__in=org_ids, series=series, round_number=round_number,
        )
    }
    per_kind: dict[tuple[str, str], list[int]] = defaultdict(list)
    for org_id in org_ids:
        round_obj = rounds.get(org_id)
        if round_obj is None:
            per_kind[("missing_round", "")].append(org_id)
            continue
        index = svc.match_index(round_obj)
        seen = set()
        for r, home, away in resolved:
            match = svc.lookup(index, r["external_id"], home, away)
            game = f"{home.name} v {away.name}"
            if match is None:
                per_kind[("missing_match", game)].append(org_id)
                continue
            seen.add(match.id)
            if abs(match.kickoff_at - r["kickoff_at"]) > KICKOFF_TOLERANCE:
                per_kind[("kickoff_drift", f"{game}: feed "
                          f"{timezone.localtime(r['kickoff_at']):%a %d %b %H:%M}, here "
                          f"{timezone.localtime(match.kickoff_at):%a %d %b %H:%M}")].append(org_id)
            finished = (r["status"] == "complete" and r["home_score"] is not None
                        and r["kickoff_at"] < now - RESULT_GRACE)
            if finished:
                if match.result is None:
                    per_kind[("result_missing", f"{game} ({r['home_score']}–{r['away_score']})")].append(org_id)
                elif (match.home_score, match.away_score) != (int(r["home_score"]), int(r["away_score"])):
                    per_kind[("score_mismatch", f"{game}: feed {r['home_score']}–{r['away_score']}, "
                              f"here {match.home_score}–{match.away_score}")].append(org_id)
        # A game we hold that the feed no longer lists in this round — moved to
        # another round, or something seeded by hand. Worth seeing; not
        # something a re-sync can decide on its own.
        held = {m.id: m for part in index for m in part.values()}
        extra = [m for m in held.values()
                 if m.id not in seen and m.status != Match.STATUS_POSTPONED]
        for m in extra:
            per_kind[("orphan_match", f"{m.home_team.name} v {m.away_team.name}")].append(org_id)

    for (kind, detail), orgs in per_kind.items():
        issues.append(_issue(
            kind, severity=WARNING if kind == "orphan_match" else ERROR,
            competition=competition, series=series.name, year=year,
            round_number=round_number, orgs=orgs, detail=detail,
        ))
    return issues, len(resolved)


def _feed_has_gone_quiet(series, year, now) -> bool:
    """Games here are being played, but the feed published nothing around today."""
    from tipping.models import Match

    return Match.objects.filter(
        round__series=series, round__org__season__year=year,
        kickoff_at__gte=now - timedelta(days=4), kickoff_at__lte=now + timedelta(days=4),
    ).exists()


def _compare(full_season, *, only=None, services=None):
    """(issues, games_checked, services). ``only`` limits to {(competition, series, year, round)}."""
    from catalog.models import Series

    from .services import SyncError, get_sync_service

    now = timezone.now()
    services = services if services is not None else {}
    issues, games = [], 0
    for competition, pairs in _in_use().items():
        try:
            svc = services.get(competition) or get_sync_service(competition)
        except SyncError:
            continue
        services[competition] = svc
        for (series_name, year), org_ids in sorted(pairs.items()):
            series = Series.objects.filter(name=series_name).first()
            if series is None:
                continue
            if only is not None:
                numbers = sorted(rn for (c, s, y, rn) in only
                                 if (c, s, y) == (competition, series_name, year))
                if not numbers:
                    continue
            else:
                try:
                    numbers = svc.published_rounds(series_name, year, full_season=full_season)
                except Exception as e:  # noqa: BLE001 — a feed fault is a finding, not a crash
                    issues.append(_issue("feed_error", competition=competition, series=series_name,
                                         year=year, orgs=org_ids, detail=str(e)[:300]))
                    continue
                if not numbers:
                    if _feed_has_gone_quiet(series, year, now):
                        issues.append(_issue(
                            "feed_silent", competition=competition, series=series_name, year=year,
                            orgs=org_ids,
                            detail=f"{series_name} games are scheduled around today, but the feed "
                                   "listed no rounds. The site's feed may have changed.",
                        ))
                    continue
            for rn in numbers:
                try:
                    _s, rows = svc.feed_rows(series_name, rn, year, fresh=not full_season)
                except Exception as e:  # noqa: BLE001
                    issues.append(_issue("feed_error", competition=competition, series=series_name,
                                         year=year, round_number=rn, orgs=org_ids, detail=str(e)[:300]))
                    continue
                if not rows:
                    continue
                found, n = _compare_round(svc, competition, series, year, rn, rows, org_ids, now)
                issues.extend(found)
                games += n
    return issues, games, services


def _scheduler_issues():
    from .models import SyncSchedule

    now = timezone.now()
    issues = []
    for kind, limit in SCHEDULER_LIMITS.items():
        row = SyncSchedule.objects.filter(kind=kind).first()
        last = row.last_started_at if row else None
        if last is None or now - last > limit:
            when = timezone.localtime(last).strftime("%a %d %b %H:%M") if last else "never"
            issues.append(_issue(
                "scheduler_stalled",
                detail=f"The {kind} sync last started {when}. The server timer "
                       "(goodtip-matchsync) may have stopped.",
            ))
    return issues


def _heal(issues, services) -> int:
    """Re-sync every round with a fixable gap. Returns rounds re-synced."""
    from orgs.models import Organisation

    from .models import SyncRun
    from .services import SyncError

    targets = sorted({
        (i["competition"], org_id, i["round"])
        for i in issues if i["kind"] in _HEALABLE and i["round"] is not None
        for org_id in i["orgs"]
    })
    orgs = {o.id: o for o in Organisation.objects.select_related("season").filter(
        id__in={t[1] for t in targets})}
    healed = 0
    laddered: set[tuple[str, int]] = set()  # (competition, org_id) already rebuilt
    for competition, org_id, rn in targets:
        org, svc = orgs.get(org_id), services.get(competition)
        if org is None or svc is None:
            continue
        for kind in ("fixtures", "results"):
            try:
                with SyncRun.record(kind=kind, competition=competition, org=org, round_number=rn) as run:
                    run.matches_touched = getattr(svc, f"sync_{kind}")(
                        competition=competition, round_number=rn, org=org)
            except SyncError as e:
                logger.warning("feed check heal %s %s r%s org=%s: %s", kind, competition, rn, org_id, e)
        healed += 1
        # Standings read off the results just re-synced above — rebuilt here
        # rather than waiting out the 30-minute ladder cadence, so a gap this
        # check just closed cannot still read wrong on the ladder for half an
        # hour after the fixtures and results themselves are already right.
        ladder_key = (competition, org_id)
        if ladder_key not in laddered:
            laddered.add(ladder_key)
            try:
                with SyncRun.record(kind="ladder", competition=competition, org=org) as run:
                    run.matches_touched = svc.sync_ladder(competition=competition, org=org)
            except Exception as e:  # noqa: BLE001 — best-effort: the 30-minute
                # ladder cadence is still the backstop, and a rebuild fault
                # must never cost the fixtures/results healing already done.
                logger.warning("feed check heal ladder %s org=%s: %s", competition, org_id, e)
    return healed


def _signature(errors) -> str:
    keys = sorted(json.dumps([i["kind"], i["series"], i["year"], i["round"], i["detail"]]) for i in errors)
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:64] if keys else ""


def _alert(check) -> bool:
    """Email the people who run syncs, once per distinct problem. True if sent."""
    from accounts.models import User
    from django.urls import reverse
    from goodtip.mail import send_template, site_url
    from sysadmin import access as admin_access

    from .models import FeedCheck

    previous = (
        FeedCheck.objects.filter(alerted=True, finished_at__isnull=False)
        .exclude(pk=check.pk).order_by("-started_at").first()
    )
    errors = check.unresolved_errors
    if errors:
        unchanged = previous and previous.signature == check.signature
        if unchanged and timezone.now() - previous.started_at < REALERT_AFTER:
            return False
        subject = f"Fixtures need attention — {len(errors)} problem{'s' if len(errors) != 1 else ''} the sync could not fix"
        template = "feed_check_problem"
    else:
        # All clear — but only worth saying if the last thing we said was a problem.
        if not previous or not previous.signature:
            return False
        subject = "Fixtures back in step with the feeds"
        template = "feed_check_clear"

    to = [
        u.email for u in User.objects.filter(is_staff=True, is_active=True).exclude(email="")
        if admin_access.can(u, "data.sync")
    ]
    if not to:
        return False
    try:
        send_template(template, subject=subject, to=to, context={
            "check": check, "errors": errors, "warnings": check.unresolved_warnings,
            "panel_link": site_url(reverse("admin:hq_sync")),
        })
    except Exception:  # noqa: BLE001 — the check is on record; a mail fault must not lose it
        logger.exception("feed check %s: alert email failed", check.pk)
        return False
    return True


def run_check(*, full_season: bool = False, heal: bool = True, alert: bool = True):
    """Compare, fix, re-check, tell. Returns the saved FeedCheck."""
    from .models import FeedCheck

    check = FeedCheck.objects.create(
        scope=FeedCheck.SCOPE_SEASON if full_season else FeedCheck.SCOPE_WINDOW,
    )
    try:
        issues, games, services = _compare(full_season)
        issues += _scheduler_issues()
        check.games_checked = games
        check.found = issues
        if heal and any(i["kind"] in _HEALABLE for i in issues):
            check.healed = _heal(issues, services)
            # Re-check only what was found wrong, against the same feed pages.
            touched = {(i["competition"], i["series"], i["year"], i["round"])
                       for i in issues if i["round"] is not None}
            after, _n, _ = _compare(full_season, only=touched, services=services)
            unfixable = [i for i in issues if i["round"] is None or i["kind"] not in _HEALABLE]
            # unfixable issues tied to a round are re-reported by the re-check
            # itself, so keep only the roundless ones from the first pass.
            check.unresolved = after + [i for i in unfixable if i["round"] is None]
        else:
            check.unresolved = issues
        check.signature = _signature(check.unresolved_errors)
        check.ok = not check.unresolved_errors
    except Exception as e:  # noqa: BLE001 — record it; the next hour tries again
        logger.exception("feed check crashed")
        check.error = f"{type(e).__name__}: {e}"[:2000]
        check.ok = False
        check.unresolved = [_issue("feed_error", detail=check.error)]
        check.signature = _signature(check.unresolved)
    check.finished_at = timezone.now()
    check.save()
    if alert and _alert(check):
        check.alerted = True
        check.save(update_fields=["alerted"])
    return check
