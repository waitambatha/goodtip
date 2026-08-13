# GoodTip — AI Group Recap, Feature Specification

*For Ambrose & Erick · Prepared 15 July 2026 · 44 days to launch (28 August 2026).*
*(Client-supplied spec, pasted into the repo 17 July 2026 — the original doc was
missing from `~/projects/adds`. Treat this file as the source of truth.)*

## 1. What it is

The AI Group Recap is a short written summary of a group's tipping round,
generated automatically from real results. No group activity, no recap. It
never uses example or placeholder data, in or out of production.

> Dave backed Collingwood again this week and he's holding steady. Julie in
> accounts went for the Roosters and she's on the rise, now third on the
> leaderboard.

This is one of the few things only GoodTip can say. A generic tipping app can
show a leaderboard. Only GoodTip can tell a workplace that Dave's loyal to a
losing team and Julie's quietly climbing. That's the point of the feature: it
makes the group's own data feel like a story, not a spreadsheet.

## 2. Where it lives

One surface only: **the Wall**, the group's in-app feed. Posted as a card at
the top of the feed, visible to group members only, logged in.

No public or pre-login version. It does not appear on the News page and it
does not use, or need, the public-visibility consent flag defined for The
Good List. This is a private surface for the group, full stop.

## 3. Trigger and cadence

- Generated once per round, per group, after the round's last match locks and
  results are final.
- Not generated mid-round. Every tip in the round must be resolved
  (win/loss/draw) before generation runs, so no recap gets overtaken by a
  later result.
- One recap per group per round. Not per user. It reads across the whole group.
- Regeneration should not happen automatically after a result correction. If
  admin corrects a result, flag the affected recap for manual admin review
  rather than silently rewriting it, so a group never sees the story change
  under them without explanation.

### Email nudge

When a recap posts to the Wall, send each group member a short email with a
link back into the app. This is a re-engagement trigger, not a second copy of
the recap.

- **Trigger:** fires off the recap actually posting, not a fixed calendar
  slot. NRL runs Monday Night Football most weeks and AFL occasionally
  carries a fixture into Monday, so a hardcoded "send Monday morning" trigger
  will sometimes fire before the round is finished. In a normal round with no
  Monday fixture, this naturally lands early week anyway, without hardcoding
  it and risking the weeks it doesn't.
- **Content:** a short teaser line, not the full recap text, plus one link
  back to the Wall. The point is to pull the person back into the app to read
  it and see what the group's saying, not to let the inbox be the whole
  experience.
- **Frequency:** one email per group per round, sent once, no follow-up chase
  if it goes unopened. Respects the person's existing notification
  preferences, same as the tip reminder emails already specced.
- **No recap, no email:** if nobody tipped and no recap generated, no email
  goes out. Silence stays silence across both surfaces.

## 4. Data inputs

Everything the recap needs already exists in the platform's own results data.
No new data collection required.

- Group roster and display names for the round
- Each member's tips and outcomes for the round (correct, incorrect, draw)
- Round points and running season points per member
- Leaderboard position this round vs. last round, per member (movement, not
  just rank)
- Any streak worth noting: correct picks in a row, same-team loyalty across
  rounds, biggest single-round jump
- Round metadata: round number or name (including Round O for State of
  Origin), competition (AFL, AFLW, NRL, NRLW)

## 5. Group and member memory

The recap should draw on more than the current round. Most of what it needs
already exists in the results history; this section is about using it, not
collecting anything new.

- **What's remembered, derived:** structured facts pulled directly from
  results data: active streaks (correct picks, same-team loyalty regardless
  of outcome), leaderboard movement over several rounds, head-to-head
  patterns between two members who keep swapping the same spot, and
  milestones (round count, first win, biggest single-round jump).
- **What's remembered, self-reported:** a short optional get-to-know-you step
  at onboarding: a nickname if the member wants one used in their recaps,
  favourite team in each code (AFL, AFLW, NRL, NRLW), favourite TV show,
  favourite band or artist. Every question skippable individually, never
  required to complete signup, editable later in profile settings. Team
  loyalty and nickname are the valuable ones, they feed the recap directly
  with real, member-supplied detail rather than anything borrowed from
  outside the platform. TV and music are lighter flavour, see Section 8 for
  how sparingly to use them.
- **Nicknames specifically:** only ever the member's own choice, entered by
  them, never assigned by the model or by an admin. If a member hasn't set
  one, the recap uses their display name. This is what gives GoodTip's own
  recaps real character over time without drawing on anyone else's club or
  anyone else's people.
- **Why these categories:** apolitical, low-controversy, can't reasonably
  offend anyone, and nobody is asked to disclose anything personal about
  themselves beyond a preference and a name they choose for themselves.
