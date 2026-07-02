# GoodTip — Platform Update: Hierarchy, Scoring & 2026 Fixtures

**Prepared for:** Team / stakeholder meeting
**Date:** 25 June 2026
**Status:** ✅ Complete — all changes live in the database, full test suite passing (18/18)

---

## 1. Executive summary

We have aligned the GoodTip platform with the three planning documents shared by Ambrose:

1. **Ambrose_Brief_Hierarchy.pptx** — the sport hierarchy and scoring model
2. **GoodTip_Fixtures_Calendar_2026_2027.pptx** — launch timeline and year-round strategy
3. **GoodTip_Fixtures_Reference_2026.docx** — the real August 2026 fixtures

In one sentence: **the platform now understands men's + women's + State of Origin as one combined competition, scores finals and Origin at a premium, and is loaded with the real 2026 fixtures.**

This delivers the brief's central promise — *year-round engagement with no dead season* — at the data level. Picking "NRL" or "AFL" now means the full code: men's, women's, finals, and Origin, all on one leaderboard.

---

## 2. What changed — at a glance

| Area | Before | After |
|------|--------|-------|
| **Hierarchy** | Flat: Sport → Competition | Sport (umbrella) → **Series** → Fixtures, with State of Origin |
| **Women's footy** | AFLW / NRLW were separate competitions | Folded into the men's umbrella — **one leaderboard, no opt-out** |
| **Scoring** | Every correct tip = 1 point | **Regular = 1, Finals = 2, State of Origin = 4** |
| **Fixtures** | Test data only | **Real Aug 2026 fixtures** — 16 rounds, 120 matches |
| **Launch date (records)** | "Sept 2026" | Corrected: soft launch **9 Aug**, platform launch **28 Aug 2026** |

---

## 3. The hierarchy (Ambrose brief, slide 7)

The brief defines four levels: **Sport → Series → Competition → Fixtures**. We implemented this so that one annual sign-up covers a whole code, all year:

```
SPORT (the umbrella a league joins — "AFL", "NRL")
  └── SERIES (the specific competitions that make up that code)
        ├── AFL        (Men's)
        ├── AFLW       (Women's)        ← same leaderboard, no toggle
        ├── NRL        (Men's)
        ├── NRLW       (Women's)        ← same leaderboard, no toggle
        └── State of Origin (Representative)
              └── FIXTURES (individual matches / rounds)
```

**Key principle — "no opt-out":** Women's footy and Origin are *structural*, not optional add-ons. When a workplace picks NRL, they get NRL + NRLW + Origin automatically, scored together. This is what makes the platform run continuously instead of going quiet when the men's season ends.

---

## 4. How scoring works now (Ambrose brief, slide 6)

| Match type | Points per correct tip | Why |
|------------|------------------------|-----|
| Regular round | **1** | Baseline — every match counts equally |
| Finals | **2** | High stakes, season climax — moves the board |
| State of Origin | **4** | Prestige event, maximum value |

The leaderboard now **sums weighted points** instead of just counting correct tips. A correct Origin tip is worth four regular-round tips — so the run home and the big events genuinely decide the ladder. League managers can tag any round as Regular / Finals / Origin when they set it up.

---

## 5. The 2026 fixtures (Fixtures Reference doc)

We transcribed and loaded the real August 2026 fixtures — the window when the platform soft-launches:

| Competition | Rounds loaded | Matches |
|-------------|---------------|---------|
| AFL (Men's) | Rounds 21–24 | 34 |
| NRL (Men's) | Rounds 22–27 | 45 |
| NRLW (Women's) | Rounds 5–7 | 16 |
| AFLW (Women's) | Rounds 1–3 | 25 |
| **Total** | **16 rounds** | **120 matches** |

Every match has the correct teams, venue, and AEST kickoff time. Loading is **repeatable and safe** (re-running makes no duplicates), so we can refresh as the season progresses.

> **Not yet loaded (intentionally):** Finals matchups (TBD until ladders lock) and State of Origin fixtures (the 2026 series finished before the August launch). The scoring for both is already built and tested — we just add the rounds once the matchups are known.

---

## 6. Current database snapshot

Live development database as of this update:

| Entity | Count |
|--------|-------|
| Users | 16 |
| Leagues (organisations) | 6 |
| League memberships | 24 |
| Sports (umbrellas) | 2 — AFL, NRL |
| Series | 5 — AFL, AFLW, NRL, NRLW, State of Origin |
| Approved charities | 7 |
| Teams | 65 (AFL 18, AFLW 18, NRL 17, NRLW 12) |
| Rounds | 21 |
| Matches | 147 |
| Tips | 193 |

A dedicated **"GoodTip 2026 Reference"** league holds the full set of real August fixtures for demos and testing.

---

## 7. The year-round vision this enables

From the calendar deck — the structural advantage no competitor offers:

```
2026   Aug → AFLW launch + NRL/AFL closing rounds
       Sep → AFL/NRL/NRLW finals (4 competitions at once)
       Oct → Grand Finals (NRL/NRLW 4 Oct, AFL 10 Oct)
       Nov → AFLW finals & Grand Final (28 Nov)
2027   Feb → Super League joins
       May → Super Netball joins  →  zero dead months
```

The new hierarchy is built so **Super League (Feb 2027)** and **Super Netball (May 2027)** drop in as new Sports without re-engineering anything — exactly what the brief's "five questions" checklist asked for.

---

## 8. Key dates (corrected)

| Date | Milestone |
|------|-----------|
| 9 Aug 2026 | AFLW opener — soft launch awareness & testing begins |
| 28 Aug 2026 | Platform launch (AFL Wildcard Round — peak acquisition) |
| 4 Oct 2026 | NRL + NRLW Grand Finals (double-header) |
| 10 Oct 2026 | AFL Grand Final |
| 28 Nov 2026 | AFLW Grand Final |
| 31 Dec 2026 | Founding Partner window closes |
| **1 Jan 2027** | **First annual charges begin** |
| Feb 2027 | Super League season |
| May 2027 | Super Netball season |

---

## 9. Quality & confidence

- ✅ **18 of 18 automated tests pass** (including 4 new tests that lock in the 1 / 2 / 4 scoring).
- ✅ All database migrations applied cleanly to the live database, **with existing data preserved** — no leagues, tips, or members were lost in the upgrade.
- ✅ System check reports no issues.
- ✅ Fixture loading verified end-to-end: 120 matches, 0 unresolved teams.

---

## 10. Recommended next steps (for discussion)

1. **Annual billing** — the docs confirm annual charges from 1 Jan 2027. Billing is currently per-season; we should align it to the annual commitment model. *(Separate workstream — not started.)*
2. **Finals & Origin rounds** — add these once 2026 matchups confirm; scoring is ready.
3. **Live data sync** — wire the NRL fixtures feed (AFL already syncs via Squiggle) so rounds update automatically.
4. **2027 sports** — schedule Super League and Super Netball onboarding ahead of their Feb/May 2027 starts.

---

*Document generated from the current codebase and live database. Figures reflect the development environment.*
