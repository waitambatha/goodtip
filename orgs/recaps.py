"""AI Group Recap — launch-minimum slice of docs/ai-group-recap-spec.md.

One short written summary of a group's tipping round, generated from real
results only, posted to the group's Wall as the pinned recap card. No group
activity, no recap; a failed model call falls back to one factual line.

Run via `manage.py generate_recaps` after results land (cron it next to
open_due_elections). Generation is batched per command run, never done on
page load.
"""

import json
import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

RECAP_MODEL = getattr(settings, "RECAP_MODEL", "claude-opus-4-8")

# §8–9 of the spec, compressed into the prompt itself — voice is enforced at
# generation time, not corrected after the fact.
RECAP_SYSTEM_PROMPT = """\
You write the round recap for GoodTip, an Australian workplace footy-tipping
platform where entry fees go to charity. You are given structured JSON with a
group's real results for one round. Write a 2 to 4 sentence recap of the
group's round.

Voice rules, all non-negotiable:
- Short, declarative sentences. Active verbs carry the result (backed, held
  on, ran down, came unstuck), not flat scorelines.
- Real names and real numbers from the data only. Never invent detail, never
  a vague magnitude without the figure next to it.
- No em dashes. Use full stops or commas.
- No weasel words: may, maybe, hope, wish, try, could, perhaps, strive.
- No triads ("no X, no Y, just Z"), no fragment-stacking, no rhetorical
  question openers, no exclamation marks, no emoji, no hashtags, no headline.
- British English spelling.
- Celebrate, don't preach. Never moralise about gambling, money or winning.
- Hunt for the one moment worth remarking on: a perfect round, a streak, a
  photo finish, a big leaderboard jump. Don't just report the ledger.
- One dry aside maximum, and only if the data earns it. Australian sporting
  vernacular in shared register (got the points, took the chocolates, kept it
  tight, came unstuck, nothing in it, steady as they come).
- Match the code's own language from round metadata. AFL/AFLW: got the four
  points, home and hosed, kicked a bag, speccie. NRL/NRLW: took the points,
  golden point, ripper, blinder. Never borrow the other code's terms and
  never disparage the other code. AFLW and NRLW get the same register as the
  men's game, no lesser weight.
- State losses and draws as evenly as wins. No sympathy, no scolding.
- No persisted character judgements about members, and no personality labels.
- If is_origin is true, call it State of Origin by name and reflect that
  correct picks were worth 4 points.
- If the group has no previous-round comparison data (first round), introduce
  the opening result without movement language.
- Vary sentence openers. Don't start with "This week" or "In round X".

Banned register: epic, incredible, legendary, dominant, seamless, elevated,
leverage, journey, moving forward, at this point in time.

Reply with the recap text only. Plain text, 2 to 4 sentences.
"""


# ---------------------------------------------------------------------------
# Readiness + facts (§3, §4)
# ---------------------------------------------------------------------------

def round_ready_for_recap(rnd) -> bool:
    """§3: the round's last match has locked and every result is final."""
    matches = list(rnd.matches.all())
    if not matches:
        return False
    if any(m.result is None for m in matches):
        return False
    return timezone.now() >= max(m.kickoff_at for m in matches)


def _season_ranks(org, cutoff):
    """Standings from all of the org's tips in rounds locked out at or before
    ``cutoff`` — same Sum(points_awarded) the leaderboard uses. Competition
    ranking with ties sharing a rank, matching user_rank_in_org."""
    from tipping.models import Tip

    rows = list(
        Tip.objects.filter(org=org, match__round__lockout_at__lte=cutoff)
        .values("user_id")
        .annotate(points=Sum("points_awarded"))
        .order_by("-points")
    )
    ranks, points = {}, {}
    last, rank = None, 0
    for i, row in enumerate(rows, start=1):
        if row["points"] != last:
            rank, last = i, row["points"]
        ranks[row["user_id"]] = rank
        points[row["user_id"]] = row["points"]
    return ranks, points