- **What's not remembered:** no persisted character judgements. The model
  does not store or carry forward a label like "stubborn" or "the group's
  weak link" from one round to the next. Every recap is generated fresh from
  current facts, so a bad run early in the season doesn't calcify into a
  permanent characterisation of one person.
- **Repetition control:** keep a short log of callouts already used for a
  member (a streak already congratulated, a milestone already marked) so the
  same joke or the same milestone doesn't get repeated across rounds.
- **Visibility:** a member should be able to see the factual pattern data
  held about them, the same way they can see their own tip history. Nothing
  here is inferred or hidden, it's their own results read back to them.

## 6. Member input (optional)

Occasionally, not every round, prompt a member for a one-line reaction to
their result. A single lightweight question, answerable in seconds: how are
you feeling about this week's tips? Excited, gutted, indifferent, whatever's
true.

- **Trigger:** selective, not universal. Pick from standout moments in the
  round: a big win, a rough loss, a broken or extended streak, a milestone.
  One or two members per group per round at most, never the whole group at
  once.
- **Format:** one question, quick-pick mood plus an optional short text field
  (roughly 140 characters). No open-ended essay prompt.
- **Use in the recap:** if a member responds, their actual words can be
  quoted in that round's recap, attributed by first name. Never paraphrased,
  never smoothed into house style, never invented if nobody responds. This is
  what gives the recap a real voice instead of a manufactured one, the same
  principle behind Alistair's DHC email: real, specific detail about the
  actual people involved, not generic colour.
- **Moderation:** responses pass a basic content check before they're
  eligible for use in an automated, republished feed. A response that fails
  is simply not used and the recap falls back to results only. No response is
  ever surfaced without passing this check first, workplace context doesn't
  remove the need for it.
- **Opt-out:** a member can decline the prompt with one tap. No response
  required, no chasing. The recap works perfectly well with zero quotes in a
  round; this is seasoning, not a dependency.

## 7. Generation approach

**Build decision, 13 Aug 2026: written in house, no model API.** The recap
is composed by `orgs/recaps.py` from the group's own numbers. Nothing leaves
the server and there is no key, no per-round cost and no provider to be down.
This overrides the original recommendation in this section, which is kept
below so the tradeoff stays on the record.

The concern that motivated the original recommendation was that templating
"reads as robotic within two or three rounds". Three things in the
implementation answer it, and they are the parts to protect in any future
change:

- **The writer chooses what to lead on, not just how to say it.** A perfect
  round, a tie at the top, a new leader, a big climber and a game the whole
  group got wrong are separate openings, picked by what actually happened.
  Two rounds with identical scorelines but different shapes read differently.
- **Sentence selection is budgeted by interest, not by running order.** Four
  sentences is a tight budget; the game the group got wrong outranks the
  runner-up's margin, so what gets cut is decided by what is worth reading.
- **Phrasing is seeded on (org, round).** Each group gets different wording
  week to week, and re-running a round reproduces it exactly, which matters
  when the recap is the pinned card a group argues about all week.

If the prose does start to feel repetitive, the fix is more openings in
`_opening` and more entries in the per-code word banks, not a model call.

Requirements that still hold either way:

- **Input:** structured facts only, built server-side from the data in
  Sections 4 and 5. Never free text or unvetted user content.
- **Output:** plain text, 2 to 4 sentences, no headline, no emoji, no
  hashtags.
- **Voice:** Sections 8 and 9 are enforced at composition, by choosing the
  words. `orgs/tests.py::RecapWriterTests` checks the output of every branch
  against them.
- **Cost and reliability:** batch all of a round's group recaps in one pass
  after lock, never real-time on page load. A round the writer can find
  nothing to say about falls back to a simple factual line (see Section 10),
  never a blank card or a visible error.

*Superseded recommendation, kept for the record:* generate recap text by an
LLM call, Anthropic API as the reference implementation, provider-agnostic
(structured JSON in, constrained plain text out, batched), with the brand
voice rules enforced in the prompt itself.

## 8. Voice rules

These are non-negotiable and must be enforced in the prompt itself, not
corrected after the fact:

- Short, declarative sentences. Active verbs.
- Real names, real numbers. Never invented detail, never a rounded-off vague
  claim.
- No em dashes. Use full stops or commas.
- No weasel words: may, maybe, hope, wish, try, could, perhaps, strive. State
  what happened.
- No triads ("no X, no Y, just Z") and no fragment-stacking.
- No rhetorical question openers.
- British English spelling throughout.
- Celebrate, don't preach. The recap reports what happened. It never
  moralises about gambling, money, or winning.
- Hunt for the one moment in the round worth remarking on: a streak, a
  boilover nobody picked, a photo finish on the leaderboard, a member's real
  quote if one exists. Don't just report the ledger.
- Draw the story from facts (Section 5's memory, Section 6's real quotes),
  never generate a fresh personality judgement about a member and never let
  one persist across rounds.
