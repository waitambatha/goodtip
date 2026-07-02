# GoodTip — Logged-in App Pages (design brief for web Claude)

These are the **authenticated / functional** pages of the product (everything *behind* login) —
separate from the public marketing pages already redesigned (home, how-it-works, pricing, wall,
the good list). Web Claude should redesign the **look** of each page while keeping the **data,
fields, and actions** listed here, so the result drops straight back into the Django app.

---

## 0. Design direction (make it match the new public site)

Reuse the public marketing design language so the whole product feels like one thing:

- **Palette:** deep forest `#0F2E22`, darker panel `#0B2319`, electric lime `#C8F135`,
  cream `#E8E4D5` / `#F2EFE6`, sage grey `#6B7A6E`.
- **Fonts:** `Archivo Black` (headlines), `Archivo` (body), `Barlow Condensed` (labels/wordmark),
  `Barlow Semi-Condensed` (numbers/scores).
- **Feel:** angular (2px radius on structural elements, softer 12–16px on cards), tabular numerals
  for all stats/points/money, lime as the single accent for CTAs and “live” states.
- The app is **utility-first** (dashboards, forms, tables) — cleaner and denser than the marketing
  pages, but same colours/type. Think “the marketing site’s calmer, working cousin.”

### The app shell (wraps every page below)
Every logged-in page sits inside a shared frame — design this once:
1. **Persistent charity strip** (thin bar, very top): heart icon + `RAISED FOR <CHARITY> · $X / GOAL $Y`
   with a slim progress bar; or `RAISING FOR <CHARITY> · no pledge set yet` when no pledge. Right side
   (desktop): `<season> · <sport>`.
2. **Top nav:** `GOODTIP` wordmark + current org name · links: **Dashboard, My Tips, Leaderboard**,
   **Admin** (staff only) · right: user avatar (initial) → Profile, log-out icon, mobile hamburger.
3. **Mobile sheet:** slide-in menu mirroring the nav links + Profile + Log out.
4. Flash messages: success / error / info banners at top of content.

---

## Integration rules (so I can plug the designs back in) — please keep

- Keep every **form field `name`** exactly as listed (Django binds by name). Labels/layout/markup can change freely.
- Keep `{% csrf_token %}` in every form and the form `method`/`action` target.
- Keep the **htmx** attributes noted per page (auto-save tips, live countdown, round filters) — these power the interactivity.
- Keep the listed **context variables** (e.g. `cards`, `org`, `ranked`) — they’re what the template renders.
- Static HTML/CSS is fine to hand back; I’ll re-wire Django template tags (`{% url %}`, loops, `{{ vars }}`).

---

# A. Auth

### A1. Sign up — `/signup/`
- **Who:** logged-out visitor (often arriving from an invite link).
- **Shows:** brand, headline, link to log in. If they came from an invite, a note that they’ll join that group after signup.
- **Form fields:** `display_name` (text), `email` (email), `password1` (password, live strength meter — has `data-pw-strength`), `password2` (password, live “match” check — has `data-pw-match="id_password1"`).
- **Action:** creates account, logs in, then → dashboard (or optional top-up prompt if joining a group that has a pledge).

### A2. Log in — `/login/`
- **Who:** logged-out visitor.
- **Shows:** brand, headline, “forgot password?” link, link to sign up.
- **Form fields:** `email`, `password`.
- **Action:** authenticates → dashboard (or `?next=` target, or invite join).

### A3. Password reset request — `/password-reset/`
- **Shows:** short explainer. **Field:** `email`. **Action:** emails a reset link → “check your inbox” page.

### A4. Reset link sent — `/password-reset/done/`
- Confirmation only: “If that email exists, we’ve sent a link.” No form.

### A5. Set new password — `/password-reset/<uid>/<token>/`
- **Fields:** `new_password1`, `new_password2` (with validation errors). **Action:** → success page.

### A6. Password reset complete — `/password-reset/complete/`
- Success message + button to log in. No form.

---

# B. Onboarding & Group (Org) management

### B1. Create a league — `/leagues/new/`
- **Who:** any logged-in user (they become Owner + Manager + Captain).
- **Purpose:** stand up a new tipping comp.
- **Form fields:**
  - `name` (text) — league name.
  - `competitions` (checkbox group) — one or more competitions (AFL/AFLW/NRL/NRLW etc.).
  - `season` (select).
  - `team_size` (number, optional) — expected group size.
  - `finals_only` (checkbox) — skip regular season.
  - `charity_method` (radio): **“I’ll choose the charity”** vs **“Let the group vote.”**
  - If *choose*: `charity` (select of approved charities) **or** `new_charity_name` (text) + `new_charity_url` (url) to suggest a new one.
  - If *vote*: `vote_charities` (checkbox group, pick ≥2) — the ballot options.
- **UX note:** the charity block should show/hide the pick-vs-vote sub-fields based on `charity_method`.
- **Action:** creates the league → “created” page.

