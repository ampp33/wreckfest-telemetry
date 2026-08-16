# Continuing wreckfest-telemetry

Handoff note, not a permanent part of the project -- delete it once it has
served its purpose. Rewritten 2026-08-16; everything the earlier version
described as open has since been fixed and verified live, so none of that
text survives.

## State

Working tree has substantial **uncommitted** changes to
`wreckfest_telemetry.py` (project convention: don't commit unasked; the
repo owner commits at the end of a session). `PROJECT.md`'s newest entries
carry the full detail -- read them before touching anything, newest first.

Everything below was verified against the live game, most of it in real
online multiplayer races.

## What the tool now relies on

Three fields inside the per-slot result struct, all found and confirmed
during the 2026-08-15/16 session:

- **`OFF_FINISH_POSITION`** (byte 0 of the `addr-32` word) -- the game's own
  finishing position, zero-indexed. Exact. Validated against three separate
  online races' results screens (P12, P4, P2, all user-confirmed). This
  drives `_rank_players()` and replaced every ranking heuristic.
  Caveat: it is a **live running order** and RENUMBERS when a racer leaves
  the array, so it is only meaningful at capture time.
- **`STATUS_DNF_BIT` (0x10)** in the `addr-36` status field -- the engine's
  real DNF flag. Confirmed in an online race (exactly the 6 reported DNFs)
  and an offline one (all 24, nobody finished).
- **`STATUS_RUN_COMPLETE_BIT` (0x40)**, same field -- marks a racer whose run
  is complete. **It is NOT the local player**, despite the tempting
  evidence; see below. Used only as part of the results-time check.

Identity comes from **CLIENT**, as it always did. Capture fires at
**results time**: every real racer carries a terminal marker and
`_STILL_RACING_COUNT` is 0.

## Hard-won warnings

- **Offline races cannot distinguish "local player" from "only human" from
  "run complete".** Three separate claims were confirmed across four offline
  races each and then disproved by a single online race. If a hypothesis
  touches identity, ownership, or per-player state, it is not verified until
  a populated online lobby has tested it. Repeated confirmation under one
  condition is not confirmation across conditions.
- **`FINISHED_BIT` is not a finish signal** -- it is the low bit of the lap
  counter. Any race with an odd lap count silently never logged because of
  this. Don't reintroduce it.
- **The lap counter holds the CURRENT LAP NUMBER**, 1-indexed, so it reads
  one higher than laps completed. Corrected on the way out in
  `_player_to_dict()`; never trust the raw value.
- **Don't "fix" the capture point by waiting longer.** Waiting for the
  results *screen* risks a racer quitting first, which renumbers everyone
  below them. Capturing when the data freezes is deliberate.
- **The synthetic test suite lives outside the repo** (a scratchpad file) and
  hand-mirrors `scrape_players()`'s loop. It drifted **twice** in one session
  and silently tested stale behaviour both times. If it is kept, make it call
  the real function instead of copying it.

## Open / unfinished

1. **`0x40`'s exact meaning is still a guess** ("run complete" fits all
   observations). It is load-bearing for the results-time check, so a better
   understanding -- or a better signal -- would be worth having.
2. **Test harness drift** (above) -- the real fix is to stop duplicating
   production logic in the harness.
3. **API posting has been running with `--no-api` all session.** The first
   real post should be watched: that endpoint returns HTTP 200 even on
   validation failure, so success is only in the response body.
4. One pre-existing bad row in `race_log.jsonl` from **2026-08-05**
   (`Pa_Drogu` logged as the local player), left alone because it predates
   this work. Everything after it is clean.