- Write in Australian sporting vernacular: dry understatement,
  self-deprecating humour, colloquial phrasing (clocked up, had a red-hot go,
  kept it tight, took the points). This is shared cultural register, not any
  one person's voice, and it is GoodTip's target register.
- Team loyalty (Section 5) can be used freely when the tipping data actually
  earns it, a member backing their own team despite a loss, or turning on
  them for the round, is a genuine story beat.
- TV and music preferences (Section 5) are used rarely, and only for a
  connection so obvious it doesn't need explaining. A forced pun that then
  explains itself is the exact "explained cleverness" failure mode already
  ruled out elsewhere in GoodTip's voice. If the model has to work to make
  the reference land, don't use it.

**Reference standard:** the weekly email Alistair Campbell writes by hand for
Doncaster Hockey Club. Alistair is a friend of Ian's and has given permission
for his writing to be used in developing GoodTip's voice. The dry wit and
understatement in it are Australian sporting vernacular, not his invention,
and that's fair game regardless, it's exactly how GoodTip should sound.
What's specifically his is DHC's own characters and in-jokes, teammates who
didn't personally sign up to be a style reference for a commercial product.
Reuse the register freely, and with permission his actual emails can be used
as literal few-shot examples in the prompt (Section 9). Keep the
club-specific detail, names, nicknames, running jokes belonging to other
people, tied to DHC. It doesn't travel into some other workplace's recap.

## 9. Voice grammar in detail

This breaks the register down into rules specific enough to embed in the
generation prompt. It's built from studying the structural moves in
Alistair's DHC email, and, with his permission, his actual emails can go
further than inspiration: used directly as few-shot reference examples in the
prompt itself, alongside the rules below. Few-shot examples are generally the
strongest lever for getting a model to hold a register consistently, stronger
than rules alone.

**Using the emails as few-shot examples:** include one or two of Alistair's
actual emails in the system prompt as "here is the register, write in this
spirit" reference material, with the rules below layered on top as explicit
constraints. Don't let the model copy DHC's own names, nicknames, or in-jokes
into another group's recap, that detail belongs to Alistair's club, not the
group whose recap is being written.

### Sequence

Each result follows a fixed beat order, compressed to fit the recap's 2 to 4
sentence limit:

1. Stakes or context first, if any exist (ladder implication, a needed win, a
   rivalry). Skip this beat if there's nothing worth saying, don't
   manufacture stakes.
2. The key moment or turning point, named with a real action, not just a
   score.
3. The outcome, stated once, plainly, with the actual numbers.
4. One standout mention, a member who did something worth naming.
5. A closing line that plants the result in the bigger picture (points,
   ladder movement, streak), never a generic sign-off.

### Sentence-level rules

- Verbs carry the result. Prefer a specific action verb over stating the
  scoreline as a fact on its own (a team moved ahead, held on, ran down a
  target, rather than just X beat Y).
- State losses and draws with the same evenness as wins. No sympathy, no
  scolding, just what happened.
- One dry aside maximum per recap, and only if it's genuinely earned by
  something specific in the data. Never force one in.
- Numbers stay concrete and stay attached to a name. Never a vague magnitude
  ("a big win", "barely scraped in") without the actual figure sitting next
  to it.
- Vary sentence openers. Don't start every recap with the same construction
  ("This week...", "In round X...").

### Lexical bank

Colloquial synonyms for common results, to draw on instead of flat statements
of fact. Not exhaustive, and the model shouldn't cycle through them
mechanically, but this is the register:

- **Winning:** got the points, took the chocolates, made it count, ran out
  comfortable winners
- **Losing:** came unstuck, ran out of legs, left it too late, couldn't find
  a way through
- **Close result:** went down to the wire, split by a single pick, nothing in
  it
- **Consistency:** kept the wheels turning, steady as they come, doing it the
  hard way

### AFL and AFLW vernacular

Use the code's own language, matched to the round metadata already in
Section 4. An AFL recap should never borrow NRL terms and vice versa, that's
the fastest way to sound like it wasn't written by someone who follows the
sport.

- **Goal and scoring:** snag or sausage roll (a goal, rhyming slang), bag
  (multiple goals in one game, "kicked a bag"), major
- **Marking and skill:** speccie (spectacular mark), clunk (strong contested
  mark), sold the dummy (beat a defender with a fake), banana or checkside (a
  curving kick around the body), torp (a long spiralling kick)
- **Result and form:** got the four points (won), home and hosed (comfortably
  won), hit the woodwork (hit the goalpost), gun (an excellent player), ball
  magnet (a player who always seems to find the footy)
- **Ladder and season:** minor premiers (top of the ladder after the regular
  season), wooden spoon (finished last), barrack for (support a team)
- Applies identically to AFLW. Same vernacular, same weight, no separate or
  lesser register for the women's game.

