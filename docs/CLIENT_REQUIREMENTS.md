# GoodTip — What We Need From the Client

_Last updated: 2 July 2026_

This is the list of items we currently need from you (the client) to finish and
launch GoodTip. Each item says **what it is** and **why we need it**. Nothing
below blocks development of the rest of the app — but each one blocks the
specific feature named against it going live.

---

## 1. Blocking launch (needed before go-live)

### 1.1 Stripe API keys
**What:** Stripe **test** and **live** keys — Secret key, Publishable key, and
the Webhook signing secret (3 values per environment).
**Why:** All payments run through Stripe — the platform subscription fee,
participant top-ups, and donation matching. Until these keys are supplied, the
billing and top-up features stay dormant (they are built, but cannot take a real
payment). We need the **test** keys to verify the full payment flow now, and the
**live** keys before launch.

### 1.2 Sports fixtures / results API key
**What:** The API key for the sports data provider (e.g. TheSports API) —
`THESPORTS_API_KEY`.
**Why:** Rounds, matches, kick-off times, and final results are pulled from this
feed. Without it we cannot auto-load fixtures or grade tips, and everything has
to be entered by hand. Needed for a real season to run.

### 1.3 Email (SMTP) credentials
**What:** Outbound email server details — host, port, username, password, and
the "from" address.
**Why:** The app sends real emails for password resets, donation receipts, and
"charity suggested for review" notifications. Without these, those emails cannot
be delivered.

### 1.4 Domain DNS + SSL
**What:** DNS access (or the records applied) to point **goodtip.com.au** and
**www.goodtip.com.au** at our host, plus an SSL certificate.
**Why:** This is the public address of the site. The application is already
configured to trust `goodtip.com.au`; we just need the domain pointed at the
server and secured (https) so it's reachable and safe.

---

## 2. Blocking specific features / decisions

### 2.1 Eclat — written consent for public display
**What:** A direct, written "yes" from Eclat to be shown publicly on The Good
List as the featured proof point.
**Why:** The Good List spec flags Eclat as a live client kept off the standard
introduction pathway, so naming them publicly is a different level of exposure.
The pre-launch page leans on Eclat as its one real proof point — we should not
publish their name until they've agreed in writing.

### 2.2 Confirm the launch date
**What:** Confirmation of the season/launch date.
**Why:** The Good List spec references **28 August**. This date drives the
"season starts" messaging and the switch from the pre-launch holding page to the
live leaderboard. We want to confirm it's correct before wiring it in.

### 2.3 Confirm the Industry list
**What:** Sign-off on the list of industries groups can pick from (or your own
list to replace ours).
**Why:** The Good List groups workplaces "By Industry". We've seeded a starter
list — Professional Services, Hospitality, Construction, Retail, Finance,
Healthcare, Education, Technology, Manufacturing, Transport & Logistics,
Government & Public Sector, Not-for-profit, Other. It's editable at any time, but
we'd like your confirmed taxonomy so the public board reads the way you want.

---

## 3. Nice to confirm (not blocking)

### 3.1 The Good List thresholds
**What:** Confirm the two thresholds are set where you want them.
**Why:** Per the spec, an aggregate (by charity/state/industry) only shows
publicly once **5+** groups sit behind it, and the public By-Group board stays
hidden until **~10** named, consenting groups exist. Both are tunable by us in
the admin without a code change — just tell us if you want different numbers.

### 3.2 Charities on the public picker
**What:** Confirm the approved charity list (e.g. Lifeline, headspace, ReachOut).
**Why:** These are what groups vote for and what the public board ranks by. We
want the official approved set before launch.

---

## Quick reference — what to send

| # | Item | Format |
|---|------|--------|
| 1.1 | Stripe keys (test + live: secret, publishable, webhook secret) | 6 values |
| 1.2 | Sports fixtures API key | 1 key |
| 1.3 | SMTP credentials | host, port, user, password, from-address |
| 1.4 | DNS pointing goodtip.com.au → our host + SSL | access or applied records |
| 2.1 | Eclat written consent to public display | email / signed note |
| 2.2 | Confirmed launch date | a date |
| 2.3 | Confirmed industry list | a list (or "use yours") |
| 3.1 | Threshold values (privacy 5 / credibility 10) | confirm or change |
| 3.2 | Approved charity list | a list |
