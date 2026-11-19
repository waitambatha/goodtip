"""Match reports: the facts of a finished game, and a short story told from them.

Fetched from ESPN's public match summary — the same API the fixtures come
from (data_sync.scrapers.espn) — once a game is graded. Only facts are kept:
who scored and when, who was sent off, the key players, possession and shots.
ESPN's prose and its photographs (Getty/PA) belong to them and are not stored.

    write_story(game)     -> MatchStory (one per real game, for everyone)
    story(match, report)  -> "Brighton beat ten-man Brentford 2–0 ..."
"""
import json
import logging
import urllib.request

from data_sync.scrapers.espn import BASE, LEAGUES

logger = logging.getLogger(__name__)

# The team numbers worth a line, per sport, in ESPN's own stat names.
STATS = {
    "soccer": [("possessionPct", "Possession", "%"), ("totalShots", "Shots", ""),
               ("shotsOnTarget", "On target", ""), ("wonCorners", "Corners", "")],
    "basketball": [("fieldGoalPct", "Field goal %", "%"), ("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "Threes", ""),
                   ("totalRebounds", "Rebounds", ""), ("assists", "Assists", "")],
    "football": [("totalYards", "Total yards", ""), ("netPassingYards", "Passing", ""),
                 ("rushingYards", "Rushing", ""), ("turnovers", "Turnovers", "")],
    "hockey": [("shotsTotal", "Shots", ""), ("powerPlayGoals", "Power-play goals", ""),
               ("hits", "Hits", ""), ("faceoffPercent", "Faceoffs", "%")],
}

GOAL_KINDS = {"Goal": "goal", "Goal - Header": "goal", "Goal - Free-kick": "goal",
              "Goal - Volley": "goal", "Penalty - Scored": "pen", "Own Goal": "own"}


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (GoodTip match reports)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _side(team_name: str, match) -> str | None:
    t = (team_name or "").lower()
    for side, team in (("home", match.home_team), ("away", match.away_team)):
        n = team.name.lower()
        if t and (t in n or n in t or t.split(" ")[0] == n.split(" ")[0]):
            return side
    return None


def parse_summary(data: dict, match, sport: str) -> dict:
    """The facts out of one ESPN summary, as MatchStory fields."""
    events = []
    for e in data.get("keyEvents") or []:
        kind_text = (e.get("type") or {}).get("text", "")
        kind = GOAL_KINDS.get(kind_text) or ("red" if kind_text == "Red Card" else None)
        if not kind:
            continue
        people = e.get("participants") or []
        player = ((people[0] or {}).get("athlete") or {}).get("displayName", "") if people else ""
        side = _side((e.get("team") or {}).get("displayName", ""), match)
        if kind == "own" and side:
            side = "away" if side == "home" else "home"     # an own goal counts for the other side
        events.append({"minute": (e.get("clock") or {}).get("displayValue", ""),
                       "kind": kind, "player": player, "side": side})

    stats = []
    teams = (data.get("boxscore") or {}).get("teams") or []
    by_side = {}
    for t in teams:
        side = _side((t.get("team") or {}).get("displayName", ""), match)
        if side:
            by_side[side] = {s.get("name"): s.get("displayValue") for s in t.get("statistics") or []}
    if len(by_side) == 2:
        for name, label, unit in STATS.get(sport, []):
            h, a = by_side["home"].get(name), by_side["away"].get(name)
            if h is None or a is None:
                continue
            row = {"label": label, "home": f"{h}{unit}", "away": f"{a}{unit}"}
            try:
                hv, av = float(str(h).split("-")[0]), float(str(a).split("-")[0])
                if hv + av > 0:
                    row["home_share"] = round(100 * hv / (hv + av))
            except ValueError:
                pass
            stats.append(row)

    leaders = []
    for block in data.get("leaders") or []:
        side = _side((block.get("team") or {}).get("displayName", ""), match)
        cats = block.get("leaders") or []
        if not side or not cats:
            continue
        top = (cats[0].get("leaders") or [{}])[0]
        who = (top.get("athlete") or {}).get("displayName")
        if who:
            leaders.append({"side": side, "player": who,
                            "what": f"{top.get('displayValue', '')} {cats[0].get('displayName', '').lower()}".strip()})

    periods = {}
    for comp in ((data.get("header") or {}).get("competitions") or [{}])[0].get("competitors") or []:
        side = _side((comp.get("team") or {}).get("displayName", ""), match)
        if side:
            vals = []
            for ls in comp.get("linescores") or []:
                try:
                    vals.append(int(float(ls.get("displayValue", ls.get("value", 0)))))
                except (TypeError, ValueError):
                    vals.append(0)
            periods[side] = vals

    info = data.get("gameInfo") or {}
    return {
        "raw": by_side if len(by_side) == 2 else {},
        "periods": periods if len(periods) == 2 else {},
        "events": events,
        "stats": stats,
        "leaders": leaders,
        "venue": ((info.get("venue") or {}).get("fullName") or "")[:200],
        "attendance": info.get("attendance") or None,
    }


# (close, a hiding) winning margins, by sport.
MARGINS = {"soccer": (1, 3), "basketball": (5, 20), "football": (3, 21), "hockey": (1, 4),
           "baseball": (1, 6), "rugby": (5, 25), "cricket": (5, 60)}


# The codes ESPN does not carry, by series name.
CODE_SPORT = {"AFL": "aussie", "AFLW": "aussie", "NRL": "league", "NRLW": "league", "State of Origin": "league"}
MARGINS.update({"aussie": (6, 40), "league": (6, 24)})


def _sport_of(match) -> str:
    league = LEAGUES.get(match.round.series.name)
    return league[0] if league else CODE_SPORT.get(match.round.series.name, "")


def _list(names):
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def story(match, report=None) -> str:
    """One or two sentences on the game, in our words, from the facts."""
    home, away = match.home_team.name, match.away_team.name
    hs, as_ = match.home_score, match.away_score
    score = f"{hs}–{as_}" if hs is not None else ""
    reds = [e for e in (report.events if report else []) if e["kind"] == "red"]
    ten_men = {e["side"] for e in reds}

    def named(side):
        name = home if side == "home" else away
        return f"ten-man {name}" if side in ten_men else name

    if match.result == "draw":
        line = f"{home} and {away} shared the points, {score}."
    elif match.result in ("home", "away"):
        w, l = ("home", "away") if match.result == "home" else ("away", "home")
        ws, ls = (hs, as_) if w == "home" else (as_, hs)
        margin = (ws or 0) - (ls or 0)
        # What counts as close and what counts as a hiding depends on the game.
        close, big = MARGINS.get(_sport_of(match), (1, 4))
        verb = "edged" if margin <= close else ("thrashed" if margin >= big else "beat")
        lead = named(w)
        line = f"{lead[0].upper()}{lead[1:]} {verb} {named(l)} {ws}–{ls}."
    else:
        return f"{home} v {away}."

    if report:
        goals = [e for e in report.events if e["kind"] in ("goal", "pen", "own") and e["player"]]
        if goals:
            bits = []
            for e in goals[:5]:
                tag = " (pen)" if e["kind"] == "pen" else (" (own goal)" if e["kind"] == "own" else "")
                bits.append(f"{e['player']} {e['minute']}{tag}")
            line += f" Goals: {_list(bits)}."
        for e in reds[:1]:
            line += f" {e['player']} was sent off on {e['minute']}."
        poss = next((s for s in report.stats if s["label"] == "Possession"), None)
        shots = next((s for s in report.stats if s["label"] == "Shots"), None)
        if poss and shots:
            line += f" {home} had {poss['home']} of the ball and {shots['home']} shots to {shots['away']}."
        elif report.leaders:
            mine = [x for x in report.leaders if x["side"] == match.result] or report.leaders
            top = mine[0]
            line += f" {top['player']} led the way with {top['what']}."
    return line


# ---------------------------------------------------------------------------
# FREE PHOTOS, from Wikimedia Commons (client, 9 Nov 2026: "real free images
# of Brighton"). Agency photos of a game (Getty, PA, AP) are never free, so
# these are the clubs' own Commons photos — the fixture itself when somebody
# photographed it, otherwise each club's players, ground and fans — each kept
# with its photographer and licence so the credit can be shown beside it.
# ---------------------------------------------------------------------------
import html as _html
import re as _re
import urllib.parse as _parse

COMMONS = "https://commons.wikimedia.org/w/api.php"
FREE = ("CC0", "Public domain", "CC BY", "CC BY-SA")
SPORT_WORD = {"soccer": "football", "basketball": "basketball", "football": "NFL",
              "hockey": "ice hockey", "baseball": "baseball", "rugby": "rugby", "cricket": "cricket"}
SERIES_WORD = {"AFL": "Australian rules football", "AFLW": "AFL Women's", "NRL": "rugby league",
               "NRLW": "rugby league women"}
NOT_PHOTOS = _re.compile(r"\b(logo|crest|badge|kit|map|route|bus|stamp|chart|diagram|svg|ticket|programme|card)\b", _re.I)
GENERIC = {"fc", "afc", "united", "city", "football", "club", "the", "and", "&", "of", "hove", "sc", "cf",
           "town", "county", "rovers", "wanderers", "athletic", "suns", "kings", "heat"}


def _marks(team_name: str) -> list[str]:
    """The words that name this club in a photo's title: "Brighton" for
    Brighton & Hove Albion, "Clippers" and "LA" for LA Clippers."""
    words = [w for w in _re.split(r"[^\w']+", team_name) if w and w.lower() not in GENERIC and len(w) > 2]
    return words or [team_name]
_photo_cache: dict = {}


def _commons(term: str, limit: int = 12) -> list[dict]:
    key = (term, limit)
    if key in _photo_cache:
        return _photo_cache[key]
    params = {"action": "query", "format": "json", "generator": "search", "gsrsearch": term,
              "gsrnamespace": "6", "gsrlimit": str(limit), "prop": "imageinfo",
              "iiprop": "url|extmetadata|mime", "iiurlwidth": "1280",
              "iiextmetadatafilter": "Categories|LicenseShortName|Artist|DateTimeOriginal"}
    req = urllib.request.Request(f"{COMMONS}?{_parse.urlencode(params)}",
                                 headers={"User-Agent": "GoodTipBot/1.0 (https://goodtip.com.au)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            pages = (json.loads(r.read().decode("utf-8")).get("query") or {}).get("pages") or {}
    except Exception:  # noqa: BLE001
        pages = {}
    out = []
    for p in sorted(pages.values(), key=lambda x: x.get("index", 0)):
        info = (p.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "")
        title = p.get("title", "")
        if info.get("mime") not in ("image/jpeg", "image/png") or not lic.startswith(FREE) or NOT_PHOTOS.search(title):
            continue
        artist = _re.sub(r"<[^>]+>", "", _html.unescape((meta.get("Artist") or {}).get("value", ""))).strip()
        cats = _html.unescape((meta.get("Categories") or {}).get("value", ""))
        year = _re.match(r"(\d{4})", (meta.get("DateTimeOriginal") or {}).get("value", "") or "")
        out.append({"thumb": info.get("thumburl") or info.get("url"), "page": info.get("descriptionurl", ""),
                    "title": title.removeprefix("File:").rsplit(".", 1)[0][:120],
                    "credit": (artist or "Wikimedia Commons")[:120], "license": lic[:40],
                    "_cats": cats, "_year": int(year.group(1)) if year else 0})
    _photo_cache[key] = out
    return out


# GAME ACTION ONLY (client, 11 Nov 2026: "players when they start the game,
# during the game, after the game, a celebration ... not houses painted in
# team colours or history"). Commons files every photo under categories, and
# real game photos sit under a match, a line-up or a club's players; street
# scenes, murals, shops and grounds-from-outside do not.
# A photo counts only when Commons files it as a GAME: a match or a game, a
# goal, a celebration, a line-up, a final or a play-off (client, 12 Nov
# 2026: "I just want to see of the games"). A club's players alone is not
# enough — that is how headshots and portraits got in.
ACTION = _re.compile(r"\b(matches|match|games|game|in action|celebrat\w*|goals?|line-?ups|derby|"
                     r"finals?|play-?offs?|grand final|semi-finals?|quarter-finals?|vs\.?|v\.)\b", _re.I)
NOT_ACTION = _re.compile(r"geograph|hoarding|parking|mural|street|house|shop|pub\b|museum|statue|interiors|"
                         r"exteriors|buildings|wrestling|stamps|maps|logos|kits|programmes|tickets|aerial|"
                         r"under construction|signs|graffiti|trains|buses|portrait|headshot|selfie|cropped|"
                         r"\bfans\b|supporters|training|press conference|signing|airport|parade|award|ceremony|"
                         r"mascot|cheerleader|dancers|wedding|politician|concert|video games|board games|"
                         r"\bmen of\b|people of|in \d{4} \(people\)|living people|\bstand\b|grandstand|panorama|view from|"
                         r"\((rugby league|rugby union|footballer|soccer|basketball|basketball player|american football|australian rules football(er)?|ice hockey)\)", _re.I)


def _is_game_photo(row, marks) -> bool:
    cats, title = row.get("_cats", ""), row["title"]
    text = f"{title}|{cats}".lower()
    if NOT_ACTION.search(text):
        return False
    # The game has to be in what Commons files it under, not just the title.
    if not ACTION.search(cats):
        return False
    return any(m.lower() in text for m in marks)


def find_photos(match, limit: int = 6) -> list[dict]:
    """Up to six free photos of these two clubs PLAYING: the fixture itself
    first, then each club's games in turn, newest first."""
    home, away = match.home_team.name, match.away_team.name
    sport = _sport_of(match)
    word = SERIES_WORD.get(match.round.series.name) or SPORT_WORD.get(sport, "sport")
    picks, seen = [], set()

    def take(rows, n, marks, recent=True):
        # Recent games first: a photo with no date, or from before 2015, only
        # fills a gap a club's recent photos could not.
        rows = sorted((r for r in rows if _is_game_photo(r, marks) and (not recent or r["_year"] >= 2015)),
                      key=lambda r: -r["_year"])
        for r in rows:
            if len(picks) >= limit or n <= 0:
                return
            if r["thumb"] and r["thumb"] not in seen:
                seen.add(r["thumb"]); picks.append(r); n -= 1

    both = _marks(home) + _marks(away)
    take(_commons(f'"{home}" "{away}"', 30), 2, both)
    pools = {}
    for team in (home, away):
        pools[team] = (_commons(f'"{team}" match', 30) + _commons(f'"{team}" players {word}', 30)
                       + _commons(f'"{team}" celebration', 20))
    order = (home, away) if match.result != "away" else (away, home)
    for _ in range(3):
        for team in order:
            take(pools[team], 1, _marks(team))
    if not picks:
        # Nothing recent: an older photo of a real game is still a game.
        for team in order:
            take(pools[team], 2, _marks(team), recent=False)
    return [{k: v for k, v in p.items() if not k.startswith("_")} for p in picks]


# ---------------------------------------------------------------------------
# THE GAME, WRITTEN UP (client, 9 Nov 2026: "discusses details about it like
# its defence, its attack, things that can make us say this is the reason we
# think they lost ... first half, second half performance"). Built from the
# report's facts, in our own words, as sections a reader can scan and a room
# can argue about.
# ---------------------------------------------------------------------------
def _num(raw, side, *names):
    for n in names:
        v = (raw.get(side) or {}).get(n)
        if v not in (None, ""):
            try:
                return float(str(v).split("-")[0])
            except ValueError:
                continue
    return None


def _fmt(x):
    return "" if x is None else (f"{x:.0f}" if float(x).is_integer() else f"{x:.1f}")


def analysis(match, report=None) -> dict:
    """{"headline", "sections": [(title, text)], "question", "text"} for one game."""
    home, away = match.home_team.name, match.away_team.name
    sport = _sport_of(match)
    sections = []
    headline = story(match, None)
    if match.result in ("home", "away"):
        win, lose = ("home", "away") if match.result == "home" else ("away", "home")
    else:
        win = lose = None
    name = {"home": home, "away": away}
    if not report:
        report_events, raw, periods = [], {}, {}
    else:
        report_events, raw, periods = report.events or [], report.raw or {}, report.periods or {}

    # ---- the halves (or quarters)
    if periods.get("home") and periods.get("away"):
        h, a = periods["home"], periods["away"]
        if sport == "soccer" and len(h) >= 2:
            ht = f"{h[0]}–{a[0]}"
            goals1 = [e for e in report_events if e["kind"] in ("goal", "pen", "own") and _minute(e["minute"]) <= 45]
            goals2 = [e for e in report_events if e["kind"] in ("goal", "pen", "own") and _minute(e["minute"]) > 45]

            def by_team(goals):
                parts = []
                for side in ("home", "away"):
                    mine = [e for e in goals if e["side"] == side and e["player"]]
                    if mine:
                        who = _list([f"{e['player']} ({e['minute']}{', pen' if e['kind'] == 'pen' else ''}"
                                     f"{', own goal' if e['kind'] == 'own' else ''})" for e in mine])
                        parts.append(f"{who} for {name[side]}")
                return "; ".join(parts)

            if goals1:
                first = f"It was {ht} at the break: {by_team(goals1)}."
            elif h[0] == a[0] == 0:
                first = "Goalless at the break — neither side found a way through in the first 45."
            else:
                first = f"It was {ht} at the break."
            sections.append(("First half", first))
            if goals2:
                second = f"The second half finished {h[1]}–{a[1]}: {by_team(goals2)}."
            elif h[1] == a[1] == 0:
                second = "Nothing more after half-time — the second half finished 0–0."
            else:
                second = f"The second half finished {h[1]}–{a[1]}."
            late = [e for e in goals2 if _minute(e["minute"]) >= 80]
            if late:
                second += f" The late one from {late[-1]['player']} ({late[-1]['minute']}) is the moment people will remember."
            sections.append(("Second half", second))
        elif len(h) >= 4:
            q = " · ".join(f"Q{i + 1} {x}–{y}" for i, (x, y) in enumerate(zip(h, a)))
            fh, sh = (sum(h[:2]), sum(a[:2])), (sum(h[2:]), sum(a[2:]))
            sections.append(("By quarter", q + "."))
            lead = home if fh[0] > fh[1] else away if fh[1] > fh[0] else None
            sections.append(("First half", f"{fh[0]}–{fh[1]} at half-time" + (f", {lead} in front." if lead else ", all square.")))
            better = home if sh[0] > sh[1] else away if sh[1] > sh[0] else None
            sections.append(("Second half", f"{sh[0]}–{sh[1]} after the break" + (f" — {better} won the second half." if better else ".")))

    # ---- attack and defence (a draw compares home with away)
    if raw and not win:
        win, lose, drawn = "home", "away", True
    else:
        drawn = False
    if raw and win:
        if sport == "soccer":
            ws, ls = _num(raw, win, "totalShots"), _num(raw, lose, "totalShots")
            wt, lt = _num(raw, win, "shotsOnTarget"), _num(raw, lose, "shotsOnTarget")
            wp = _num(raw, win, "possessionPct")
            if ws is not None:
                sections.append(("Attack", f"{name[win]} had {_fmt(ws)} shots ({_fmt(wt)} on target) to {name[lose]}'s {_fmt(ls)} ({_fmt(lt)} on target)"
                                 + (f", with {_fmt(wp)}% of the ball." if wp is not None else ".")))
            saves = _num(raw, lose, "saves")
            tackles = _num(raw, lose, "effectiveTackles", "totalTackles")
            inter = _num(raw, lose, "interceptions")
            fouls = _num(raw, lose, "foulsCommitted")
            bits = []
            if saves is not None:
                bits.append(f"{name[lose]}'s keeper made {_fmt(saves)} save{'s' if saves != 1 else ''}")
            if tackles is not None:
                bits.append(f"{_fmt(tackles)} tackles won")
            if inter is not None:
                bits.append(f"{_fmt(inter)} interceptions")
            if fouls is not None:
                bits.append(f"{_fmt(fouls)} fouls given away")
            if bits:
                sections.append(("Defence", ", ".join(bits) + "."))
        elif sport == "basketball":
            wfg, lfg = _num(raw, win, "fieldGoalPct"), _num(raw, lose, "fieldGoalPct")
            w3, l3 = _num(raw, win, "threePointFieldGoalPct"), _num(raw, lose, "threePointFieldGoalPct")
            if wfg is not None:
                sections.append(("Attack", f"{name[win]} shot {_fmt(wfg)}% from the field" + (f" and {_fmt(w3)}% from three" if w3 is not None else "")
                                 + f"; {name[lose]} {_fmt(lfg)}%" + (f" and {_fmt(l3)}%" if l3 is not None else "") + "."))
            wr, lr = _num(raw, win, "totalRebounds"), _num(raw, lose, "totalRebounds")
            wto, lto = _num(raw, win, "turnovers", "totalTurnovers"), _num(raw, lose, "turnovers", "totalTurnovers")
            bits = []
            if wr is not None:
                bits.append(f"rebounds {_fmt(wr)}–{_fmt(lr)}")
            if wto is not None:
                bits.append(f"turnovers {_fmt(wto)}–{_fmt(lto)}")
            if bits:
                sections.append(("Defence", f"{name[win]} v {name[lose]}: " + ", ".join(bits) + "."))
        if report and report.leaders:
            stars = "; ".join(f"{x['player']} ({name.get(x['side'], '')}) {x['what']}" for x in report.leaders[:2])
            sections.append(("Stand-outs", stars + "."))

    # ---- why the loser lost, and the question for the room
    question = ""
    if drawn:
        win = lose = None
    if win:
        why = None
        reds = [e for e in report_events if e["kind"] == "red" and e["side"] == lose]
        if reds:
            why = f"Down to ten from {reds[0]['minute']} when {reds[0]['player']} was sent off — hard to come back from that."
            question = f"Did the red card cost {name[lose]} this, or were they beaten anyway?"
        elif sport == "soccer" and raw:
            lp, lt = _num(raw, lose, "possessionPct"), _num(raw, lose, "shotsOnTarget")
            if lp is not None and lp >= 55:
                why = f"{name[lose]} had the ball ({_fmt(lp)}%) but not the cutting edge: {_fmt(lt)} on target."
                question = f"Is possession overrated? {name[lose]} had the ball and still lost."
            elif lt is not None and lt <= 2:
                why = f"{name[lose]} barely tested the keeper — {_fmt(lt)} shot{'s' if lt != 1 else ''} on target all game."
                question = f"Where were {name[lose]}'s chances? What needs to change?"
        elif sport == "basketball" and raw:
            lto, wto = _num(raw, lose, "turnovers", "totalTurnovers"), _num(raw, win, "turnovers", "totalTurnovers")
            lr, wr = _num(raw, lose, "totalRebounds"), _num(raw, win, "totalRebounds")
            if lto is not None and wto is not None and lto - wto >= 4:
                why = f"Turnovers: {name[lose]} gave it away {_fmt(lto)} times to {_fmt(wto)}."
            elif lr is not None and wr is not None and wr - lr >= 6:
                why = f"Beaten on the boards, {_fmt(wr)} rebounds to {_fmt(lr)}."
        if not why:
            why = f"{name[win]} took their chances when it mattered; {name[lose]} didn't."
        sections.append((f"Why {name[lose]} lost", why))
        if not question:
            question = f"Who was the difference for {name[win]} — and are {name[lose]} worried?"
    else:
        question = f"Fair result? Who should have won {home} v {away}?"

    if report and report.venue:
        sections.append(("Where", report.venue + (f", {report.attendance:,} there." if report.attendance else ".")))

    story_line = story(match, report)
    text = story_line + "\n\n" + "\n".join(f"{t}: {x}" for t, x in sections) + (f"\n\n{question}" if question else "")
    return {"headline": story_line, "sections": sections, "question": question, "text": text[:2000]}


def _minute(m: str) -> int:
    """15' -> 15, 45'+5' -> 45, 90'+3' -> 90."""
    import re

    found = re.match(r"(\d+)", m or "")
    return int(found.group(1)) if found else 0


# ---------------------------------------------------------------------------
# FINAL WHISTLE: one story per real game, for everyone (11 Nov 2026).
# The library's HistoricalMatch is the game; Game() lets the writer above
# read it exactly as it reads an organisation's Match.
# ---------------------------------------------------------------------------
from types import SimpleNamespace as _NS


class Game:
    def __init__(self, hm):
        self.hm = hm
        self.home_team, self.away_team = hm.home_team, hm.away_team
        self.home_score, self.away_score = hm.home_score, hm.away_score
        self.result = "home" if hm.home_score > hm.away_score else ("away" if hm.away_score > hm.home_score else "draw")
        self.round = _NS(series=hm.series)
        self.external_id = hm.external_id or ""
        self.kickoff_at = hm.kickoff_at
        self.pk = None


def facts_for(game: Game) -> dict:
    """The game's facts from ESPN's summary, or {} for a feed without one."""
    league = LEAGUES.get(game.round.series.name)
    if not league or not game.external_id.startswith("espn:"):
        return {}
    sport, code = league
    try:
        data = _get(f"{BASE}/{sport}/{code}/summary?event={game.external_id.split(':', 1)[1]}")
    except Exception:  # noqa: BLE001
        logger.info("final whistle: no summary for %s", game.external_id)
        return {}
    return parse_summary(data, game, sport)


def write_story(hm, *, fetch: bool = True, refresh_photos: bool = False):
    """Write (or rewrite) one game's Final Whistle story."""
    from datetime import timedelta

    from .models import MatchStory

    game = Game(hm)
    story = MatchStory.objects.filter(game=hm).first() or MatchStory(game=hm, published_at=hm.kickoff_at + timedelta(hours=2, minutes=30))
    if fetch and not story.raw:
        for k, v in facts_for(game).items():
            setattr(story, k, v)
    if fetch and (refresh_photos or not story.photos):
        story.photos = find_photos(game)
    w = analysis(game, story)
    story.headline, story.sections, story.question = w["headline"], [list(x) for x in w["sections"]], w["question"]
    story.save()
    return story


def generate_match_stories(days: int = 3, *, fetch: bool = True, series=None) -> list:
    """Every finished game in the library from the last `days` without a story."""
    from datetime import timedelta

    from django.utils import timezone

    from matchreader.models import HistoricalMatch

    now = timezone.now()
    games = (HistoricalMatch.objects.filter(kickoff_at__gte=now - timedelta(days=days), kickoff_at__lte=now,
                                            story__isnull=True)
             .select_related("series", "home_team", "away_team").order_by("kickoff_at"))
    if series:
        games = games.filter(series__slug__in=series)
    made = []
    for hm in games:
        try:
            made.append(write_story(hm, fetch=fetch))
        except Exception:  # noqa: BLE001 — one bad game must not stop the rest
            logger.exception("final whistle: could not write %s", hm)
    return made
