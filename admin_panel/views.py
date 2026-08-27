import html

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.files.storage import default_storage
from django.db.models import Q
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.html import linebreaks, strip_tags

from catalog.models import Charity, Competition, Season, Series, Sport
from data_sync.models import SyncRun, SyncSchedule
from data_sync.services import get_sync_service, SyncError
from orgs.forms import _unique_charity_slug
from orgs.models import MembershipRequest, OrgMember, Organisation
from orgs.services import approve_membership_request, decline_membership_request
from orgs.signing import make_join_token
from tipping.models import Match, Round, Team, Tip
from tipping.services import record_match_result


# Map the org-creation form's value to one or more Competition slugs.
COMP_FORM_MAP = {"AFL": ["afl"], "NRL": ["nrl"], "BOTH": ["afl", "nrl"]}


@staff_member_required
def overview(request):
    org_count = Organisation.objects.count()
    round_count = Round.objects.count()
    match_count = Match.objects.count()
    tip_count = Tip.objects.count()
    recent_orgs = Organisation.objects.order_by("-created_at")[:5]
    return render(request, "manage/overview.html", {
        "org_count": org_count, "round_count": round_count,
        "match_count": match_count, "tip_count": tip_count,
        "recent_orgs": recent_orgs,
    })


@staff_member_required
def approvals(request):
    """Every join request waiting on somebody, across all orgs.

    Approval is normally the org admin's job on their own Members page, but
    that leaves a request stuck when a group's admin is away or the group has
    no admin at all. This is the staff view of the same queue — one list, act
    on any of it, so nothing sits pending unnoticed.
    """
    if request.method == "POST":
        join_req = get_object_or_404(
            MembershipRequest, pk=request.POST.get("request_id"),
        )
        action = request.POST.get("action")
        try:
            if action == "approve":
                approve_membership_request(join_req, by_user=request.user)
                messages.success(
                    request,
                    f"{join_req.user.display_name} is now a member of {join_req.org.name}.",
                )
            elif action == "decline":
                decline_membership_request(join_req, by_user=request.user)
                messages.info(
                    request,
                    f"{join_req.user.display_name}'s request to {join_req.org.name} was declined.",
                )
        except ValueError as e:
            messages.info(request, str(e))
        return redirect("manage:approvals")

    pending = (
        MembershipRequest.objects
        .filter(status=MembershipRequest.STATUS_PENDING)
        .select_related("user", "org")
        .order_by("org__name", "created_at")
    )
    # Who else could act on each one — an empty list is the reason a request is
    # sitting here, so it's worth showing rather than leaving staff to guess.
    admins_by_org = {}
    for m in (
        OrgMember.objects
        .filter(org__in=[r.org_id for r in pending])
        .filter(Q(role__in=[OrgMember.ROLE_MANAGER, OrgMember.ROLE_BOTH]) | Q(is_league_owner=True))
        .select_related("user", "org")
    ):
        admins_by_org.setdefault(m.org_id, []).append(m.user)

    rows = [{"req": r, "admins": admins_by_org.get(r.org_id, [])} for r in pending]
    recent = (
        MembershipRequest.objects
        .exclude(status=MembershipRequest.STATUS_PENDING)
        .select_related("user", "org", "decided_by")
        .order_by("-decided_at")[:10]
    )
    return render(request, "manage/approvals.html", {
        "rows": rows, "pending_count": len(rows), "recent": recent,
    })