### B2. League created — `/leagues/<id>/created/`
- **Who:** the Owner/Manager, right after creation.
- **Shows:** success state, the **invite link** (copyable), and — if a vote was started — a prompt to share the charity vote. CTAs: invite members, go to dashboard.

### B3. Invite members — `/leagues/<id>/invite/`
- **Who:** managers.
- **Shows:** the shareable **invite link** (copy button), count of people invited, and a list of invitees you’ve brought in (name + joined date).

### B4. Members (manager view) — `/leagues/<id>/members/`
- **Who:** managers/owner.
- **Shows:** table of members — name, role label(s) (Owner / Manager / Captain / Participant), joined date.
- **Actions (forms):**
  - Change a member’s role — POST `action=set_role`, `member_id`, `role` (from role choices).
  - (Owner only) Nominate a Team Manager by email — POST `action=nominate_manager`, `email`.

### B5. Charity vote — `/leagues/<id>/charity-vote/`
- **Who:** any member.
- **Shows:** the question + list of candidate charities (radio-style cards); which one *you* picked; total ballots cast. It’s a **blind vote** — tallies are hidden while open and only revealed once closed (winner highlighted).
- **Actions:**
  - Cast/change vote — POST `option=<option_id>` to `.../charity-vote/cast/`.
  - (Manager only) **Close the vote** — POST to `.../charity-vote/close/` → announces the winner.
- **Empty state:** “no vote running” message when there’s no active vote.

### B6. Join prompt — `/join/<id>/<token>/` (logged-out)
- **Shows:** “You’ve been invited to **<group>**” + Sign up / Log in buttons. After auth they auto-join.

### B7. Invalid invite — join with bad/expired token
- Simple error page: “This invite link is invalid or expired.” + link home.

---

# C. Core tipping experience

### C1. Dashboard — `/dashboard/`  *(the home base after login)*
- **Who:** any member. Renders a `cards` list (one per league); the **first/primary league** is expanded, others are compact.
- **Primary league shows:**
  - Greeting: “Welcome back, **<NAME>**” + role pills.
  - **3 stat cards:** Season points · Your rank (of all tippers) · Tips submitted (X of Y this round).
  - **Charity-vote banner** (if a vote is open): “Vote now” / “Review.”
  - **Active round block:** round number, series · league name, a **live countdown to lockout** (auto-refreshes every 60s via htmx — keep `hx-get` to `accounts:dashboard_countdown`), tips-submitted progress bar, and a primary CTA that changes by state: *Finish your tips* / *All tips in — review* / *Review tips* (when locked).
  - **Owner “League setup” zone (owner only):** two cards —
    - **Plan:** if active → tier name + seats used + Receipt/Manage buttons; if none → “Choose a plan (Action needed).”
    - **Donation pledge:** if set → “$X raised for <charity>” with base/top-ups/matched breakdown + progress bar + Edit; if none → “Set your donation pledge (Action needed).”
  - Header quick-actions (manager): Members, Invite, Season summary, Leaderboard.
- **Other comps:** compact cards — name, sport · season · charity, rank, and Tips / Board / Invite buttons.
- **Empty state (no leagues):** “You’re not in a comp yet” → Start a league / ask for a join link.

### C2. Tip a round — `/org/<id>/tip/<round_id>/`
- **Who:** members.
- **Shows:** the round’s matches (home vs away, kickoff time, venue). For each match, a pick control between the two teams; your existing pick is pre-selected. A **locked** banner if lockout has passed (read-only).
- **Key interaction (htmx auto-save):** choosing a team **saves instantly** — each pick POSTs `selection` to `tipping:tip_save` for that match and swaps in a small “saved ✓” confirmation. No page submit button; no reload. Please preserve the per-match `hx-post` + swap target pattern.
- **This is the most important interactive screen** — make the two-team pick feel fast, tactile, and obviously “saved.”

### C3. My tips — `/org/<id>/tips/`
- **Who:** members.
- **Shows:** a round selector (dropdown of rounds), your season points, and for the selected round a list of matches with **your pick**, the **result**, and whether you got it right (correct/incorrect/pending).
- **Interaction:** changing the round filter swaps just the list via htmx (returns `partials/my_tips_round.html`). Keep the round `<select>` + htmx trigger.

### C4. Leaderboard — `/org/<id>/leaderboard/`
- **Who:** members.
- **Shows:** ranked table of everyone in the league — rank (ties share a rank), name, points, tips correct / total. **Your own row is highlighted.** A round filter (All rounds / specific round). A podium for the top 3 is a nice touch.
- **Interaction:** round filter swaps the table via htmx (returns `partials/leaderboard_table.html`). Keep the filter control + htmx.
- **Context:** `ranked` (list of `{rank, user, points, tips_correct, tips_total}`), `me`, `rounds`, `selected_round_id`.