### NRL and NRLW vernacular

- **Scoring:** meat pie (a try, rhyming slang), converted (goal kicked after
  a try), set of six (a team's run of six tackles)
- **Play and skill:** hit-up (a forward taking the ball into the defensive
  line), grubber (a low kick along the ground), dummy (a fake pass), don't
  argue (a fend or hand-off), sidestep (evading a defender), shot!
  (commentary praise for a strong tackle)
- **Result and form:** golden point (sudden-death extra time after a draw),
  sin bin (a ten-minute send-off), ripper or blinder (a great individual
  performance), wooden spoon (finished last)
- Applies identically to NRLW. Same vernacular, same weight, no separate or
  lesser register for the women's game.

### Cross-code rule

- Never use a term that disparages the other code to make a point about this
  one. Aerial ping-pong, cross-country wrestling, and similar put-downs exist
  in fan culture on both sides, and none of them belong in GoodTip's voice.
  GoodTip runs AFL and NRL as equals, the copy has to actually behave that
  way.
- Code-specific vernacular is seasoning, same rule as everything else in this
  section. If a term doesn't fit naturally, use plain language instead of
  forcing it in.

### Banned register

These read as corporate or as generic AI output, not as a club voice, and
undercut everything above:

- Hype adjectives: epic, incredible, legendary, dominant, seamless, elevated
- Corporate softeners: leverage, journey, moving forward, at this point in
  time
- Manufactured excitement: exclamation marks, emoji, any line that announces
  its own energy rather than earning it

### Worked example

A flat, ungoverned model output for the same data:

> This week Dave tipped Collingwood again and scored well. Julie also had a
> great round and moved up to third place on the leaderboard, which is an
> exciting result for her!

Applying the grammar above:

> Dave backed Collingwood again this week and he's holding steady. Julie in
> accounts went for the Roosters and she's on the rise, now third on the
> leaderboard.

Same facts, same length. The difference is verb choice (backed, holding
steady, on the rise, not tipped, scored well, moved up), the dropped
exclamation mark, and the numbers doing the work instead of adjectives.

## 10. Edge cases

| Case | Handling |
| --- | --- |
| Group's first ever round | No comparison data yet. Recap introduces the group's opening result without movement language. |
| Nobody tipped this round | No recap generated. No card shown. Silence, not an empty or apologetic message. |
| Nothing worth two sentences in the round | Fallback to one factual line: top scorer's name and round points only. |
| Very small group (2 to 3 people) | Same rules apply. No lowered bar on data reality just because the group is small. |
| Round O (State of Origin) | Recap must reflect 4-point scoring and reference Origin by name, not treat it as a normal round. |
| Member responds but fails moderation | Response not used. Recap generates from results data only, as if nothing was submitted. |
| No one responds to the prompt | Expected, most rounds. Recap generates from results data only. This is the default case, not a failure case. |
| Member skipped onboarding questions | No team, show, or artist preference to draw on. Recap runs on results data alone. Never invent a favourite to fill the gap. |
| Round includes a Monday fixture | Recap and email nudge wait for that match to lock like any other. They land later in the week than usual. That's correct, not a delay to fix. |

## 11. Admin controls

- Admin can view a group's recap before or after it posts to the Wall.
- Admin can hide a single recap from a group's Wall without deleting the
  underlying data.
- No free-text admin edit field for launch. If a recap is wrong, hide it and
  let the next round supersede it, don't build an editor for launch scope.
- Responses that fail moderation are logged for admin visibility, not
  silently discarded, so a pattern of a member submitting inappropriate
  content can actually be seen.

## 12. Decision needed from Ian

Everything above is buildable now. GoodTip already holds the result data this
feature needs; it does not depend on any other Milestone 3 item. The open
question raised at the 9 July standup, launch or Phase 2, is a scope and
timeline call, not a technical one. This spec removes the technical excuse
either way.

Worth naming plainly: this has grown a long way past a short paragraph on the
Wall. It now includes behavioural memory, an onboarding step, an optional
sentiment prompt with its own moderation requirement, a detailed voice
grammar, and an email re-engagement trigger. All of it is good and all of it
is buildable, but not all of it needs to exist on 28 August.

- **Recommended launch minimum:** the recap itself (Sections 1 to 4, 7 to 9):
  results-driven, posts to the Wall, in voice. This alone delivers the thing
  only GoodTip can say.
- **Recommended fast-follow:** group and member memory (Section 5), the
  sentiment prompt (Section 6), and the email nudge (Section 3), added once
  the core recap is live and actually being read. Each one makes the recap
  better, none of them are load-bearing for launch.

Recommend confirming both the launch/Phase 2 call and this internal split in
this week's standup, so Ambrose can slot the launch-minimum piece against the
org-creation and admin-mapping work still ahead of it, without inheriting the
full scope of this document on day one.
