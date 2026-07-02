# GoodTip — Remaining pages to redesign (brief for web Claude)

These are the logged-in pages **not yet redesigned**. Auth + the tipping loop
(dashboard, tip round, my tips, leaderboard) are already done in the new look;
everything below still uses the old style and needs the new one.

## Checklist (18 pages)
- [ ] 1. Profile
- [ ] 2. Create a league
- [ ] 3. League created
- [ ] 4. Invite members
- [ ] 5. Members (manage)
- [ ] 6. Charity vote
- [ ] 7. Join prompt (logged-out)
- [ ] 8. Invalid invite
- [ ] 9. Billing — Plans
- [ ] 10. Billing — Checkout success
- [ ] 11. Billing — Donation pledge
- [ ] 12. Billing — Top-up ("Add to the cause")
- [ ] 13. Billing — Season summary
- [ ] 14. Billing — Receipt
- [ ] 15. Admin — Overview
- [ ] 16. Admin — Orgs list
- [ ] 17. Admin — Org rounds
- [ ] 18. Admin — Round matches
- [ ] 19. Admin — Org members
- [ ] 20. Admin — Sync

---

## Design + integration rules (apply to every page)
- **Match the new app look** already shipped: deep-forest / electric-lime / cream, Archivo fonts. Reuse the app shell + components: `.app`, `.card`, `.stat-card`, `.abtn`/`.abtn-primary`/`.abtn-ghost`/`.abtn-dark`, `.atable`, `.fld` form fields, `.dd` custom dropdowns, `.flash` (success/error/info), `.pill-role`, `.pbar`. Admin pages can be denser/tool-like in the same palette.
- Deliver static HTML/CSS. Keep every **form field `name`** exactly as listed (I bind Django by name). I'll re-wire `{% url %}`, `{% csrf_token %}`, loops, `{{ vars }}`.
- The top nav / charity strip / loaders come from the shared shell — you don't need to build them; just design the page body.
- For empty / error / loading states, include them where noted.

---

# A. Account

## 1. Profile — `/profile/`
- **About:** manage your account and see the leagues you're in.
- **Shows:** your display name + email; a list of your memberships (league name + your role).
- **Does — two forms:**
  - Update profile → field `display_name`.
  - Change password → fields `old_password`, `new_password1`, `new_password2`.
  - Log out button.

---

# B. Onboarding & group (org)

## 2. Create a league — `/leagues/new/`
- **About:** stand up a new tipping comp (creator becomes Owner + Manager + Captain).
- **Shows:** one form.
- **Fields:** `name`; `competitions` (checkbox group, ≥1 — AFL/AFLW/NRL/NRLW); `season` (select); `team_size` (number, optional); `finals_only` (checkbox); `charity_method` (radio: **"I'll choose"** vs **"Let the group vote"**); if choose → `charity` (select of approved charities) **or** `new_charity_name` (text) + `new_charity_url` (url); if vote → `vote_charities` (checkbox group, pick ≥2).
- **UX:** show/hide the pick-vs-vote sub-fields based on `charity_method`.

## 3. League created — `/leagues/<id>/created/`
- **About:** success screen right after creating a league.
- **Shows:** success state; the **invite link** (copy button); if a vote was started, a prompt to share the charity vote.
- **Does:** CTAs to Invite members / go to Dashboard.

## 4. Invite members — `/leagues/<id>/invite/`
- **About:** grow the group.
- **Shows:** the shareable **invite link** (copy button); count of people invited; list of invitees you brought in (name + joined date).

## 5. Members (manage) — `/leagues/<id>/members/`
- **About:** manage who's in the league and their roles.
- **Shows:** members table — name, role label(s) (Owner / Manager / Captain / Participant), joined date.
- **Does:** change a member's role → `action=set_role`, `member_id`, `role`; (owner only) nominate a Team Manager by email → `action=nominate_manager`, `email`.

## 6. Charity vote — `/leagues/<id>/charity-vote/`
- **About:** the group votes for the season's charity. **Blind vote** — tallies hidden while open, revealed after it closes.
- **Shows:** the candidate charities (selectable cards); which one *you* picked; total ballots cast. Once closed: results with the winner highlighted.
- **Does:** cast/change vote → POST `option=<option_id>` to `.../charity-vote/cast/`; (manager) **close vote** → POST `.../charity-vote/close/`.
- **Empty state:** "no vote running."