def build_recap_facts(org, rnd):
    """The structured JSON the model sees (§4). Real results only.

    Returns None when nobody in the group tipped this round — silence, not an
    apologetic card (§10).
    """
    from tipping.models import Tip

    round_tips = list(
        Tip.objects.filter(org=org, match__round=rnd)
        .select_related("user")
    )
    if not round_tips:
        return None

    per_member = {}
    for t in round_tips:
        m = per_member.setdefault(t.user_id, {
            "name": t.user.display_name or t.user.email,
            "correct": 0, "picks": 0, "round_points": 0,
        })
        m["picks"] += 1
        if t.is_correct:
            m["correct"] += 1
            m["round_points"] += t.points_awarded

    ranks_now, season_points = _season_ranks(org, rnd.lockout_at)
    ranks_prev, _ = _season_ranks(org, rnd.lockout_at - timezone.timedelta(seconds=1))
    prev_rounds_exist = bool(ranks_prev)

    match_count = rnd.matches.count()
    members = []
    for user_id, m in per_member.items():
        entry = {
            "name": m["name"],
            "correct": m["correct"],
            "picks": m["picks"],
            "round_points": m["round_points"],
            "season_points": season_points.get(user_id, m["round_points"]),
            "rank_now": ranks_now.get(user_id),
            "perfect_round": m["correct"] == match_count and match_count > 0,
        }
        if user_id in ranks_prev:
            entry["rank_before_round"] = ranks_prev[user_id]
            if entry["rank_now"] is not None:
                entry["moved"] = ranks_prev[user_id] - entry["rank_now"]  # +ve = climbed
        members.append(entry)
    members.sort(key=lambda e: (-e["round_points"], e["name"]))

    return {
        "round": {
            "number": rnd.round_number,
            "competition": rnd.competition.name if rnd.competition else rnd.series.name,
            "stage": rnd.stage,
            "is_origin": rnd.stage == rnd.STAGE_ORIGIN,
            "points_per_correct_pick": rnd.points_per_correct,
            "matches_in_round": match_count,
        },
        "group": {
            "name": org.name,
            "members_who_tipped": len(members),
            "first_round_for_group": not prev_rounds_exist,
        },
        "members": members,
    }


# ---------------------------------------------------------------------------
# Generation (§7) + fallback (§10)
# ---------------------------------------------------------------------------

def fallback_line(facts) -> str:
    """§10: one factual line built without the LLM — top scorer and round
    points only."""
    top = facts["members"][0]
    rnd = facts["round"]
    label = f"State of Origin round {rnd['number']}" if rnd["is_origin"] else f"round {rnd['number']}"
    return (
        f"{top['name']} topped {label} for {facts['group']['name']} with "
        f"{top['round_points']} points from {top['picks']} picks."
    )


def _clean_output(text: str) -> str:
    """Light §8 enforcement on the way out. The prompt does the real work;
    this only catches the mechanical slips."""
    text = " ".join(text.strip().split())
    text = text.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ")
    return text


def generate_recap_text(facts) -> str:
    """One constrained model call: structured JSON in, 2–4 plain sentences
    out. Raises on any API problem — the caller decides on the fallback."""
    import anthropic

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=RECAP_MODEL,
        max_tokens=1024,  # deliberately short output: 2–4 sentences
        system=[{
            "type": "text",
            "text": RECAP_SYSTEM_PROMPT,
            # Same frozen prompt for every group in the batch — cache it.
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{
            "role": "user",
            "content": json.dumps(facts, sort_keys=True, ensure_ascii=False),
        }],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("recap generation refused")
    text = _clean_output(
        "".join(b.text for b in response.content if b.type == "text")
    )
    if not text or len(text) > 900:
        raise RuntimeError("recap output failed validation")
    return text


def generate_recaps(org=None, dry_run=False):
    """Generate every recap that's due: rounds fully resolved, tips present,
    no recap yet. One per (org, round), posted to the Wall (§2, §3).

    Returns a list of (RoundRecap|facts, text) — with dry_run the text is
    printed by the caller and nothing is written.
    """
    from tipping.models import Round

    from .models import Organisation, RoundRecap, WallPost

    orgs = [org] if org is not None else list(Organisation.objects.all())
    results = []
    for o in orgs:
        done = set(RoundRecap.objects.filter(org=o).values_list("round_id", flat=True))
        candidate_rounds = (
            Round.objects.filter(org=o, lockout_at__lte=timezone.now())
            .exclude(id__in=done)
            .prefetch_related("matches")
            .select_related("competition", "series")
        )
        for rnd in candidate_rounds:
            if not round_ready_for_recap(rnd):
                continue
            facts = build_recap_facts(o, rnd)
            if facts is None:
                continue  # nobody tipped — silence (§10)
            if not settings.ANTHROPIC_API_KEY:
                # No key configured: don't burn the one-recap-per-round slot
                # on a fallback line that a real recap could replace later.
                logger.warning("ANTHROPIC_API_KEY unset — skipping recap for %s R%s", o, rnd.round_number)
                continue
            fallback = False
            try:
                text = generate_recap_text(facts)
            except Exception:
                logger.exception("Recap generation failed for %s R%s — using fallback line", o, rnd.round_number)
                text = fallback_line(facts)
                fallback = True
            if dry_run:
                results.append((facts, text))
                continue
            with transaction.atomic():
                post = WallPost.objects.create(
                    org=o, kind=WallPost.KIND_RECAP, body=text,
                )
                recap = RoundRecap.objects.create(
                    org=o, round=rnd, post=post,
                    fallback_used=fallback,
                    model_used="" if fallback else RECAP_MODEL,
                )
            results.append((recap, text))
    return results