@staff_member_required
def orgs_list(request):
    if request.method == "POST":
        season, _ = Season.objects.get_or_create(
            year=int(request.POST["season"]),
            defaults={"label": request.POST["season"].strip()},
        )
        charity_name = request.POST["charity_name"].strip()
        charity = Charity.objects.filter(name__iexact=charity_name).first()
        if charity is None:
            charity = Charity.objects.create(
                name=charity_name,
                slug=_unique_charity_slug(charity_name),
                website=request.POST.get("charity_url", "").strip(),
                is_approved=True,
            )
            # Same as the signup wizard: find the logo off the request thread.
            from catalog.logos import backfill_in_background

            backfill_in_background(charity)
        org = Organisation.objects.create(
            name=request.POST["name"].strip(),
            season=season,
            charity=charity,
        )
        comp_slugs = COMP_FORM_MAP.get(request.POST["sport"], [])
        org.competitions.set(Competition.objects.filter(slug__in=comp_slugs, season=season))
        messages.success(request, "Org created.")
        return redirect("manage:orgs_list")
    orgs = (
        Organisation.objects
        .select_related("charity", "season")
        .prefetch_related("competitions__sport")
        .order_by("-created_at")
    )
    # The list doubles as a finder once there are more than a screenful:
    # free-text over org and charity name, plus a sport filter.
    q = (request.GET.get("q") or "").strip()
    if q:
        orgs = orgs.filter(Q(name__icontains=q) | Q(charity__name__icontains=q))
    sport = (request.GET.get("sport") or "").strip()
    if sport:
        orgs = orgs.filter(competitions__sport__name=sport)
    orgs = orgs.distinct()
    sports = list(
        Sport.objects.filter(competitions__organisations__isnull=False)
        .order_by("name").values_list("name", flat=True).distinct()
    )
    return render(request, "manage/orgs_list.html", {
        "orgs": orgs, "q": q, "sport": sport, "sports": sports,
    })


