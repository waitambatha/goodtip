"""More sports, all switched off (client, 27 Oct 2026: "add the top 10
sports... the admin should be able to say turn off this and turn on this").

AFL, AFLW, the NRL, NRLW and State of Origin stay on — the defaults. Super
League and Super Netball, seeded in 2026 as roadmap entries with no feed,
are switched off with everything new; the organisations already signed up to
them keep their rounds (off hides a competition from what members choose
from, it does not touch what exists).

Each competition names where its fixtures will come from, so nobody switches
one on believing games will arrive when nothing fetches them yet.
"""
from django.db import migrations

FOOTBALL_DATA = "football-data.org (free key)"
ESPN = "ESPN public feeds"

# sport name, slug, emoji, format, draw tips, order, blurb, [series]
# series: (name, slug, category, womens, feed, region)
SPORTS = [
    ("Football", "football", "⚽", "match", True, 10,
     "The world game. Home, away — or a draw.",
     [("English Premier League", "epl", "mens", False, FOOTBALL_DATA, "England"),
      ("La Liga", "la-liga", "mens", False, FOOTBALL_DATA, "Spain"),
      ("Serie A", "serie-a", "mens", False, FOOTBALL_DATA, "Italy"),
      ("Bundesliga", "bundesliga", "mens", False, FOOTBALL_DATA, "Germany"),
      ("UEFA Champions League", "ucl", "mens", False, FOOTBALL_DATA, "Europe"),
      ("A-League Men", "a-league-men", "mens", False, ESPN, "Australia"),
      ("A-League Women", "a-league-women", "womens", True, ESPN, "Australia"),
      ("Women's Super League", "wsl", "womens", True, ESPN, "England")]),
    ("Basketball", "basketball", "\U0001f3c0", "match", False, 20,
     "Four quarters, no draws.",
     [("NBA", "nba", "mens", False, ESPN, "USA"),
      ("WNBA", "wnba", "womens", True, ESPN, "USA"),
      ("NBL", "nbl", "mens", False, ESPN, "Australia"),
      ("WNBL", "wnbl", "womens", True, ESPN, "Australia")]),
    ("Cricket", "cricket", "\U0001f3cf", "match", True, 30,
     "Twenty20 to five-day Tests — where a draw is a real result.",
     [("Big Bash League", "bbl", "mens", False, ESPN, "Australia"),
      ("Women's Big Bash League", "wbbl", "womens", True, ESPN, "Australia"),
      ("Test Cricket", "test-cricket", "representative", False, ESPN, "International")]),
    ("Rugby Union", "rugby-union", "\U0001f3c9", "match", False, 40,
     "Super Rugby and the Tests.",
     [("Super Rugby Pacific", "super-rugby", "mens", False, ESPN, "Pacific"),
      ("Super Rugby Women's", "super-rugby-w", "womens", True, ESPN, "Pacific"),
      ("Six Nations", "six-nations", "representative", False, ESPN, "Europe")]),
    ("Tennis", "tennis", "\U0001f3be", "match", False, 50,
     "The four Grand Slams, match by match.",
     [("Grand Slams — Men", "slams-men", "mens", False, ESPN, "International"),
      ("Grand Slams — Women", "slams-women", "womens", True, ESPN, "International")]),
    ("Golf", "golf", "⛳", "event", False, 60,
     "The majors. Pick the winner from the field.",
     [("Men's Majors", "golf-majors-men", "mens", False, ESPN, "International"),
      ("Women's Majors", "golf-majors-women", "womens", True, ESPN, "International")]),
    ("Motorsport", "motorsport", "\U0001f3ce️", "event", False, 70,
     "Race weekends. Pick who takes the chequered flag.",
     [("Formula 1", "f1", "mens", False, "Jolpica F1 API", "International"),
      ("Supercars", "supercars", "mens", False, ESPN, "Australia")]),
    ("American Football", "american-football", "\U0001f3c8", "match", False, 80,
     "The NFL, Sunday by Sunday.",
     [("NFL", "nfl", "mens", False, ESPN, "USA")]),
    ("Ice Hockey", "ice-hockey", "\U0001f3d2", "match", False, 90,
     "Overtime and shoot-outs — always a winner.",
     [("NHL", "nhl", "mens", False, ESPN, "North America"),
      ("PWHL", "pwhl", "womens", True, ESPN, "North America")]),
    ("Baseball", "baseball", "⚾", "match", False, 95,
     "Nine innings, every day of summer.",
     [("MLB", "mlb", "mens", False, ESPN, "USA")]),
]

DEFAULT_ON = {"AFL", "AFLW", "NRL", "NRLW", "State of Origin"}


def seed(apps, schema_editor):
    db = schema_editor.connection.alias
    Sport = apps.get_model("catalog", "Sport")
    Series = apps.get_model("catalog", "Series")

    # The codes already here: the defaults on, the roadmap entries off.
    Sport.objects.using(db).filter(slug="australian-rules").update(icon="\U0001f3c9", sort_order=1, is_active=True,
                                                                  blurb="The AFL and AFLW.")
    Sport.objects.using(db).filter(slug="rugby-league").update(icon="\U0001f3c9", sort_order=2, is_active=True,
                                                              blurb="The NRL, NRLW and State of Origin.")
    Sport.objects.using(db).filter(slug="netball").update(icon="\U0001f3d0", sort_order=85, is_active=False,
                                                         blurb="Super Netball.")
    for s in Series.objects.using(db).all():
        s.is_active = s.name in DEFAULT_ON
        if s.name in ("AFL", "AFLW"):
            s.feed_source = "afl.com.au (our scraper)"
        elif s.name in ("NRL", "NRLW", "State of Origin"):
            s.feed_source = "nrl.com (our scraper)"
        s.save(using=db)

    for name, slug, icon, fmt, draws, order, blurb, series in SPORTS:
        sport, _ = Sport.objects.using(db).get_or_create(
            slug=slug, defaults={"name": name})
        sport.name, sport.icon, sport.format, sport.draw_tips = name, icon, fmt, draws
        sport.sort_order, sport.blurb, sport.is_active = order, blurb, False
        sport.save(using=db)
        for i, (sname, sslug, cat, womens, feed, region) in enumerate(series):
            row, _ = Series.objects.using(db).get_or_create(
                slug=sslug, defaults={"name": sname, "sport": sport})
            row.name, row.sport, row.category, row.is_womens = sname, sport, cat, womens
            row.feed_source, row.region, row.sort_order, row.is_active = feed, region, (i + 1) * 10, False
            row.save(using=db)


def unseed(apps, schema_editor):
    db = schema_editor.connection.alias
    Sport = apps.get_model("catalog", "Sport")
    Sport.objects.using(db).filter(slug__in=[s[1] for s in SPORTS], competitions__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("catalog", "0023_sports_switches")]
    operations = [migrations.RunPython(seed, unseed)]