## 7. Join prompt — `/join/<id>/<token>/` (logged-out)
- **About:** landing page from an invite link when not logged in.
- **Shows:** "You've been invited to **<group>**" + Sign up / Log in buttons (they auto-join after auth).

## 8. Invalid invite — bad/expired token
- **About:** error page. "This invite link is invalid or expired." + link home.

---

# C. Billing (owner-only unless noted)

## 9. Plans — `/billing/<id>/plans/`
- **About:** choose the platform plan for the league.
- **Shows:** 5 tier cards — **Starter $99 (≤20)**, **Growth $199 (≤50, "Popular")**, **Pro $499 (≤200)**, **Enterprise $999 (≤500)**, **Enterprise+ $1,999 (500+)** — each with an audience line + feature bullets; current plan marked; a notice if Stripe isn't configured ("payments not switched on — choice is saved").
- **Does:** choose a tier → POST `tier=<key>` to checkout.

## 10. Checkout success — `/billing/<id>/success/`
- **About:** reassurance after payment. Plan name, "you're set for the season," back to dashboard. No form.

## 11. Donation pledge — `/billing/<id>/pledge/`
- **About:** the league commits its season donation.
- **Fields:** `pledged_amount` (AUD); `payment_schedule` (radio — upfront / instalments / season close); `matching_enabled` (checkbox — "match top-ups $-for-$"); `matching_cap` (AUD, required if matching on). Show a **suggested minimum** hint.

## 12. Top-up / "Add to the cause" — `/billing/<id>/top-up/`
- **About:** optional personal donation on top of the league's pledge. **Any member** (not just owner). Shown as a nudge after joining a league that has a pledge.
- **Shows:** live donation summary — charity, base pledge, top-ups so far, matched amount, raised / goal with % progress, matching cap remaining.
- **Field:** `amount` (AUD). On submit → records/checkout → leaderboard with a "your $X became $2X thanks to your organisation" thank-you.

## 13. Season summary — `/billing/<id>/season-summary/`
- **About:** end-of-season wrap. **Managers.**
- **Shows:** the winner + top-3 **podium**; final donation summary (total raised + breakdown); plan; (owner + Pro plan) a **Download ESG report (PDF)** link.
- **Does (owner):** **Close the season** → POST to settle the donation pool; then shows the disbursement record.

## 14. Receipt — `/billing/<id>/receipt/`
- **About:** a clean, **printable** platform service-fee receipt (GoodTip Pty Ltd) — plan, amount, season, org. No form.

---

# D. Admin / "Manage" (staff-only back office)

Own denser layout, same palette. Tool-like.

## 15. Overview — `/manage/`
- **Shows:** stat tiles (total orgs, rounds, matches, tips) + list of 5 most recent orgs.

## 16. Orgs list — `/manage/orgs/`
- **Shows:** table of all orgs (newest first) with links into each org's rounds/members.
- **Create-org form:** `season`, `charity_name`, `charity_url`, `name`, `sport` (AFL / NRL / BOTH).

## 17. Org rounds — `/manage/org/<id>/rounds/`
- **Shows:** rounds list (number, series, stage, lockout, status) + links to each round's matches.
- **Forms:** create round → `action=create`, `series`, `round_number`, `stage`, `lockout_at`, `status`; update status → `action=status`, `round_id`, `status`.

## 18. Round matches — `/manage/org/<id>/round/<round_id>/matches/`
- **Shows:** matches in a round.
- **Forms:** add match → `action=create`, `home_team`, `away_team`, `kickoff_at`, `venue`; enter result → `action=result`, `match_id`, `home_score`, `away_score` (grades tips on save).

## 19. Org members (admin) — `/manage/org/<id>/members/`
- **Shows:** members table + the org's join link.
- **Forms:** remove → `action=remove`, `member_id`; promote → `action=promote`, `member_id` (toggles Participant ↔ Manager).

## 20. Sync — `/manage/sync/`
- **About:** pull fixtures/results from the data provider.
- **Form:** `competition`, `round_number`, `org_id`, `kind` (fixtures / results).

---

## Suggested order
1. **Profile** (small, high-traffic).
2. **Create league → League created → Invite → Members → Charity vote** (the onboarding flow — do together, they share style).
3. **Billing** (Plans, Pledge, Top-up, Season summary, Receipt, Success).
4. **Admin/Manage** (lowest polish priority — internal tools).

Ask web Claude for each: HTML + CSS in the app design, mobile + tablet + desktop, and the empty/error states noted above.