### C5. Profile — `/profile/`
- **Who:** any user.
- **Shows:** your details + the leagues you belong to (name + role).
- **Two forms:**
  - Update profile — field `display_name` (POST includes `display_name`).
  - Change password — fields `old_password`, `new_password1`, `new_password2` (POST includes `old_password`).
- Also a log-out action.

---

# D. Billing (Owner-only unless noted)

### D1. Plans — `/billing/<id>/plans/`
- **Who:** league owner.
- **Shows:** 5 tier cards — **Starter $99 (≤20)**, **Growth $199 (≤50, “Popular”)**, **Pro $499 (≤200)**, **Enterprise $999 (≤500)**, **Enterprise+ $1,999 (500+)** — each with audience line + feature bullets. Current plan is marked. A notice if Stripe isn’t configured yet (“payments not switched on — choice is saved”).
- **Action:** choose a tier — POST `tier=<key>` to `billing:checkout` (starts Stripe checkout, or saves the choice if unconfigured).

### D2. Checkout success — `/billing/<id>/success/`
- Reassurance page after payment: “You’re set for the season,” plan name, back to dashboard. No form.

### D3. Donation pledge — `/billing/<id>/pledge/`
- **Who:** owner.
- **Purpose:** commit the league’s donation for the season.
- **Fields:** `pledged_amount` (AUD number), `payment_schedule` (radio — e.g. upfront / instalments / at season close), `matching_enabled` (checkbox — “match participant top-ups $-for-$”), `matching_cap` (AUD number, required if matching on). Show a **suggested minimum** hint.
- **Action:** saves → dashboard.

### D4. Top-up / “Add to the cause” — `/billing/<id>/top-up/`
- **Who:** **any member** (not just owner). Shown as an optional nudge after joining a league that has a pledge.
- **Shows:** the live donation summary — charity, base pledge, top-ups so far, matched amount, running **raised / goal** with % progress, matching cap remaining.
- **Field:** `amount` (AUD number). **Action:** records the top-up (or Stripe donation checkout) → leaderboard, with a thank-you that shows the matched amount (e.g. “Your $20 became $40 thanks to your organisation”).

### D5. Season summary — `/billing/<id>/season-summary/`
- **Who:** managers.
- **Shows:** end-of-season wrap — the winner + top-3 **podium**, the final donation summary (total raised, breakdown), subscription/plan, and (owner + Pro-plan) a link to download the **ESG report PDF**.
- **Action (owner):** **Close the season** — POST to settle the donation pool for disbursement; afterwards shows the disbursement record.

### D6. Receipt — `/billing/<id>/receipt/`
- **Who:** owner. A clean, printable **platform service-fee receipt** (GoodTip Pty Ltd) — plan, amount, season, org. No form.

---

# E. Admin / “Manage” (staff-only back office)

Own sub-layout with its own side/top nav (Overview, Orgs, Sync). Denser, tool-like, same palette.

### E1. Overview — `/manage/`
- Stat tiles: total orgs, rounds, matches, tips. List of 5 most recent orgs.

### E2. Orgs list — `/manage/orgs/`
- Table of all orgs (newest first) with links into each org’s rounds/members.
- **Create-org form:** `season`, `charity_name`, `charity_url`, `name`, `sport` (AFL / NRL / BOTH).

### E3. Org rounds — `/manage/org/<id>/rounds/`
- List of rounds for an org (number, series, stage, lockout, status) with links to each round’s matches.
- **Forms:** create round — `action=create`, `series`, `round_number`, `stage`, `lockout_at`, `status`; update status — `action=status`, `round_id`, `status`.

### E4. Round matches — `/manage/org/<id>/round/<round_id>/matches/`
- List of matches in a round.
- **Forms:** add match — `action=create`, `home_team`, `away_team`, `kickoff_at`, `venue`; enter result — `action=result`, `match_id`, `home_score`, `away_score` (grades tips on save).

### E5. Org members (admin) — `/manage/org/<id>/members/`
- Members table + the org’s join link. **Forms:** `action=remove` (`member_id`); `action=promote` (`member_id`, toggles Participant ↔ Manager).

### E6. Sync — `/manage/sync/`
- Pull fixtures/results from a data provider. **Form:** `competition`, `round_number`, `org_id`, `kind` (fixtures / results).

---

## Priority order (if built in waves)
1. **Auth** (signup, login, reset) — first impression, simplest.
2. **Dashboard + Tip round + Leaderboard + My tips** — the daily loop, highest value.
3. **Create league / Invite / Members / Charity vote** — onboarding.
4. **Billing** (plans, pledge, top-up, season summary, receipt).
5. **Admin/Manage** — internal, lowest polish priority.

For each page you can ask web Claude for: the HTML + CSS, mobile + desktop, empty/loading/error states,
and — for Tip round, Leaderboard, My tips — keep the interaction model (instant save / live filter) intact.
