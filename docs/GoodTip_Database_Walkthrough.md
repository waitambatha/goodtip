# GoodTip — Database Walkthrough

A guided tour of how the platform is structured in the database, from the top of the hierarchy down to individual matches. Each level of the structure maps to one database table. We will move through them in order.

The structure has four levels:

**Sport → Series → Competition → Fixtures**

A Sport contains several Series. A Competition bundles those Series together for one season and is the thing a workplace joins. Fixtures are the individual matches that are tipped on.

---

## Level 1 — Sport

**Table: `catalog_sport`**

This table holds the sport codes — the broadest category in the system. A sport is permanent and does not change from season to season; everything else in the hierarchy hangs off it.

| name | slug |
|------|------|
| Australian Rules | australian-rules |
| Rugby League | rugby-league |
| Netball | netball |

Netball is already present in readiness for Super Netball in 2027. A new sport can be added here as a single row without changing anything else in the system.

---

## Level 2 — Series

**Table: `catalog_series`** *(each row links back to a Sport through the `sport_id` column)*

This table holds the specific competitions that run within each sport — the men's, women's, and representative versions. Each Series belongs to exactly one Sport.

| name | sport | type |
|------|-------|------|
| AFL | Australian Rules | Men's |
| AFLW | Australian Rules | Women's |
| NRL | Rugby League | Men's |
| NRLW | Rugby League | Women's |
| State of Origin | Rugby League | Representative |
| Super League | Rugby League | Men's |
| Super Netball | Netball | Women's |

The women's series and State of Origin are part of the structure, not optional extras. Every series is set to be fully included with no opt-out, which is what allows men's and women's to share a single leaderboard.

---

## Level 3 — Competition

**Table: `catalog_competition`** *(links to a Sport, a Season, and the list of Series it includes)*

This table holds the competitions a workplace actually joins and tips on. A Competition takes a sport's series and bundles them together for one specific season.

| reads as | season | bundles these Series |
|----------|--------|----------------------|
| AFL (2026) | 2026 | AFL + AFLW |
| NRL (2026) | 2026 | NRL + NRLW + State of Origin |
| Super League (2027) | 2027 | Super League |
| Super Netball (2027) | 2027 | Super Netball |

In the raw table the `name` column stores only the brand — for example `"NRL"` — and the year is held separately in the `season` column. The two are shown together as "NRL (2026)". Keeping the year as its own column means next season simply adds an "NRL (2027)" row that reuses the same NRL and NRLW series, with no duplication.

This is the level where the structure delivers its core promise: joining **NRL (2026)** means tipping the men's NRL, the women's NRLW and State of Origin together, on one leaderboard.

---

## How a league joins a Competition

**Table: `orgs_organisation_competitions`**

This is a link table. It does not define anything new — it only records which league has signed up to which competition. Each row is a single pairing of a league and a competition.

| league | competition joined |
|--------|--------------------|
| Office Footy Crew | AFL |
| Office Footy Crew | NRL |
| Test Friends Comp | AFL |

Because the relationship runs both ways — one league can join several competitions, and one competition can have many leagues — the sign-ups live in their own table rather than as a column on either side. The competitions themselves remain defined once in `catalog_competition`; removing a row here only un-enrols a league and leaves the competition intact.

---

## Level 4 — Fixtures

**Tables: `tipping_round` and `tipping_match`**

Fixtures are the actual matches that get tipped. They are stored across two tables:

- **`tipping_round`** holds the round header — the round number, which Series it belongs to, which Competition it sits under, the match type, and the lock-out time.
- **`tipping_match`** holds the individual games — home team, away team, kick-off time, venue, and final result.

Every match points to a round, and every round points to both a Series and a Competition, so an NRLW round and an NRL round both roll up into the single NRL (2026) leaderboard.

| Competition | Series | Round | Match | Venue |
|-------------|--------|-------|-------|-------|
| AFL (2026) | AFL | R21 | St Kilda v Sydney Swans | Marvel Stadium |
| AFL (2026) | AFL | R21 | Hawthorn v North Melbourne | UTAS Stadium |
| NRL (2026) | NRL | R22 | Brisbane Broncos v Newcastle Knights | Suncorp Stadium |
| NRL (2026) | NRLW | R5 | Cronulla Sharks v Wests Tigers | Geohex Park, Wagga Wagga |

The fixtures currently loaded are the real August 2026 draw taken from the official fixtures document: **16 rounds and 120 matches** across AFL, AFLW, NRL and NRLW, with correct teams, venues and kick-off times.

---

## Scoring

**Where it lives: `tipping_round.stage` sets the match type; `tipping_tip.points_awarded` stores each tip's weighted score.**

Not every correct tip is worth the same. The value depends on the match type of the round.

| Match type | Points per correct tip |
|------------|------------------------|
| Regular round | 1 |
| Finals | 2 |
| State of Origin | 4 |

The leaderboard adds up these weighted points rather than simply counting correct tips, so finals and Origin carry more weight in deciding the ladder.

---

## Summary — level to table

| Level | Table | What it holds |
|-------|-------|---------------|
| Sport | `catalog_sport` | Rugby League, Australian Rules, Netball |
| Series | `catalog_series` | NRL, NRLW, AFL, AFLW, State of Origin, Super League, Super Netball |
| Competition | `catalog_competition` | NRL (2026), AFL (2026), Super League (2027), Super Netball (2027) |
| Sign-ups | `orgs_organisation_competitions` | which league joined which competition |
| Fixtures | `tipping_round` + `tipping_match` | the rounds and the 120 real matches |

The structure is four clean levels: the Sport, the Series within it, the Competition a workplace joins for a season, and the Fixtures tipped on. Women's competitions and State of Origin are built into the structure rather than added on, and the same structure already holds 2027's Super League and Super Netball. The 2026 fixtures are loaded and live in the database.
