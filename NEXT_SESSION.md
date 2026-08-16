# Continuing wreckfest-telemetry on a new machine

This file is a copy-pasteable prompt (plus context) for starting a fresh
Claude Code session on a different computer and picking this work back up.
Delete or overwrite this file once it's no longer needed -- it's a handoff
note, not a permanent part of the project.

## IMPORTANT — do this first, before anything else

The work described below is sitting as **uncommitted changes** in
`wreckfest_telemetry.py` on the *original* machine (check with `git status`
/ `git diff --stat` there). It has **not been committed or pushed**. If
you're reading this on a new machine, those changes do not exist here yet
-- you need to either:
  - go back to the original machine, review + commit + push the changes,
    then `git pull` here, or
  - manually copy `wreckfest_telemetry.py` over some other way.

Don't start re-implementing anything below from scratch without first
checking whether it's already sitting there uncommitted somewhere.

---

## The prompt to paste into a new session

```
This is a continuation of work on `wreckfest-telemetry`, a tool that reads
live Wreckfest race telemetry from the game process's memory (Wine/Proton,
via /proc/{pid}/mem) and logs/posts race results to an API.

Main file: wreckfest_telemetry.py (single file, ~2000 lines)

## History
- PROJECT.md in this repo has the full session-by-session history — read it
  first for context on past bugs/fixes, especially the newest few entries
  dated 2026-08-15.
- A sibling investigation project, wf-memory-tool (path varies by machine —
  ../wf-memory-tool relative to this repo on the machine this was written
  on; may not exist / may need cloning on a new machine), has its own
  PROJECT.md with a longer, more detailed reverse-engineering history
  (memory structure discovery, GDB techniques) — check it too when a
  question touches on how some memory structure was originally found.
- GDB attach/detach safety procedure and script templates live in
  wf-memory-tool/investigation/gdb_scripts/README.md — read before attaching
  GDB to the live game process (never kill -9 an attached session).

## Code, briefly
- Player results: 24 fixed-stride slots (SLOT_STRIDE=272), found via
  fast_find_slots() or scan_for_sentinels(). Per-player struct reached via
  OFF_PLAYER_PTR; POFF_NAME=0 holds the inline name string.
- Car names: NEW as of 2026-08-15 — no longer read from the per-player
  struct (POFF_CAR was removed). Now resolved once per finalized race via
  resolve_car_names(), which reads a separate fixed-stride (0x80-byte)
  roster car-name table, discovered structurally and cached per-pid. See
  that section of the file and the 2026-08-15 PROJECT.md entry for why.
- Lap/finish tracking: NEW as of 2026-08-15 — OFF_FINISHED_FLAG/FINISHED_BIT
  is NOT a "race finished" signal, it's the parity bit of a real per-player
  lap counter at OFF_LAP_COUNTER (addr-31). PlayerResult.laps_completed is
  the real signal now; _position_sort_key() ranks on it. See that section
  and the PROJECT.md entry — there's a known, documented, unsolved gap in
  how multiple simultaneous DNFs get sub-ordered relative to each other.
- Local player identity: CLIENT singleton hash-registry lookup
  (_local_player_slot_index / _mark_local_player), with a 30s stale-value
  fallback added 2026-08-14. A DIFFERENT, NOT-yet-fixed identity
  misattribution bug was found 2026-08-15 (see below) — not covered by that
  existing fix.
- Tuning: _read_tuning_widgets() (hash-registry + tune-array mechanism) is
  the sole live source, confirmed reliable 2026-08-14. An older
  "loadout array" source was removed after being caught returning wrong
  values live.

## Ground rules
- Debug live against the actual running game process when possible — this
  project's whole methodology is live verification over guessing, including
  writing small ad hoc tracer scripts (see scratchpad examples described in
  PROJECT.md's 2026-08-15 entries) when investigating a new memory field.
- Don't commit to git unless explicitly asked.
- Be plain about mitigation vs. root-cause fix.

## Current state / next task
Last session (2026-08-15) shipped two verified live fixes (car names now
come from a separate, more reliable roster table; DNF'd players no longer
outrank real finishers in the results, via a newly-found real lap counter)
and found — but did NOT fix — a third, real bug: if the LOCAL player never
generates their own legitimate finish event (e.g. times out having
completed 0 laps), the tool can misattribute a different, already-DNF'd
player as "you" and log a race with the real local player missing
entirely. The existing session-level confirmed_local_name guard caught it
and correctly skipped the API post, but a bad entry is still sitting in
race_log.jsonl uncleaned, and the underlying resolution bug is unfixed.

Next task: root-cause and fix that identity misattribution. Likely needs
the same live raw-struct-diff tracer approach used to find the lap-counter
bug — watch CLIENT's raw value (and _local_player_slot_index()'s
resolution of it) continuously through a deliberately-induced "local
player times out with 0 laps while other racers finish/DNF normally"
scenario, and see what it actually resolves to and why. Also worth
cleaning the one known-bad entry out of race_log.jsonl once the root cause
is understood (don't blindly delete it first — it may be useful evidence
until the bug is actually reproduced/understood).

Secondary, lower-priority open item: the DNF sub-ordering gap noted above
(PROJECT.md's 2026-08-15 entry #2) — total_time_ms doesn't reproduce the
real relative order of multiple simultaneous DNFs; some more precise
progress signal probably exists and hasn't been found.
```

## Where to get more info

- **`PROJECT.md`** (this repo) — full history, newest entries first. Read
  the 2026-08-15 entries in full before doing anything else; they're
  detailed and explain exactly what was found, how, and what's still open.
- **`wf-memory-tool/PROJECT.md`** (sibling repo, separate machine-local
  path) — deeper reverse-engineering history if a question comes up about
  *how* some existing offset/mechanism was originally found.
- **`wf-memory-tool/investigation/gdb_scripts/README.md`** — GDB safety
  procedure, read before attaching to the live game.
- **This session's ad hoc investigation scripts** (raw-struct diff tracer,
  DNF watcher, car-name-table scanner) were written to a scratchpad
  directory outside the repo and won't exist on a new machine/session —
  treat PROJECT.md's description of the techniques as the reusable part,
  not the literal script files.