@staff_member_required
def org_rounds(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if request.method == "POST":
        action = request.POST.get("action", "create")
        if action == "create":
            series = get_object_or_404(Series, pk=int(request.POST["series"]))
            Round.objects.create(
                org=org,
                round_number=int(request.POST["round_number"]),
                series=series,
                competition=Competition.for_series(series, org.season),
                stage=request.POST.get("stage", Round.STAGE_REGULAR),
                lockout_at=request.POST["lockout_at"],
                status=request.POST.get("status", "upcoming"),
            )
            messages.success(request, "Round created.")
        elif action == "status":
            r = get_object_or_404(Round, pk=int(request.POST["round_id"]), org=org)
            r.status = request.POST["status"]
            r.save(update_fields=["status"])
            messages.success(request, "Status updated.")
        return redirect("manage:org_rounds", org_id=org.id)
    rounds = Round.objects.filter(org=org).order_by("-round_number")
    return render(request, "manage/org_rounds.html", {"org": org, "rounds": rounds})


@staff_member_required
def round_matches(request, org_id: int, round_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    round_obj = get_object_or_404(Round, pk=round_id, org=org)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            home = get_object_or_404(Team, pk=int(request.POST["home_team"]))
            away = get_object_or_404(Team, pk=int(request.POST["away_team"]))
            Match.objects.create(
                round=round_obj, home_team=home, away_team=away,
                kickoff_at=request.POST["kickoff_at"],
                venue=request.POST.get("venue", "").strip(),
            )
            messages.success(request, "Match created.")
        elif action == "result":
            match = get_object_or_404(Match, pk=int(request.POST["match_id"]), round=round_obj)
            try:
                hs = int(request.POST["home_score"])
                as_ = int(request.POST["away_score"])
            except (KeyError, ValueError):
                messages.error(request, "Invalid scores.")
                return redirect("manage:round_matches", org_id=org.id, round_id=round_obj.id)
            n = record_match_result(match, hs, as_)
            messages.success(request, f"Result saved. {n} tip(s) graded.")
        return redirect("manage:round_matches", org_id=org.id, round_id=round_obj.id)
    matches = round_obj.matches.select_related("home_team", "away_team").order_by("kickoff_at")
    # Teams from any series under the same sport (e.g. an AFL round can draw on AFL + AFLW).
    teams = Team.objects.filter(series__sport=round_obj.series.sport).select_related("series")
    return render(request, "manage/round_matches.html", {
        "org": org, "round": round_obj, "matches": matches, "teams": teams,
    })


@staff_member_required
def org_members(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "remove":
            OrgMember.objects.filter(org=org, id=int(request.POST["member_id"])).delete()
            messages.success(request, "Member removed.")
        elif action == "promote":
            m = OrgMember.objects.filter(org=org, id=int(request.POST["member_id"])).first()
            if m:
                # Toggle between Participant and Manager+Captain.
                m.role = OrgMember.ROLE_PARTICIPANT if m.is_manager else OrgMember.ROLE_BOTH
                m.save(update_fields=["role"])
        return redirect("manage:org_members", org_id=org.id)
    members = OrgMember.objects.filter(org=org).select_related("user").order_by("joined_at")
    token = make_join_token(org.id)
    join_url = request.build_absolute_uri(reverse("join_org", args=[org.id, token]))
    return render(request, "manage/org_members.html", {
        "org": org, "members": members, "join_url": join_url,
    })


@staff_member_required
def sync_panel(request):
    msg = None
    if request.method == "POST":
        comp = request.POST["competition"]
        round_number = int(request.POST["round_number"])
        org = get_object_or_404(Organisation, pk=int(request.POST["org_id"]))
        kind = request.POST.get("kind", "fixtures")
        LABELS = {"fixtures": "fixtures", "results": "results", "live": "live scores"}
        if kind not in LABELS:
            kind = "fixtures"
        try:
            # Recorded like a scheduled run, so a manual sync shows up in the
            # same history and refreshes the same "last updated" stamps.
            with SyncRun.record(kind=kind, competition=comp, org=org, round_number=round_number) as run:
                svc = get_sync_service(comp)
                n = getattr(svc, f"sync_{kind}")(
                    competition=comp, round_number=round_number, org=org,
                )
                run.matches_touched = n
            messages.success(request, f"Synced {n} {LABELS[kind]}.")
        except SyncError as e:
            messages.error(request, str(e))
        return redirect("manage:sync")
    orgs = Organisation.objects.select_related("season").all()
    return render(request, "manage/sync.html", {
        "orgs": orgs,
        # No feed needs a key any more — both sources are scraped. Kept true so
        # the template's "feed configured" branch stays correct without needing
        # a change in two places; what actually matters now is the freshness
        # below, which reports whether the scrapers are still working.
        "thesports_ready": True,
        # Freshness per feed kind, so "are the games up to date?" is answerable
        # from the panel rather than from the server logs.
        "last_live": SyncRun.last_success(kind=SyncRun.KIND_LIVE),
        "last_results": SyncRun.last_success(kind=SyncRun.KIND_RESULTS),
        "last_fixtures": SyncRun.last_success(kind=SyncRun.KIND_FIXTURES),
        "last_ladder": SyncRun.last_success(kind=SyncRun.KIND_LADDER),
        "last_backfill": SyncRun.last_success(kind=SyncRun.KIND_BACKFILL),
        "recent_runs": SyncRun.objects.select_related("org")[:12],
        # What the scheduler thinks it should be doing, and what each feed
        # actually holds. Freshness stamps alone answer "did it run", never
        # "is the data complete" — and the season-long holes that made every
        # ladder wrong were invisible precisely because every run said ok.
        "schedules": SyncSchedule.objects.all(),
        "coverage": _feed_coverage(),
    })


def _feed_coverage():
    """Per-series: rounds held, results in, and whether a feed exists at all.

    Built for the question the freshness stamps cannot answer — "is anything
    missing?" A sync that runs every two minutes and reports success can still
    be leaving half a season unfetched, which is exactly what was happening.

    Series with no feed are listed rather than hidden. Six leagues are signed
    up to Super League and Super Netball, which have no scraper and no teams,
    so their fixtures are never coming; saying so here is the difference
    between a known limitation and a bug somebody rediscovers every month.
    """
    from catalog.models import Series
    from data_sync.services import competition_for_series
    from matchreader.models import HistoricalMatch
    from tipping.models import LadderEntry, Round

    season = Season.objects.order_by("-year").first()
    rows = []
    for series in Series.objects.select_related("sport").order_by("name"):
        feed = competition_for_series(series.name)
        numbers = sorted(set(
            Round.objects.filter(series=series, org__season=season)
            .values_list("round_number", flat=True)
        ))
        held = len(numbers)
        # A hole is a round number the feed skipped over — present on either
        # side, absent in the middle. That is the shape the rolling-window
        # discovery used to leave behind, so it is worth naming precisely.
        missing = [n for n in range(1, max(numbers) + 1) if n not in numbers] if numbers else []
        rows.append({
            "series": series,
            "feed": feed,
            "rounds": held,
            "missing": missing,
            "results": HistoricalMatch.objects.filter(
                series=series, season=season.year if season else 0
            ).count(),
            "ladder": LadderEntry.objects.filter(series=series, season=season).count(),
        })
    return rows


# ---------------------------------------------------------------------------
# News & blog — super admin only (the platform owner, not group admins and
# not ordinary staff). Posts feed the member dashboard.
# ---------------------------------------------------------------------------
from django.contrib.auth.decorators import user_passes_test  # noqa: E402

from .models import NewsPost  # noqa: E402
from .sanitize import sanitize_editor_html  # noqa: E402

superuser_required = user_passes_test(
    lambda u: u.is_active and u.is_superuser, login_url="/admin/login/"
)


def _plain_text(html_value: str) -> str:
    """The plain-text reading of an editor surface.

    strip_tags alone is not enough: the surface stores entities, so a headline
    typed as "Tips & tricks" comes back as "Tips &amp; tricks" and that literal
    string then gets escaped a second time wherever it is printed — in the page
    <title>, the slug, the email subject and the OG tags. Unescaping here means
    the plain-text mirror holds the characters the author actually typed.
    """
    text = html.unescape(strip_tags(html_value or ""))
    # A contenteditable emits &nbsp; freely; collapse it with the rest.
    return " ".join(text.replace("\xa0", " ").split())


def _headline_text(request) -> str:
    """Plain-text version of the rich headline, for the empty-headline guard."""
    return _plain_text(request.POST.get("title_html", ""))


def _parse_sources(post_data) -> list:
    labels = post_data.getlist("source_label")
    urls = post_data.getlist("source_url")
    sources = []
    for label, url in zip(labels, urls):
        url = url.strip()
        if not url:
            continue
        sources.append({"label": label.strip() or url, "url": url})
    return sources


def _fill_news_post(request, post: NewsPost) -> NewsPost:
    """Populate `post` from POST data. Caller decides whether to save it."""
    title_html = sanitize_editor_html(request.POST.get("title_html", "").strip())
    post.title_html = title_html
    post.title = _plain_text(title_html)
    post.tag = request.POST.get("tag", "NEWS")
    excerpt_html = sanitize_editor_html(request.POST.get("excerpt_html", "").strip())
    post.excerpt_html = excerpt_html
    post.excerpt = _plain_text(excerpt_html)
    post.body = sanitize_editor_html(request.POST.get("body", "").strip())
    post.sources = _parse_sources(request.POST)
    post.is_published = bool(request.POST.get("is_published"))
    if request.FILES.get("image"):
        post.image = request.FILES["image"]
    elif request.POST.get("image_clear"):
        # The drop zone's Remove button. Only the reference is dropped — the
        # file itself stays on disk, since an image is easy to re-point at and
        # impossible to get back.
        post.image = None
    return post


def _apply_news_form(request, post: NewsPost) -> NewsPost:
    _fill_news_post(request, post)
    post.save()
    return post


def _editor_body_html(body: str) -> str:
    """The story editor is a contenteditable surface, so it needs real HTML.

    Posts written before the rich editor existed have a plain-text body — one
    wrapping <p> per line reproduces how it already renders on the article
    page (see the news_detail templates' linebreaks fallback) instead of
    dumping it into the editor as one unbroken run of text.
    """
    if not body:
        return ""
    if "<" in body:
        return body
    return linebreaks(body)


@superuser_required
def news_list(request):
    # Each row carries the story's own public URL so it can be copied straight
    # from the list — the point of the auto-generated slug is that the link is
    # ready to share the moment the post is saved.
    posts = list(NewsPost.objects.all())
    for post in posts:
        post.share_url = request.build_absolute_uri(post.get_absolute_url())
    return render(request, "manage/news.html", {"posts": posts})


@superuser_required
def news_new(request):
    if request.method == "POST":
        if not _headline_text(request):
            messages.error(request, "Give the story a headline before saving.")
            draft = _fill_news_post(request, NewsPost())
            return render(request, "manage/news_editor.html", {
                "post": draft, "tag_choices": NewsPost.TAG_CHOICES, "is_new": True,
                "initial_body": draft.body,
            })
        post = NewsPost(created_by=request.user, published_at=timezone.now())
        _apply_news_form(request, post)
        messages.success(request, "Post published." if post.is_published else "Post saved as a draft.")
        return redirect("manage:news")
    return render(request, "manage/news_editor.html", {
        "tag_choices": NewsPost.TAG_CHOICES, "is_new": True,
    })


@superuser_required
def news_edit(request, post_id: int):
    post = get_object_or_404(NewsPost, pk=post_id)
    if request.method == "POST":
        if not _headline_text(request):
            messages.error(request, "Give the story a headline before saving.")
            draft = _fill_news_post(request, post)
            return render(request, "manage/news_editor.html", {
                "post": draft, "tag_choices": NewsPost.TAG_CHOICES, "is_new": False,
                "initial_body": draft.body,
            })
        _apply_news_form(request, post)
        messages.success(request, "Post updated.")
        return redirect("manage:news")
    return render(request, "manage/news_editor.html", {
        "post": post, "tag_choices": NewsPost.TAG_CHOICES, "is_new": False,
        "initial_body": _editor_body_html(post.body),
        "share_url": request.build_absolute_uri(post.get_absolute_url()),
    })


@superuser_required
def news_upload_image(request):
    """Inline image upload for the story editor — returns the URL to insert."""
    f = request.FILES.get("file")
    if request.method != "POST" or not f:
        return JsonResponse({"error": "No file given."}, status=400)
    if not (f.content_type or "").startswith("image/"):
        return JsonResponse({"error": "That's not an image."}, status=400)
    path = default_storage.save(f"news/body/{f.name}", f)
    return JsonResponse({"url": default_storage.url(path)})


@superuser_required
def news_toggle(request, post_id: int):
    post = get_object_or_404(NewsPost, pk=post_id)
    if request.method == "POST":
        post.is_published = not post.is_published
        post.save(update_fields=["is_published"])
        messages.success(request, "Post published." if post.is_published else "Post unpublished.")
    return redirect("manage:news")


@superuser_required
def news_announce(request, post_id: int):
    """Email a published post to every member — once.

    Kept separate from publishing on purpose: publishing is reversible and gets
    toggled, and nobody should get the same announcement twice because an admin
    unpublished and republished a post.
    """
    post = get_object_or_404(NewsPost, pk=post_id)
    if request.method != "POST":
        return redirect("manage:news")

    if not post.is_published:
        messages.error(request, "Publish the post before emailing it out.")
    elif post.announced_at:
        stamp = timezone.localtime(post.announced_at)
        messages.info(request, f"Already emailed to members on {stamp:%d %b %Y at %H:%M}.")
    else:
        from accounts.models import User
        from orgs.notifications import send_news_published

        recipients = User.objects.filter(is_active=True).exclude(email="")
        sent = send_news_published(post, recipients)
        if sent:
            post.announced_at = timezone.now()
            post.save(update_fields=["announced_at"])
            messages.success(request, f"Emailed to {sent} member{'s' if sent != 1 else ''}.")
        else:
            messages.error(
                request,
                "Nothing sent — check the email settings and the server log.",
            )
    return redirect("manage:news")


@superuser_required
def news_delete(request, post_id: int):
    post = get_object_or_404(NewsPost, pk=post_id)
    if request.method == "POST":
        post.delete()
        messages.success(request, "Post deleted.")
    return redirect("manage:news")


# ---- member-facing news pages (any logged-in user) ------------------------
from django.contrib.auth.decorators import login_required  # noqa: E402


def news_index(request):
    """All published posts. Public page (client site structure) — anonymous
    visitors get the marketing design, members the in-app feed. The staging
    gate still fronts it until launch.
    """
    posts = NewsPost.objects.filter(is_published=True)
    # ?code=AFL filters by tag; anything unknown falls back to all stories.
    valid_tags = {t for t, _ in NewsPost.TAG_CHOICES}
    active_tag = (request.GET.get("code") or "").upper()
    if active_tag in valid_tags:
        posts = posts.filter(tag=active_tag)
    else:
        active_tag = ""
    tpl = "news_index.html" if request.user.is_authenticated else "public/news_index.html"
    return render(request, tpl, {
        "posts": posts,
        "active": "news",
        "news_tags": NewsPost.TAG_CHOICES,
        "active_tag": active_tag,
    })


def news_detail(request, slug: str):
    """Full story page. Public, same split as the index.

    Also the page link-preview crawlers (Facebook, LinkedIn, Slack…) hit when
    a story is shared, since those requests are always anonymous — so the
    absolute image/URL built here for the Open Graph tags need to be right on
    the public template every time, not just for logged-in members.
    """
    post = get_object_or_404(NewsPost, slug=slug, is_published=True)
    more = NewsPost.objects.filter(is_published=True).exclude(pk=post.pk)[:5]
    tpl = "news_detail.html" if request.user.is_authenticated else "public/news_detail.html"
    return render(request, tpl, {
        "post": post, "more": more, "active": "news",
        "share_url": request.build_absolute_uri(),
        "share_image_url": request.build_absolute_uri(post.image.url) if post.image else "",
    })


# ---------------------------------------------------------------------------
# Customer enquiries
# ---------------------------------------------------------------------------

# The manage area normally leans on staff_member_required's default, which
# sends you to Django's own /admin/login/. That is fine for pages you reach by
# already being in the admin, but the enquiry link is emailed out and may well
# be opened cold on a phone — landing on a bare Django form that looks nothing
# like GoodTip, and that bypasses the normal email-code sign-in, is the wrong
# door. These two go to the real login, which already honours ?next=.
staff_login_required = staff_member_required(login_url=reverse_lazy("accounts:login"))


@staff_login_required
def enquiries(request):
    """The enquiry inbox.

    Unanswered first regardless of age, because the thing that matters about an
    enquiry is whether somebody has dealt with it, not when it arrived — a
    three-day-old unanswered message should not be buried under this morning's
    answered ones.
    """
    from .models import Enquiry

    show = request.GET.get("show") or "open"
    qs = Enquiry.objects.select_related("replied_by")
    if show == "open":
        qs = qs.filter(status=Enquiry.STATUS_NEW)
    elif show == "replied":
        qs = qs.filter(status=Enquiry.STATUS_REPLIED)

    return render(request, "manage/enquiries.html", {
        "enquiries": qs[:200],
        "show": show,
        "open_count": Enquiry.objects.filter(status=Enquiry.STATUS_NEW).count(),
        "replied_count": Enquiry.objects.filter(status=Enquiry.STATUS_REPLIED).count(),
        "total_count": Enquiry.objects.count(),
    })


@staff_login_required
def enquiry_detail(request, enquiry_id):
    """One enquiry, and the box to answer it in.

    This is where the link in the notification email lands. It is behind
    staff_member_required, which is what makes that link work for someone not
    yet signed in: Django bounces them to the login page carrying this URL as
    ?next=, and drops them back here the moment they are through.
    """
    from django.utils import timezone as tz

    from goodtip.mail import send_template

    from .models import Enquiry

    enquiry = get_object_or_404(Enquiry, pk=enquiry_id)

    if request.method == "POST":
        action = request.POST.get("action") or "reply"

        if action == "close":
            enquiry.status = Enquiry.STATUS_CLOSED
            enquiry.save(update_fields=["status"])
            messages.success(request, "Enquiry closed without a reply.")
            return redirect("manage:enquiries")

        if action == "reopen":
            enquiry.status = Enquiry.STATUS_NEW
            enquiry.save(update_fields=["status"])
            messages.info(request, "Enquiry reopened.")
            return redirect("manage:enquiry_detail", enquiry_id=enquiry.pk)

        body = (request.POST.get("reply_body") or "").strip()
        if not body:
            messages.error(request, "Write something before sending.")
            return redirect("manage:enquiry_detail", enquiry_id=enquiry.pk)

        sent = send_template(
            "enquiry_reply",
            subject="Re: your GoodTip enquiry",
            to=enquiry.email,
            context={"enquiry": enquiry, "reply_body": body},
            # Their reply should reach a person, not the no-reply sender.
            reply_to=[request.user.email] if request.user.email else None,
        )

        if not sent:
            # Not marked replied. A reply that never left is not a reply, and
            # recording it as one is how an enquiry gets silently dropped —
            # it would vanish from the open list with nobody having been told.
            messages.error(
                request,
                "That couldn't be sent — the enquiry is still open. Check the mail "
                "settings and try again.",
            )
            return redirect("manage:enquiry_detail", enquiry_id=enquiry.pk)

        enquiry.reply_body = body
        enquiry.replied_at = tz.now()
        enquiry.replied_by = request.user
        enquiry.status = Enquiry.STATUS_REPLIED
        enquiry.save(update_fields=["reply_body", "replied_at", "replied_by", "status"])
        messages.success(request, f"Replied to {enquiry.name} at {enquiry.email}.")
        return redirect("manage:enquiries")

    return render(request, "manage/enquiry_detail.html", {"enquiry": enquiry})
