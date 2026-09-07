# Wreckfest Telemetry

Reads your Wreckfest race results live, straight out of the game's own memory, and prints/logs/POSTs each completed race automatically — no game mods, no plugins, no restarting the game.

## Requirements

- Linux, with Wreckfest running under Steam Play (Proton)
- Python 3.8+ (standard library only — nothing to `pip install`)
- Read access to `/proc/<pid>/mem` for the game process (see [Troubleshooting](#troubleshooting) if you get a permission error)

## Quick start

Find the game's PID:

```bash
grep -l "^Wreckfest_x64" /proc/[0-9]*/comm 2>/dev/null | cut -d/ -f3
```

Run the tool with that PID:

```bash
python3 wreckfest_telemetry.py --pid 12345
```

It attaches, waits for a race to actually finish, then prints a results table and appends the race to `race_log.jsonl` (one JSON object per line). Leave it running in the background across as many races as you want — press Ctrl+C to stop.

## Options

| Flag | Effect |
|---|---|
| `--pid PID` | **Required** (except with `--flush-only`). The Wreckfest process PID. |
| `--interval SECONDS` | Poll interval (default `0.5`). |
| `--json` | Also print each race as JSON to stdout. |
| `--log-file PATH` | JSON-lines file to append races to (default `race_log.jsonl`). |
| `--watch-tuning` | Instead of racing, watch the pre-race tuning screen and print each slider value as you set it. |
| `--debug` | Print the raw memory addresses used for each detected race. |
| `--no-api` | Don't POST to the configured API, even if `config.json` is set up. |
| `--queue-file PATH` | Offline queue of races that haven't been POSTed yet (default `pending_races.jsonl`, beside the log file). |
| `--flush-only` | POST any queued races and exit. Doesn't need the game running or a `--pid`. |

## Posting results to an API (optional)

Copy `config.json.example` to `config.json` and fill in your `api_key`, `supabase_url`, and `supabase_anon_key`. With that in place, every completed race is POSTed automatically, in addition to being appended to the log file. If `config.json` is missing or incomplete, API posting is silently disabled — everything else works the same either way.

### Running offline

You don't have to be online to race. If a POST fails for a *transient* reason — no
network, DNS failure, the backend down or timing out — the race is parked in
`pending_races.jsonl` next to your log file, and retried automatically: once at
startup, before each new race is posted, and on a slowing timer (1 → 2 → 5 → 15
minutes) while the tool runs. Queued races are sent oldest-first, so results
arrive in the order you raced them, and a race is removed from the queue the
moment the backend accepts it — a crash mid-flush can't double-post.

To drain the queue by hand after a session, with the game closed:

```bash
python3 wreckfest_telemetry.py --flush-only
```

If the endpoint actually *rejects* a race (a validation failure, or a bad
`api_key`), retrying would fail identically forever — so that one is moved to
`failed_races.jsonl` with the error attached and skipped, leaving the rest of
the queue to go through. The race is still in your log file either way; the
queue is only ever the second copy.

## How it works

Wreckfest runs under Proton, which is real Windows game code — so its in-memory data layout is identical to running natively on Windows. This tool opens `/proc/<pid>/mem` (the same interface a debugger uses) and reads that layout directly: a fixed-size table the game keeps for the current race's standings, plus the engine's own internal name-lookup table to resolve things like the track and car names to their real display text. A race is only reported once every driver's own "finished" flag is set in that table — not when times merely look plausible — so it won't fire early on a fast finish, a tie, or a paused game.

**Tuning** (suspension/gear ratio/differential/brake balance) is read separately, straight out of the game's own save file (`cars5.ccrs`) rather than live memory — this is your *persisted* tuning for whichever car you raced, so it's always included, whether or not you visited the pre-race Tune screen this session. (`--watch-tuning` is different: it reads the Tune screen's live memory instead, for watching a slider change value in real time as you drag it — the save file only updates once you back out.)

All of this depends on Wreckfest's current memory/save-file layout. A game update that changes these internal structures could break it; if results or tuning stop showing up correctly, that's the first thing to suspect.

## Troubleshooting

**`Permission denied reading /proc/<pid>/mem`** — your system's ptrace restrictions are blocking the read. Either run the tool with the same user as the game (should already be the case) and lower the restriction for this session:

```bash
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

or run the tool with `sudo`.

**No race ever gets detected** — make sure `--pid` is the actual game process, not a launcher/wrapper (`Wreckfest_x64.exe`'s own PID, found via the `grep` command above, not Steam's or Proton's PID).
