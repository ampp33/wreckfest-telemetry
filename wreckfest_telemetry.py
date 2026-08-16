#!/usr/bin/env python3
"""
Wreckfest Telemetry
Reads race result data from game memory via /proc/{pid}/mem. Works with Wine/Proton.

Usage:
  python wreckfest_telemetry.py --pid 1234          # attach and poll until Ctrl+C
  python wreckfest_telemetry.py --pid 1234 --json   # also emit JSON after the table

API posting (optional): copy config.json.example to config.json and fill in
api_key/supabase_url/supabase_anon_key to have each completed race POSTed to
the Wreckfest 2 Race Log backend automatically, in addition to --log-file.
"""

import argparse
import array
import glob
import json
import os
import re
import struct
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

_PROC_ERRORS = (FileNotFoundError, PermissionError)          # /proc read races (process gone/no perms)
_MEM_ERRORS  = (FileNotFoundError, PermissionError, OSError)  # ditto, plus a live process moving/exiting mid-read

# ── player-slot struct layout (offsets relative to total_time_ms address) ────
SLOT_STRIDE = 272   # bytes between entries in the 24-slot player array
MAX_PLAYERS = 24

CHAIN_STATIC_OFFSET = 0x2cbcde0   # module_base + this -> race_manager_ptr
CHAIN_ARRAY_OFFSET  = 0x1d84      # race_manager_ptr + this -> slot[0]

OFF_TOTAL_TIME   = 76
OFF_BEST_LAP     = -4
OFF_CLASS_RATING = 16
OFF_SENTINEL_A   = 20   # always 65535
OFF_PLAYER_PTR   = 68   # ptr64 -> player/car data block

# addr-32: int32. bit 0x100 was originally read as a one-shot "this player
# has finished" latch (see the git history for that original reasoning --
# it looked right at the time, against a race with no DNFs). CORRECTED
# live 2026-08-15, against a real race with several DNFs deliberately
# caused: bit 0x100 is just the low bit of byte 1 of this same int32 (addr
# -31) -- and that byte is a genuine, live-verified LAP COUNTER, not a
# finish flag. It increments by exactly 1 each time this player completes a
# lap (confirmed live: a still-racing player's counter climbs, e.g. 7->8,
# while a DNF'd player's freezes solid), so bit 0x100 just flips on/off
# with the counter's parity, roughly once per lap time (~20-30s), for every
# player, all race long -- it is NOT specific to actually finishing. The
# only reason this ever looked like a working "finished" signal is that a
# real finisher's counter (and total_time_ms) also happen to freeze at the
# same moment their last lap completes, since there's no next lap to flip
# it back -- purely incidental, and it fails exactly the way you'd expect
# once a DNF is in the mix: a dead/DNF'd player's counter can freeze on
# either parity depending on when they died, so this bit reads True or
# False for a DNF at random. See laps_completed on PlayerResult / the new
# OFF_LAP_COUNTER byte below for the real, verified-reliable signal.
OFF_FINISHED_FLAG = -32
FINISHED_BIT       = 0x100
# addr-31: single byte, byte 1 of the same int32 above. The real lap
# counter -- see the comment above for how this was found and verified.
# Read as (flag_word >> 8) & 0xFF from the same ri32 read as FINISHED_BIT,
# rather than a separate memory read.
OFF_LAP_COUNTER    = -31

# Per-player status bitfield, found live 2026-08-15 by diffing ~19 candidate
# offsets across all 24 slots through a real 24-player race. Mid-race it
# reads 0 or 4; it takes a terminal value the moment that player's result is
# finalized. Two bits are verified and used:
#
#   STATUS_RUN_COMPLETE_BIT (0x40) -- badly named, kept only to avoid churn.
#     **It is NOT the local player.** Best current evidence (2026-08-16,
#     online): it marks a racer whose run is COMPLETE -- seven slots carried
#     it simultaneously in a 22-player race, exactly the racers who had
#     finished, while others were still circulating; by the results screen
#     nearly the whole field had it.
#     It looked like a local-player flag for one structural reason: every
#     race it had been tested against was offline, where the local player is
#     the only human and (finishing normally) the only slot to end up with
#     it set at the moment it was sampled. It was briefly promoted to the
#     primary identity source on 2026-08-15 and a single real online race
#     disproved it -- the bit sat on `TiamaT` (the winner) while the local
#     player was `Ampp33` at another slot, and CLIENT was correct.
#     Do NOT use for identity. It IS used, scoped to the LOCAL player only,
#     as the finality edge in _race_is_final() -- "this player's own run is
#     over" is exactly what that check wants. Never test it across the whole
#     field: that fires when the leader finishes, not when the local player
#     does.
#
#   STATUS_DNF_BIT (0x10) -- "this player did not finish". Verified across
#     two opposite scenarios: a 24-player online race where exactly 6 slots
#     carried it and the user independently reported exactly 6 DNFs, and an
#     offline race where all 24 carried it and the user confirmed nobody
#     finished. No finisher has ever carried it. This is the project's first
#     real DNF signal -- everything before it inferred DNF status indirectly
#     from the lap counter.
#
# TIMING, important: this field is populated when results finalize, NOT
# maintained live during the race (confirmed live -- 0x40 was absent on
# every sample from green flag to finish, then appeared). That makes its
# appearance a genuine "this race is over for the local player" edge, which
# is exactly what _race_is_final() needs and what FINISHED_BIT never was.
# BYTE 0 of the same int32 as OFF_FINISHED_FLAG/OFF_LAP_COUNTER: the game's
# own FINISHING POSITION, zero-indexed (so position = byte + 1). Found live
# 2026-08-15 by taking a real 24-car race's in-game finishing order from the
# user and searching every field in and around the slot struct for one that
# reproduces it -- this byte matched all 24 exactly, finishers and DNFs
# alike, values 0..23 with no duplicates. Confirmed against two independent
# ground truths: the local player read 1 for a user-confirmed 2nd place, and
# the 15-strong DNF block matched the results screen's order name for name.
#
# This retires every heuristic the project used to guess ordering. It is
# also the answer to the DNF sub-ordering problem that had been open since
# the lap counter was found: the engine ranks DNFs by order of death, first
# out placed last (user-confirmed), and this byte already encodes that --
# something neither total_time_ms direction could reproduce, because time
# only correlates with progress while a car is actually moving (a wrecked
# car crawling for 90s can outlast the field while placing dead last).
OFF_FINISH_POSITION = -32   # byte 0 of the OFF_FINISHED_FLAG word
OFF_STATUS_FLAGS  = -36
STATUS_RUN_COMPLETE_BIT  = 0x40
STATUS_DNF_BIT    = 0x10

POFF_NAME   = 0
# POFF_CAR/POFF_ENGINE (used to be 48/288) are gone -- car name now comes
# from the roster car-name table (see resolve_car_names() below), which is
# both more reliable (doesn't go through the per-player struct at all, so
# it isn't affected by a car asset still streaming in) and doesn't need a
# per-player offset. Engine name was dropped entirely, unused downstream.

SENTINEL = struct.pack('<ii', 65_535, -1_000_000)

MIN_LAP_MS   = 3_000
MAX_LAP_MS   = 3_600_000
MAX_TOTAL_MS = 36_000_000
MIN_HEAP_PTR = 0x10000000

# ── track / variation snake-case fallback tables ─────────────────────────────
KNOWN_TRACKS = [
    "Big Valley Speedway", "Bleak City", "Bloomfield Speedway",
    "Bonebreaker Valley", "Boulder Bank Circuit", "Clayridge Circuit",
    "Crash Canyon", "Deathloop", "Devil's Canyon", "Dirt Devil Stadium",
    "Drytown Desert Circuit", "Eagles Peak Motorpark", "Espedalen Raceway",
    "FinnCross Circuit", "Fire Rock Raceway", "Firwood Motocenter",
    "Hellride", "Hillstreet Circuit", "Hilltop Stadium", "Kingston Raceway",
    "Maasten Motocenter", "Madman Stadium", "Midwest Motocenter",
    "Motorcity Circuit", "Mudford Motorpark", "Northfolk Ring",
    "Northland Raceway", "Pinehills Raceway", "Rally Trophy",
    "Rattlesnake Racepark", "Rockfield Roughspot", "Rosenheim Raceway",
    "Sandstone Raceway", "Savolax Sandpit", "Torsdalen Circuit",
    "Tribend Speedway", "Vale Falls Circuit", "Wrecknado",
]
KNOWN_VARIATIONS = [
    # longer/compound names must precede any string they start with
    "Race Track Reverse", "Race Track", "Outer Oval Loop", "Outer Oval",
    "Asphalt Oval Reverse", "Asphalt Oval", "Short Route Reverse", "Short Route",
    "Main Route Reverse", "Main Route", "Rally Circuit Reverse", "Rally Circuit",
    "Outer Route Reverse", "Outer Route", "Racing Track Reverse", "Racing Track",
    "Short Circuit Reverse", "Short Circuit", "Main Circuit Reverse", "Main Circuit",
    "Alt Route Reverse", "Alt Route", "Inner Route Reverse", "Inner Route",
    "Figure 8", "Free Route", "Dirt Oval", "Mud Oval", "Wild Circuit",
    "Inner Oval", "Oval", "Special Stage", "Reverse Circuit",
    "Trophy Circuit", "Dirt Speedway", "Open Circuit",
]


@dataclass
class PlayerResult:
    position:      int
    name:          str
    car:           str
    class_letter:  str
    class_rating:  int
    best_lap_ms:   int
    total_time_ms: int
    lap_times_ms:  list
    is_local:      bool = False
    finished:      bool = False   # this player's own OFF_FINISHED_FLAG bit is set -- see that constant's docstring, this is NOT a reliable "did they really finish" signal by itself
    # Real lap-completion count, from OFF_LAP_COUNTER -- see that constant's
    # docstring. Verified live 2026-08-15 against a race with 4 real DNFs:
    # the 2 genuine finishers both froze at 4 (the race's real lap count),
    # every DNF froze at 3 or lower. _position_sort_key() ranks on this
    # first, precisely so a DNF's misleadingly-low frozen total_time_ms
    # can't outrank someone who actually completed more of the race.
    laps_completed: int = 0
    # Raw per-player status bitfield from OFF_STATUS_FLAGS -- see that
    # constant for the verified bits. 0 means "not populated yet" (mid-race,
    # or a slot that never finalized), which is meaningful in itself:
    # _race_is_final() keys off STATUS_RUN_COMPLETE_BIT appearing here.
    status_flags:   int = 0
    # The engine's own finishing position, 1-based (see OFF_FINISH_POSITION).
    # 0 means "not read / not populated", which _rank_players() treats as
    # unusable and falls back on.
    finish_position: int = 0
    # Index into the 24-slot player array (0-23), set by scrape_players() --
    # not identity/gameplay data, just plumbing so resolve_car_names() can
    # map this player back to their row in the car-name table (see that
    # section) without needing to re-derive it from a raw slot address.
    slot_index:    Optional[int] = None

    def class_str(self) -> str:
        return f"{self.class_letter} {self.class_rating}"

    @property
    def dnf(self) -> bool:
        """True if the engine itself marked this player as not finishing.
        Only meaningful once status_flags is populated (results finalized);
        reads False mid-race, which is the correct answer then anyway."""
        return bool(self.status_flags & STATUS_DNF_BIT)

    def __eq__(self, other):
        return (isinstance(other, PlayerResult)
                and self.name == other.name
                and self.total_time_ms == other.total_time_ms)

    def __hash__(self):
        return hash((self.name, self.total_time_ms))


@dataclass
class RaceResult:
    track:     str
    variation: str
    timestamp: str
    players:   list
    tuning:    dict = None   # last-known {category: 0-4 index}, if any


def ms_to_str(ms: int) -> str:
    if ms <= 0:
        return "--:--.---"
    m, rem = divmod(abs(ms), 60_000)
    s, ms_r = divmod(rem, 1_000)
    return f"{m:02d}:{s:02d}.{ms_r:03d}"


def class_from_rating(r: int) -> str:
    if   r <= 100: return 'D'
    elif r <= 200: return 'C'
    elif r <= 250: return 'B'
    else:          return 'A'


def _ts() -> str:
    return time.strftime('%H:%M:%S')


# ── process / memory-region helpers ──────────────────────────────────────────
def _parse_maps(pid: int):
    """Yields (start, end, perms, dev, inode) for each /proc/pid/maps line."""
    try:
        with open(f"/proc/{pid}/maps") as f:
            for line in f:
                parts = line.split(None, 5)
                if len(parts) < 5:
                    continue
                s, e = parts[0].split('-')
                yield int(s, 16), int(e, 16), parts[1], parts[3], parts[4]
    except _PROC_ERRORS:
        return


def anon_writable_regions(pid: int) -> list:
    """Anonymous rw private regions only (device 00:00, inode 0) -- where the
    player-result array actually lives; skips file-backed/read-only mappings."""
    return [(s, e) for s, e, perms, dev, inode in _parse_maps(pid)
            if 'r' in perms and 'w' in perms and dev == "00:00" and inode == "0"
            and e - s > len(SENTINEL)]


def _writable_regions(pid: int) -> list:
    return [(s, e) for s, e, perms, _, _ in _parse_maps(pid) if 'w' in perms]


# ── low-level memory reads ───────────────────────────────────────────────────
def _rd(f, addr: int, n: int) -> Optional[bytes]:
    try:
        f.seek(addr)
        d = f.read(n)
        return d if len(d) == n else None
    except (OSError, OverflowError):
        return None


def ri32(f, addr: int) -> Optional[int]:
    d = _rd(f, addr, 4)
    return struct.unpack('<i', d)[0] if d else None


def ru64(f, addr: int) -> Optional[int]:
    d = _rd(f, addr, 8)
    return struct.unpack('<Q', d)[0] if d else None


def rcstr(f, addr: int, maxlen: int = 64) -> str:
    d = _rd(f, addr, maxlen)
    if not d:
        return ""
    end = d.find(b'\x00')
    return d[:end if end != -1 else maxlen].decode('utf-8', errors='replace').strip()


def _looks_garbled(s: str) -> bool:
    """Control chars / replacement char -- sign of mid-struct binary data, not a real string."""
    return any(ch < ' ' or ch == '�' for ch in s)


# Quake-style color-code markers some Wreckfest servers prefix onto player
# names (e.g. "^2*^0sharpneli" -> "sharpneli") -- a caret+digit color code,
# optionally preceded by a literal '*' when two codes sit back-to-back.
# Cosmetic only, not part of the real name.
_COLOR_CODE_RE = re.compile(r'\*\^[0-9]|\^[0-9]')


def _strip_color_codes(name: str) -> str:
    return _COLOR_CODE_RE.sub('', name).strip()


# ── struct discovery ──────────────────────────────────────────────────────────
_module_base_cache: dict = {}


def find_module_base(pid: int) -> Optional[int]:
    """Load address of Wreckfest_x64.exe -- stable for the process's lifetime, cached per-pid."""
    cached = _module_base_cache.get(pid)
    if cached is not None:
        return cached
    try:
        with open(f"/proc/{pid}/maps") as f:
            for line in f:
                if 'Wreckfest_x64.exe' not in line:
                    continue
                parts = line.split()
                if len(parts) >= 2 and 'r--p' in parts[1]:
                    base = int(parts[0].split('-')[0], 16)
                    _module_base_cache[pid] = base
                    return base
    except _PROC_ERRORS:
        pass
    return None


def fast_find_slots(pid: int) -> Optional[list]:
    """Follow the static pointer chain to the 24 player-result slots. None if chain broken."""
    module_base = find_module_base(pid)
    if module_base is None:
        return None
    try:
        with open(f"/proc/{pid}/mem", "rb") as f:
            manager_ptr = ru64(f, module_base + CHAIN_STATIC_OFFSET)
            if not manager_ptr:
                return None
            slot0 = manager_ptr + CHAIN_ARRAY_OFFSET
            if _rd(f, slot0 + OFF_SENTINEL_A, 8) != SENTINEL:
                return None
            return [slot0 + n * SLOT_STRIDE for n in range(MAX_PLAYERS)]
    except _PROC_ERRORS:
        return None


def cluster_sentinel_hits(hits: list) -> list:
    """Keep the run of addresses spaced exactly SLOT_STRIDE apart (the real
    24-slot array), discarding stray sentinel hits from UI mirrors/buffers."""
    if len(hits) < 3:
        return hits
    sorted_hits = sorted(hits)
    best_run: list = []
    current_run: list = [sorted_hits[0]]
    for addr in sorted_hits[1:]:
        if addr - current_run[-1] == SLOT_STRIDE:
            current_run.append(addr)
        else:
            if len(current_run) > len(best_run):
                best_run = current_run[:]
            current_run = [addr]
    if len(current_run) > len(best_run):
        best_run = current_run
    return best_run if len(best_run) >= 10 else hits


def scan_for_sentinels(pid: int, regions: list) -> list:
    hits = []
    CHUNK = 4 * 1024 * 1024
    try:
        with open(f"/proc/{pid}/mem", "rb") as f:
            for start, end in regions:
                size = end - start
                offset = 0
                try:
                    f.seek(start)
                except OSError:
                    continue
                while offset < size:
                    n = min(CHUNK, size - offset)
                    try:
                        chunk = f.read(n)
                    except OSError:
                        break
                    if not chunk:
                        break
                    pos = 0
                    while True:
                        idx = chunk.find(SENTINEL, pos)
                        if idx == -1:
                            break
                        hits.append(start + offset + idx - OFF_SENTINEL_A)
                        pos = idx + 1
                    offset += len(chunk)
    except _PROC_ERRORS:
        pass
    return hits


def validate_entry(f, addr: int) -> Optional[dict]:
    total_time   = ri32(f, addr + OFF_TOTAL_TIME)
    best_lap     = ri32(f, addr + OFF_BEST_LAP)
    class_rating = ri32(f, addr + OFF_CLASS_RATING)

    if any(v is None for v in [total_time, best_lap, class_rating]):
        return None
    if not (MIN_LAP_MS <= best_lap <= MAX_LAP_MS):
        return None
    if not (MIN_LAP_MS <= total_time <= MAX_TOTAL_MS):
        return None
    if best_lap > total_time:
        return None
    if not (50 <= class_rating <= 600):
        return None

    return {'addr': addr, 'total_time_ms': total_time, 'best_lap_ms': best_lap, 'class_rating': class_rating}


def read_player(f, d: dict, module_base: Optional[int] = None, table_base: Optional[int] = None) -> Optional[PlayerResult]:
    ptr = ru64(f, d['addr'] + OFF_PLAYER_PTR)
    if not ptr or ptr < MIN_HEAP_PTR or ptr > (1 << 47):
        return None

    name = _strip_color_codes(rcstr(f, ptr + POFF_NAME, 64))
    if not name:
        return None

    laps = [d['best_lap_ms']] if MIN_LAP_MS <= d['best_lap_ms'] <= MAX_LAP_MS else []

    # car is deliberately left blank here -- resolve_car_names() fills it in
    # for every player at once, once per finalized race, from the roster
    # car-name table (see that section's docstring for why: the per-player
    # struct's own car field, read straight off `ptr` the way this function
    # used to, can sit on outright garbage -- a leftover float, an
    # unresolved VEHICLE_NAME_<id>_<variant> template key -- for a
    # noticeable window while a car asset is still streaming in; the roster
    # table doesn't have that problem, confirmed live 2026-08-15). Dropped
    # along with it: the VEHICLE_NAME/_resolve_localized_string patch-up
    # this used to do here (moot, nothing left to patch up), and a
    # time-delta-shaped-string stray-pointer guard that used to piggyback
    # on the old car field as a validity signal for `ptr` -- MIN_HEAP_PTR
    # plus the non-empty-name check above are what's left guarding against
    # a bad player_ptr now.
    flag = ri32(f, d['addr'] + OFF_FINISHED_FLAG)
    finished = bool(flag is not None and flag & FINISHED_BIT)
    laps_completed = ((flag & 0xFFFFFFFF) >> 8) & 0xFF if flag is not None else 0
    status_flags = ri32(f, d['addr'] + OFF_STATUS_FLAGS) or 0
    finish_position = (flag & 0xFF) + 1 if flag is not None else 0

    return PlayerResult(
        position=0, name=name, car="",
        class_letter=class_from_rating(d['class_rating']),
        class_rating=d['class_rating'], best_lap_ms=d['best_lap_ms'],
        total_time_ms=d['total_time_ms'], lap_times_ms=laps, is_local=False,
        finished=finished, laps_completed=laps_completed,
        status_flags=status_flags, finish_position=finish_position,
    )


# pid -> number of REAL racers (named, occupied slots) whose status field has
# no terminal marker yet, i.e. who are still circulating. Set by
# scrape_players() from the raw slot array on every poll, and read by
# _race_is_final() to decide whether the results screen has been reached.
#
# Why this can't be derived from the returned player list: a racer who is
# still out on track and hasn't completed a lap has neither a valid lap time
# (so validate_entry rejects them) nor a terminal status (so
# _validate_any_slot_relaxed rejects them too), which makes them invisible in
# `players` at exactly the moment they most need to block a premature
# "results are in" verdict.
_STILL_RACING_COUNT: dict = {}

_LAST_GOOD_CLIENT_SLOT: dict = {}   # pid -> (raw_slot, monotonic_timestamp)
_CLIENT_SLOT_STALE_FALLBACK_S = 30  # how long a last-known-good reading stays trusted over a fresh -1


def _local_player_slot_index(f, table_base: int, pid: Optional[int] = None) -> Optional[int]:
    """CLIENT singleton's first field is the local client's car slot index in
    multiplayer; -1 (no network client) means solo/AI, which the engine seats
    at slot 0 -- but confirmed live 2026-08-14 that this field can ALSO read
    -1 during a genuine online race with real opponents clearly present
    (raw_idx=-1 caught directly on the results screen while other real
    players' results were on screen too) -- almost certainly the CLIENT
    object going stale once the race itself ends and you're just viewing
    results, matching the "connected-but-not-actively-racing" theory this
    project had suspected but never confirmed live before. Blindly trusting
    -1 -> slot 0 in that situation mislabels whichever entry happens to sit
    at slot 0 as the local player (silently wrong if slot 0 is a real
    stranger; silently a no-op, as in the case that caught this, if slot 0 is
    unpopulated) -- this is the root cause of the "identity misattribution"
    bug this project's session-level confirmed_local_name guard has been
    mitigating without ever being able to explain.

    Fix: remember the last raw value that was genuinely > -1 (per pid, with a
    timestamp) and keep trusting it for a short window even if a later read
    goes back to -1 -- covers exactly the "results screen briefly resets
    CLIENT" case without needing to guess online-vs-solo from player count
    (which doesn't work: a solo/AI race can have just as many populated
    slots as a networked one). A genuinely solo/AI race never has a
    known-good value to fall back on, so it still correctly defaults to slot
    0 as before. The window (_CLIENT_SLOT_STALE_FALLBACK_S) is short enough
    that it shouldn't bleed a stale value into a completely different next
    race, which take much longer than that to get back to a results screen."""
    client_obj = _hash_registry_lookup(f, table_base, "CLIENT")
    if not client_obj:
        return None
    raw = ri32(f, client_obj)
    if raw is None:
        return None
    if raw > -1:
        if pid is not None:
            _LAST_GOOD_CLIENT_SLOT[pid] = (raw, time.monotonic())
        return raw
    if pid is not None:
        cached = _LAST_GOOD_CLIENT_SLOT.get(pid)
        if cached is not None and (time.monotonic() - cached[1]) < _CLIENT_SLOT_STALE_FALLBACK_S:
            return cached[0]
    return 0


def _read_player_native_only(f, d: dict) -> PlayerResult:
    """Builds a player entry from the slot's own native fields (total time,
    best lap, class rating, finished flag) -- independent of `player_ptr`'s
    normal *struct-shaped* read, which can fail for the local player's own
    slot specifically (confirmed live 2026-08-05: `player_ptr` pointed into
    what looks like an unrelated engine string/name table rather than a
    normal per-player struct -- persisted unchanged for the whole race, not
    a timing/not-yet-populated issue). Only ever used as a fallback for the
    CLIENT-recognized local slot, once the normal player_ptr-based
    read_player() has already failed for it -- losing the local player's
    actual finish position/time over one bad pointer would be far worse
    than a placeholder (car gets filled in separately anyway, by
    resolve_local_car_name(), which never depended on player_ptr at all).

    Name specifically: still attempted at `ptr + POFF_NAME`, deliberately
    *without* read_player()'s MIN_HEAP_PTR floor -- confirmed live that even
    though the pointer fails that struct-validity check, the memory it
    points to is still validly mapped and readable, and the name field
    specifically read back correct and non-garbled (landing at the expected
    offset in what's apparently a shared name-interning table rather than a
    per-player struct, not by coincidence -- it was the real name, not some
    other registered string). MIN_HEAP_PTR exists to reject *structurally*
    invalid pointers (Wine low-memory stray values) before trusting a whole
    struct's shape; a single string field is much lower-risk to attempt
    even when that fuller trust isn't warranted, and `_looks_garbled()`
    still catches an outright bad read."""
    laps = [d['best_lap_ms']] if MIN_LAP_MS <= d['best_lap_ms'] <= MAX_LAP_MS else []
    flag = ri32(f, d['addr'] + OFF_FINISHED_FLAG)
    finished = bool(flag is not None and flag & FINISHED_BIT)
    laps_completed = ((flag & 0xFFFFFFFF) >> 8) & 0xFF if flag is not None else 0

    name = ""
    ptr = ru64(f, d['addr'] + OFF_PLAYER_PTR)
    if ptr:
        candidate = rcstr(f, ptr + POFF_NAME, 64)
        if candidate and not _looks_garbled(candidate):
            name = _strip_color_codes(candidate)

    return PlayerResult(
        position=0, name=name, car="",
        class_letter=class_from_rating(d['class_rating']),
        class_rating=d['class_rating'], best_lap_ms=d['best_lap_ms'],
        total_time_ms=d['total_time_ms'], lap_times_ms=laps, is_local=False,
        finished=finished, laps_completed=laps_completed,
        status_flags=ri32(f, d['addr'] + OFF_STATUS_FLAGS) or 0,
        finish_position=(flag & 0xFF) + 1 if flag is not None else 0,
    )


def _validate_local_slot_relaxed(f, addr: int) -> Optional[dict]:
    """validate_entry() for the ONE slot CLIENT has already independently
    named as the local player's -- with the lap-time plausibility floors
    dropped, because for this slot they reject the exact case they were
    never meant to judge.

    Why this is needed (root cause of the 2026-08-15 identity
    misattribution, traced statically 2026-08-15 through the code path the
    live symptom implicates -- see below for what is and isn't verified):
    validate_entry() requires `MIN_LAP_MS <= best_lap` and
    `MIN_LAP_MS <= total_time`. A player who times out having completed
    ZERO laps never posts a lap time at all, so their slot's best_lap is
    unset and the check rejects it -- even though the slot is a real,
    seated, correctly-identified racer. That single rejection used to
    cascade all the way to a wrong name being labelled "you":
      1. scrape_players() skips the slot -> the local player is missing
         from `pairs` entirely (matches the observed symptom exactly: 5
         rows logged for a 6-racer heat, real local player absent);
      2. _mark_local_player()'s slot-index loop correctly finds nothing;
      3. its salvage path re-ran *the same* validate_entry() and so failed
         for the very same reason, leaving local_player None;
      4. control reached the lowest-address fallback, which cheerfully
         labelled whichever other racer happened to sit in the lowest slot
         as the local player.
    So the misattribution never required CLIENT to return a wrong index at
    all (PROJECT.md's 2026-08-15 entry #3 guessed it did) -- CLIENT was
    most likely correct the whole time, and the bug lived entirely
    downstream of it. Both possibilities are covered now regardless: the
    lowest-address fallback is no longer reachable once CLIENT resolves
    anything (see _mark_local_player).

    The floors exist to keep the *sentinel scan* from trusting random
    memory that happens to look struct-shaped. This slot didn't come from
    that scan -- CLIENT named it -- so those particular heuristics are
    buying nothing here. What still has to be proven is that the seat is
    OCCUPIED, since an empty slot also reads zeroes and must never be
    salvaged into a phantom local player (that would fabricate a 1-player
    "race" out of an idle menu). Two independent occupancy proofs are
    required instead of the lap floors:
      - a class rating in the plausible range: set at race start, before a
        single lap is completed, so it's populated for a 0-lap racer but
        not for a genuinely empty seat;
      - a real, non-garbled name behind player_ptr.
    Unset lap/total times are then clamped to 0 rather than rejected -- 0
    is the honest value for "completed no laps", and laps_completed (the
    reliable signal, see OFF_LAP_COUNTER) carries the real ranking
    information anyway.

    Deliberately used only as a SECOND attempt, after the strict
    validate_entry() has already failed, so nothing about the existing
    bad-player_ptr salvage path changes. The one case neither tier can
    cover is a slot with BOTH a bad player_ptr and no laps completed --
    that is genuinely indistinguishable from an empty seat with this
    information, and is left uncovered on purpose rather than guessed at.

    NOT yet verified live: the game wasn't running when this was written,
    so the "best_lap is unset for a 0-lap timeout" premise is deduced from
    the observed symptom (the local player's absence from `pairs` is only
    explainable by validate_entry rejecting the slot; a bad player_ptr
    alone would have hit the old salvage path and produced a real, if
    degraded, local entry) rather than read off the live struct. Worth
    confirming with the same raw-struct tracer used for OFF_LAP_COUNTER
    next time a local-player timeout can be induced."""
    return _validate_slot_relaxed(f, addr, require_status_marker=False)


def _validate_any_slot_relaxed(f, addr: int) -> Optional[dict]:
    """The same relaxed validation, for ANY slot rather than just the local
    player's -- but requiring the engine's own status field to vouch for the
    slot first.

    Why this exists: the lap-time floors don't only drop the local player,
    they drop *every* racer who completed zero laps. Caught live 2026-08-15 --
    an offline race where all 24 cars DNF'd (most at 0 laps) logged as a
    **4-row** result, because only the three racers who had set a lap time
    survived `validate_entry()`. The other 20 were real, named, classified
    racers and simply vanished from the logged race.

    The local-player path (above) can afford to skip a status check because
    CLIENT has already independently named that exact slot. There is no such
    external witness for an arbitrary slot, so this requires a terminal
    marker in the status field -- STATUS_DNF_BIT or STATUS_RUN_COMPLETE_BIT
    -- as proof the engine itself has classified this slot as a real
    participant with a settled result. That is a much stronger
    occupancy proof than name-plus-class-rating alone, and it is what makes
    widening the net safe here: an empty seat never gets a terminal status,
    and stale mid-race slots read 0 or 4 (bit 0x01 flickers mid-race and is
    deliberately NOT accepted as a marker -- only STATUS_DNF_BIT and
    STATUS_RUN_COMPLETE_BIT are trusted).

    Note this admits a player who DNFs *mid-race* as soon as the engine flags
    them, which is correct: they are genuinely out, and their result is
    settled even though the race continues."""
    status = ri32(f, addr + OFF_STATUS_FLAGS)
    if not status or not (status & (STATUS_DNF_BIT | STATUS_RUN_COMPLETE_BIT)):
        return None
    return _validate_slot_relaxed(f, addr, require_status_marker=True)


def _validate_slot_relaxed(f, addr: int, require_status_marker: bool) -> Optional[dict]:
    """Shared core of the two relaxed validators above. Drops validate_entry()'s
    lap-time floors (clamping unset times to 0 instead of rejecting) while
    still requiring real occupancy: a plausible class rating, set at race
    start before any lap completes, and a readable non-garbled name behind
    player_ptr. `require_status_marker` is handled by the callers."""
    total_time   = ri32(f, addr + OFF_TOTAL_TIME)
    best_lap     = ri32(f, addr + OFF_BEST_LAP)
    class_rating = ri32(f, addr + OFF_CLASS_RATING)

    if any(v is None for v in [total_time, best_lap, class_rating]):
        return None
    if not (50 <= class_rating <= 600):
        return None

    ptr = ru64(f, addr + OFF_PLAYER_PTR)
    if not ptr or ptr < MIN_HEAP_PTR or ptr > (1 << 47):
        return None
    name = _strip_color_codes(rcstr(f, ptr + POFF_NAME, 64))
    if not name or _looks_garbled(name):
        return None

    if not (MIN_LAP_MS <= best_lap <= MAX_LAP_MS):
        best_lap = 0
    if not (MIN_LAP_MS <= total_time <= MAX_TOTAL_MS):
        total_time = 0

    return {'addr': addr, 'total_time_ms': total_time, 'best_lap_ms': best_lap, 'class_rating': class_rating}


def _count_still_racing(f, slot0: int, pid: Optional[int]) -> None:
    """Records how many real racers have no terminal status marker yet.

    Walks the raw slot array rather than the validated player list, because a
    racer still circulating without a completed lap appears in neither (see
    _STILL_RACING_COUNT). "Real racer" means an occupied seat: a readable,
    non-garbled name behind player_ptr plus a plausible class rating -- the
    same occupancy proof the relaxed validators use, so an empty seat (which
    also has no terminal marker) can't hold the results screen off forever."""
    if pid is None:
        return
    still = 0
    for i in range(MAX_PLAYERS):
        addr = slot0 + i * SLOT_STRIDE
        st = ri32(f, addr + OFF_STATUS_FLAGS)
        if st is None or (st & (STATUS_RUN_COMPLETE_BIT | STATUS_DNF_BIT)):
            continue
        rating = ri32(f, addr + OFF_CLASS_RATING)
        if rating is None or not (50 <= rating <= 600):
            continue
        ptr = ru64(f, addr + OFF_PLAYER_PTR)
        if not ptr or ptr < MIN_HEAP_PTR or ptr > (1 << 47):
            continue
        name = _strip_color_codes(rcstr(f, ptr + POFF_NAME, 64))
        if name and not _looks_garbled(name):
            still += 1
    _STILL_RACING_COUNT[pid] = still


def _mark_local_player(f, module_base: Optional[int], table_base: Optional[int], pairs: list, slot0: Optional[int], pid: Optional[int] = None) -> None:
    """Marks the local player's entry, resolving their slot index from
    CLIENT's hash-registry lookup.

    The status field is deliberately NOT consulted for identity. `0x40` was
    briefly used as the primary source on 2026-08-15 -- it had matched the
    local player in four offline races -- and one real online race disproved
    it: the bit marks a racer whose RUN IS COMPLETE, not the local player,
    and seven slots carried it at once. See STATUS_RUN_COMPLETE_BIT.

    If the resolved slot is real but wasn't captured in `pairs` at all --
    e.g. its player_ptr is invalid, so read_player() rejected it entirely, or
    it belongs to a racer who completed zero laps, so validate_entry()'s
    lap-time floors rejected it -- salvages a degraded entry from the slot's
    own native fields instead of silently dropping the local player's real
    result. Two tiers: the strict validate_entry() first (unchanged), then
    _validate_local_slot_relaxed() (see its docstring -- that's the 0-lap
    case, and the root cause of the 2026-08-15 misattribution bug).

    Falls back to lowest address ONLY if CLIENT couldn't resolve a slot
    index at all. That fallback is wrong in general -- it can mislabel a
    real networked player as local -- so once CLIENT *has* named a slot,
    its answer is the only one used: if no entry can be built there, this
    marks nobody rather than guessing. Marking nobody is a genuinely
    recoverable state (race_to_dict emits `player: null`, _race_is_final()
    falls back to the all-players check, and the caller's
    confirmed_local_name guard sees no mismatch to trip on); silently
    attributing a stranger's result to the user is not. The previous
    version reached this fallback whenever the salvage failed for any
    reason, which is exactly how a 0-lap local timeout ended up logging a
    different, already-DNF'd racer as "you"."""
    local_player = None
    client_resolved = False
    local_slot = None

    # STATUS_RUN_COMPLETE_BIT (0x40) is deliberately NOT used for identity -- see
    # that constant's docstring. It was briefly made the primary source on
    # 2026-08-15 after it matched the local player in three offline/solo
    # races, and a real 22-player online race immediately disproved it: the
    # bit sat on `TiamaT` (slot 4, who WON the race) while the local player
    # was `Ampp33` at slot 6, and CLIENT -- the mechanism it had been
    # promoted over -- was correct. In every offline race the local player is
    # the only human present, so a "not an AI"/host-ish flag is indistinguishable
    # from "local player"; only a populated online lobby separates them.
    # CLIENT is the identity source, as before.
    if module_base is not None and table_base is not None and slot0 is not None:
        local_slot = _local_player_slot_index(f, table_base, pid)
        if local_slot is not None and not (0 <= local_slot < MAX_PLAYERS):
            local_slot = None

    if slot0 is not None:
        if local_slot is not None:
            client_resolved = True
            for addr, p in pairs:
                if (addr - slot0) // SLOT_STRIDE == local_slot:
                    local_player = p
                    break
            if local_player is None:
                addr = slot0 + local_slot * SLOT_STRIDE
                # The relaxed tier is gated on at least one OTHER racer
                # having already validated this tick. Caught live 2026-08-15
                # (game sitting in a menu, real slot data stale from a
                # previous race): the local player's own slot alone can
                # satisfy both occupancy proofs -- real name, plausible
                # class rating -- while holding tt=0/bl=0 and a leftover
                # odd lap counter, whose parity makes `finished` read True,
                # so _race_is_final() fires and a phantom 1-row "race" gets
                # logged straight out of an idle menu. That was a real
                # regression this fix introduced and this gate closes it.
                # It costs nothing against the bug actually being fixed:
                # misattribution means labelling some OTHER racer as "you",
                # which can only happen when other racers are present --
                # and when `pairs` is empty the lowest-address fallback is
                # already a no-op anyway, so there is nothing to protect
                # against. Deliberate, documented limit: a SOLO race where
                # the local player times out with 0 laps still won't
                # salvage an entry -- there is no second racer to prove a
                # race happened at all, and nothing worth logging there.
                d = validate_entry(f, addr)
                relaxed = False
                if d is None and pairs:
                    d = _validate_local_slot_relaxed(f, addr)
                    relaxed = d is not None
                if d is not None:
                    local_player = _read_player_native_only(f, d)
                    local_player.slot_index = local_slot
                    # A relaxed salvage means this player has NO valid lap
                    # time yet, so they cannot possibly have finished --
                    # force `finished` False regardless of what the flag
                    # bit says. Caught live 2026-08-15 in a 24-player
                    # multiplayer race: without this, a relaxed-salvaged
                    # local player one minute into the race (no lap set
                    # yet, counter=1) carried `finished=True` purely
                    # because counter 1 is ODD, so _race_is_final() fired
                    # and logged a bogus mid-race "result" -- all 24 rows
                    # sharing one identical total_time (the shared running
                    # clock) and the local player with no lap time at all.
                    # This is the parity bit being garbage (see
                    # OFF_FINISHED_FLAG), but the relaxed tier is what
                    # newly exposed the local player to it, so the tier
                    # owns the guard. Costs nothing real: a player with no
                    # completed lap has no finish to report either way.
                    if relaxed:
                        local_player.finished = False
                    pairs.append((addr, local_player))
    if local_player is None:
        if client_resolved or not pairs:
            return
        _, local_player = min(pairs, key=lambda pair: pair[0])
    local_player.is_local = True


def resolve_local_car_name(pid: int, players: list) -> None:
    """Fills in the local player's car name. Call once per new race (not every
    tick) -- the resolution chain costs real time and the result can't change mid-race."""
    local_player = next((p for p in players if p.is_local), None)
    if local_player is None:
        return
    try:
        with _mem_and_bases(pid) as (f, module_base, table_base):
            if not table_base:
                return
            resolved = _local_player_car_name(f, module_base, table_base)
            if resolved:
                local_player.car = resolved
    except _MEM_ERRORS:
        return


# ── car names, live (roster car-name table) ──────────────────────────────────
# Confirmed live 2026-08-15 (two separate races, one online, one offline):
# a small, fixed-stride (0x80-byte) array holds one all-caps, null-terminated
# car name per player slot, in the SAME order as the 24-slot player array
# (table slot N == player-array slot N -- verified by cross-checking against
# the local player's own car name, already independently known via
# _local_player_car_name() above, at the local player's own known slot
# index). This is a materially more reliable source than the old
# per-player-struct read it replaces: caught live, repeatedly, a car whose
# asset is still streaming in reads as outright garbage at the old fixed
# struct offset (a leftover float, an unresolved
# "VEHICLE_NAME_<id>_<variant>" template key) for a real window of time,
# while this table already had the correct, resolved, human-readable name.
# Also confirmed persistent across races within one game session -- same
# base address, overwritten in place each race, not reallocated -- but there
# is no known static pointer chain to it (unlike the main player array), so
# it has to be structurally re-discovered per pid, same as the sentinel-scan
# fallback for the player array itself.
_CAR_NAME_TABLE_STRIDE = 0x80
_CAR_NAME_TOKEN_RE = re.compile(rb'[A-Z][A-Z ]{2,19}\x00')
_CAR_NAME_TABLE_CACHE: dict = {}   # pid -> base address of table slot 0


def _stride_runs(hits: list, stride: int, min_run: int = 2) -> list:
    """All runs of hits spaced exactly `stride` bytes apart, each >= min_run
    long -- like cluster_sentinel_hits, but returns every run instead of
    just the longest one. Car-table discovery needs that: confirmed live
    other same-shape, same-stride tables exist elsewhere in memory (a
    race-mode-name list, a couple of unrelated repeated-tag tables) and can
    legitimately be longer than the real one, so "longest run wins" isn't
    safe here the way it is for the sentinel scan."""
    if len(hits) < min_run:
        return []
    sorted_hits = sorted(hits)
    runs = []
    current = [sorted_hits[0]]
    for addr in sorted_hits[1:]:
        if addr - current[-1] == stride:
            current.append(addr)
        else:
            if len(current) >= min_run:
                runs.append(current)
            current = [addr]
    if len(current) >= min_run:
        runs.append(current)
    return runs


def _find_car_name_table(pid: int, local_slot: int, local_car_name: str) -> Optional[int]:
    """Structural discovery of the car-name table. Expensive -- scans every
    anonymous writable region (multiple seconds) -- callers must cache the
    result; only ever needs to succeed once per pid. A candidate run is only
    trusted once `local_car_name` (already resolved independently and
    reliably by _local_player_car_name(), not through this table or the old
    per-player struct field) shows up at that same run's `local_slot`
    entry -- content match at the one position we can already verify,
    not just structural shape alone."""
    regions = anon_writable_regions(pid)
    hits = []
    CHUNK = 8 * 1024 * 1024
    overlap = 24  # >= longest possible token, so a match split across a chunk boundary is never missed
    try:
        with open(f"/proc/{pid}/mem", "rb") as f:
            for start, end in regions:
                size = end - start
                offset = 0
                try:
                    f.seek(start)
                except OSError:
                    continue
                prev_tail = b""
                while offset < size:
                    n = min(CHUNK, size - offset)
                    try:
                        chunk = f.read(n)
                    except OSError:
                        break
                    if not chunk:
                        break
                    hay = prev_tail + chunk
                    hay_base = start + offset - len(prev_tail)
                    for m in _CAR_NAME_TOKEN_RE.finditer(hay):
                        hits.append(hay_base + m.start())
                    prev_tail = chunk[-overlap:]
                    offset += len(chunk)
    except _PROC_ERRORS:
        return None

    runs = _stride_runs(hits, _CAR_NAME_TABLE_STRIDE)
    if not runs:
        return None

    target = local_car_name.upper()
    try:
        with open(f"/proc/{pid}/mem", "rb") as f:
            for run in runs:
                if local_slot >= len(run):
                    continue
                if rcstr(f, run[local_slot], 32).upper() == target:
                    return run[0]
    except _PROC_ERRORS:
        return None
    return None


def _get_car_name_table(pid: int, local_slot: Optional[int], local_car_name: Optional[str]) -> Optional[int]:
    """Cached lookup -- discovery is expensive, the table's base address
    isn't (confirmed stable across two separate races in the same game
    session)."""
    cached = _CAR_NAME_TABLE_CACHE.get(pid)
    if cached is not None:
        return cached
    if local_slot is None or not local_car_name:
        return None   # can't validate a candidate yet -- caller can retry next race
    base = _find_car_name_table(pid, local_slot, local_car_name)
    if base is not None:
        _CAR_NAME_TABLE_CACHE[pid] = base
    return base


def read_car_names(pid: int, local_slot: Optional[int], local_car_name: Optional[str]) -> dict:
    """slot_index -> car name (Title Case), for every populated slot. Reads
    fresh every call -- cheap once the table's base address is cached, and
    deliberately not cached itself, so a slot whose car is still streaming
    in the first time this runs picks up the real value automatically on a
    later call, no restart needed. NOTE: Title Case is a lossy
    reconstruction of the table's all-caps text -- confirmed live it's
    correct for every car name seen so far except one: "ROADSLAYER" (whose
    real display name has an internal capital, "RoadSlayer") comes back as
    "Roadslayer". Known, narrow, cosmetic-only limitation, not chased
    further since it doesn't affect identity/matching anywhere downstream."""
    base = _get_car_name_table(pid, local_slot, local_car_name)
    if base is None:
        return {}
    result = {}
    try:
        with open(f"/proc/{pid}/mem", "rb") as f:
            for i in range(MAX_PLAYERS):
                val = rcstr(f, base + i * _CAR_NAME_TABLE_STRIDE, 32)
                if val:
                    result[i] = val.title()
    except _PROC_ERRORS:
        pass
    return result


def resolve_car_names(pid: int, players: list) -> None:
    """Fills in every OTHER player's car name (not the local player's -- see
    below) from the roster car-name table. Call once per new race (not
    every tick), and AFTER resolve_local_car_name(): this function reuses
    its result both as the trusted value table discovery validates against,
    and, deliberately, as the local player's own final car name --
    _local_player_car_name() reads the game's own loc-string table directly
    and gets the byte-exact display name (e.g. "RoadSlayer", "KillerBee S"),
    while this table only round-trips through Title Case (see
    read_car_names()'s docstring -- lossy for a name with an internal
    capital). Overwriting the local player's already-correct value with the
    lossy one right before read_tuning_for_race() matches it against
    cars5.ccrs's exact-cased catalog would be a straight regression, so it's
    left alone here."""
    local_player = next((p for p in players if p.is_local), None)
    local_car_name = local_player.car if local_player else None
    local_slot = local_player.slot_index if local_player else None
    if _CAR_NAME_TABLE_CACHE.get(pid) is None:
        print(f"[{_ts()}] Locating car-name table (one-time this session, ~10s)...")
    car_names = read_car_names(pid, local_slot, local_car_name)
    for p in players:
        if p.is_local:
            continue
        if p.slot_index is not None and p.slot_index in car_names:
            p.car = car_names[p.slot_index]


def _position_sort_key(p: PlayerResult) -> tuple:
    """Ranks by laps_completed first (descending -- more laps completed
    always outranks fewer), total_time_ms as the tiebreak within an equal
    lap count. Plain total_time_ms alone isn't safe to sort the whole field
    by: confirmed live 2026-08-08 a still-racing/DNF'd straggler's
    total_time_ms can sit well BELOW the genuinely-finished pack's -- and
    confirmed again live 2026-08-15, this time root-caused: OFF_FINISHED_FLAG
    (the old `finished`-based first sort key) was never a real "did they
    finish" signal, just the parity of the same lap counter this function
    now sorts on directly (see OFF_LAP_COUNTER's docstring) -- it flips on
    and off roughly once a lap for every player, racing or not, so a DNF's
    frozen total_time_ms could get sorted above real finishers depending on
    which parity their counter happened to freeze on. laps_completed
    doesn't have that problem: verified live against a real race with 4
    DNFs, the 2 genuine finishers both froze at the race's real lap count
    (4) while every DNF froze at 3 or lower, cleanly separating them
    regardless of raw elapsed time. (No known way yet to compare a DNF's
    progress *within* an equal lap count more precisely than total_time_ms
    -- confirmed live this doesn't perfectly reproduce the real relative
    order of multiple same-lap-count DNFs, so treat sub-ordering among tied
    DNFs as best-effort, not verified, unlike the finished-vs-DNF split
    itself.)

    As of 2026-08-15 the finisher/DNF split no longer has to be INFERRED from
    the lap counter at all: STATUS_DNF_BIT is the engine's own DNF flag (see
    OFF_STATUS_FLAGS), verified across a 24-player online race where exactly
    the 6 reported DNFs carried it and an offline race where all 24 did and
    nobody finished. Sorting on it first makes every real finisher outrank
    every DNF outright, which the lap counter only achieved indirectly (and
    couldn't achieve at all for a DNF who died on the final lap with the same
    counter value as a finisher). The lap counter stays as the next key, so
    ordering among DNFs is unchanged and behaviour is identical whenever the
    status field isn't populated (`dnf` reads False for everyone, and the key
    degrades exactly to the previous one)."""
    return (p.dnf, -p.laps_completed, p.total_time_ms)


def _rank_players(players: list) -> list:
    """Sorts a field into finishing order, preferring the engine's own
    position byte (OFF_FINISH_POSITION) over any heuristic.

    That byte is exact -- verified live against a full 24-car race's real
    results screen, finishers and DNFs alike -- so when it looks coherent it
    is used directly and nothing is inferred. "Coherent" means every player
    has a non-zero value and no two share one: a partially-populated or
    duplicated set (mid-race, stale slots, a mode that doesn't fill it)
    would otherwise silently produce a garbage order, and a wrong order that
    looks authoritative is worse than an approximate one that is honest
    about being approximate.

    Falls back to _position_sort_key()'s heuristic (DNF flag, then lap
    count, then total time) whenever that check fails, which keeps the
    previous behaviour exactly for any case the byte doesn't cover."""
    positions = [p.finish_position for p in players]
    usable = all(v > 0 for v in positions) and len(set(positions)) == len(positions)
    if usable:
        return sorted(players, key=lambda p: p.finish_position)
    return sorted(players, key=_position_sort_key)


def scrape_players(pid: int, cached_addrs: Optional[list]) -> tuple:
    """
    Returns (players, used_addrs), or (None, None/addrs) if nothing valid.
    used_addrs is the 24-slot array's addresses -- pass back next call to skip
    the expensive scan. The array is a fixed, per-race-rewritten allocation, so
    it stays valid for the process's lifetime; zero valid players from cached
    addrs is normal (mid-race/menu) and doesn't trigger a rescan, only a hard
    read failure does.
    """
    if cached_addrs:
        try:
            with _mem_and_bases(pid) as (f, module_base, table_base):
                slot0 = min(cached_addrs)
                cached_pairs = []
                seen_names: set = set()
                for addr in cached_addrs:
                    # Second chance for a real racer the lap-time floors
                    # reject -- anyone who DNF'd having completed 0 laps.
                    # See _validate_any_slot_relaxed().
                    d = validate_entry(f, addr) or _validate_any_slot_relaxed(f, addr)
                    if d is None:
                        continue
                    p = read_player(f, d, module_base, table_base)
                    if p and p.name not in seen_names:
                        seen_names.add(p.name)
                        p.slot_index = (addr - slot0) // SLOT_STRIDE
                        cached_pairs.append((addr, p))
                # Always attempt local-player resolution, even if nothing
                # else currently validates -- a solo/AI race where the local
                # player's own slot has a bad player_ptr this tick would
                # otherwise never get the salvage path a chance to run.
                _mark_local_player(f, module_base, table_base, cached_pairs, slot0, pid)
                _count_still_racing(f, slot0, pid)
                if cached_pairs:
                    cached_players = _rank_players([p for _, p in cached_pairs])
                    for i, p in enumerate(cached_players):
                        p.position = i + 1
                    return cached_players, cached_addrs
                return None, cached_addrs
        except _PROC_ERRORS:
            return None, None

    hits = fast_find_slots(pid)
    if hits is None:
        regions = anon_writable_regions(pid)
        if not regions:
            return None, None
        hits = scan_for_sentinels(pid, regions)
        if not hits:
            return None, None
        hits = cluster_sentinel_hits(hits)

    pairs = []
    seen_names: set = set()
    try:
        with _mem_and_bases(pid) as (f, module_base, table_base):
            slot0 = min(hits)
            for addr in hits:
                # Second chance for a real racer the lap-time floors reject
                # -- see the identical call in the cached branch above.
                d = validate_entry(f, addr) or _validate_any_slot_relaxed(f, addr)
                if d is None:
                    continue
                p = read_player(f, d, module_base, table_base)
                if p is None or p.name in seen_names:
                    continue
                seen_names.add(p.name)
                p.slot_index = (addr - slot0) // SLOT_STRIDE
                pairs.append((addr, p))

            # Always attempt local-player resolution, even if nothing else
            # currently validates -- see the identical comment in the cached
            # branch above.
            _mark_local_player(f, module_base, table_base, pairs, slot0, pid)
            _count_still_racing(f, slot0, pid)
            if not pairs:
                return None, None
    except _PROC_ERRORS:
        return None, None

    players_raw = _rank_players([p for _, p in pairs])
    for i, p in enumerate(players_raw):
        p.position = i + 1
    # Cache the FULL candidate slot list (`hits`), not just whichever ones
    # happened to validate on this exact scan (`[addr for addr, _ in pairs]`,
    # the previous behavior). Both discovery paths (pointer chain, sentinel
    # scan) find slots by structural existence, independent of whether that
    # player's stats are populated *yet* -- a slot that isn't valid this tick
    # (e.g. hasn't posted a first lap time -- entirely normal if discovery
    # runs while a race is still in progress, which is the tool's whole
    # intended use) is still a real slot. The cached-path branch above
    # already tolerates some cached addresses being momentarily invalid (it
    # just skips them that tick, cached_addrs is returned unchanged either
    # way) -- caching only the validated subset meant any player not yet
    # valid at discovery time was silently, permanently dropped for the rest
    # of the game session, since the cached path never re-scans. Confirmed
    # live 2026-08-05: a discovery scan mid-race caught only 1 of 6 racers
    # as currently valid, and the other 5 were never looked at again even
    # once they became fully valid later in the same race.
    return players_raw, hits


# ── engine string hash (MurmurHash2 variant) + name registry ─────────────────
_HASH_MASK32 = 0xFFFFFFFF
_HASH_MUL    = 0x5bd1e995


def wf_hash(data: bytes, seed: int = 0) -> int:
    length = len(data)
    h = (length ^ seed) & _HASH_MASK32
    pos = 0
    n4 = length // 4
    rem = length % 4
    for _ in range(n4):
        k = struct.unpack_from('<i', data, pos)[0] & _HASH_MASK32
        pos += 4
        km = (k * _HASH_MUL) & _HASH_MASK32
        mixed = (((km >> 0x18) ^ km) * _HASH_MUL) & _HASH_MASK32
        h = ((h * _HASH_MUL) & _HASH_MASK32) ^ mixed
    if rem == 3:
        h ^= (data[pos + 2] << 0x10)
        h ^= (data[pos + 1] << 8)
        h = ((data[pos] ^ h) * _HASH_MUL) & _HASH_MASK32
    elif rem == 2:
        h ^= (data[pos + 1] << 8)
        h = ((data[pos] ^ h) * _HASH_MUL) & _HASH_MASK32
    elif rem == 1:
        h = ((data[pos] ^ h) * _HASH_MUL) & _HASH_MASK32
    h2 = (((h >> 0xd) ^ h) * _HASH_MUL) & _HASH_MASK32
    return (h2 >> 0xf) ^ h2


# Global name -> object hash registry (every engine "system", e.g. event_settings, CLIENT).
PTR_DAT_OFFSET = 0x127e7f8    # module_base + this -> ptr to registry table_base
BUCKET_ARR_OFF = 0x306020     # table_base + this + bucket*8 -> head node ptr
NAME_ARR_OFF   = 0x40605c     # table_base + this + idx*STRIDE -> registered name cstr
OBJ_ARR_OFF    = 0x406040     # table_base + this + idx*STRIDE -> object ptr
REGISTRY_STRIDE = 0x138

EVENT_SETTINGS_TRACK_FIELD_OFF      = 0xb0   # -> "<track>_<variation>" cstr
EVENT_SETTINGS_BASE_TRACK_FIELD_OFF = 0xa0   # -> "<track>" cstr (no variation)

_table_base_cache: dict = {}


def _get_table_base(f, pid: int, module_base: Optional[int]) -> Optional[int]:
    cached = _table_base_cache.get(pid)
    if cached is not None:
        return cached
    if module_base is None:
        return None
    table_base = ru64(f, module_base + PTR_DAT_OFFSET)
    if table_base:
        _table_base_cache[pid] = table_base
    return table_base


@contextmanager
def _mem_and_bases(pid: int):
    """Shared boilerplate: opens /proc/pid/mem and resolves (module_base,
    table_base) once. Yields (f, module_base, table_base) -- the latter two
    may be None if unresolved, callers check before use."""
    with open(f"/proc/{pid}/mem", "rb") as f:
        module_base = find_module_base(pid)
        table_base = _get_table_base(f, pid, module_base)
        yield f, module_base, table_base


def _hash_registry_lookup(f, table_base: int, name: str) -> Optional[int]:
    h = wf_hash(name.encode())
    bucket = h & 0x1FFFF
    node = ru64(f, table_base + BUCKET_ARR_OFF + bucket * 8)
    maxlen = len(name) + 4
    seen = 0
    while node and seen < 64:
        idx = ri32(f, node)
        if idx is None:
            return None
        cand = rcstr(f, table_base + NAME_ARR_OFF + idx * REGISTRY_STRIDE, maxlen)
        if cand == name:
            return ru64(f, table_base + OBJ_ARR_OFF + idx * REGISTRY_STRIDE)
        node = ru64(f, node + 8)
        seen += 1
    return None


# ── localization/template string resolver ────────────────────────────────────
LOC_HASH_TABLE_PTR_OFF = 0xb2e9210   # module_base + this -> ptr to loc-string hash table
LOC_SYS_IDX_OFF        = 0xb2e91f4   # module_base + this -> int32, this system's registry idx


def _resolve_localized_string(f, module_base: int, table_base: int, key: str) -> Optional[str]:
    bucket_table_ptr = ru64(f, module_base + LOC_HASH_TABLE_PTR_OFF)
    if not bucket_table_ptr:
        return None
    bucket_array_base = ru64(f, bucket_table_ptr)
    bucket_count = ru64(f, bucket_table_ptr + 8)
    if not bucket_array_base or not bucket_count:
        return None

    h = wf_hash(key.encode())
    bucket = h % bucket_count
    node = ru64(f, bucket_array_base + bucket * 8)
    maxlen = len(key) + 4
    seen = 0
    idx = None
    while node and seen < 64:
        key_ptr = ru64(f, node + 8)
        cand = rcstr(f, key_ptr, maxlen) if key_ptr else ""
        if cand == key:
            idx = ri32(f, node + 0x10)
            break
        node = ru64(f, node)
        seen += 1
    if idx is None or idx < 0:
        return None

    loc_sys_idx = ri32(f, module_base + LOC_SYS_IDX_OFF)
    if loc_sys_idx is None:
        return None
    loc_data_ptr = ru64(f, table_base + OBJ_ARR_OFF + loc_sys_idx * REGISTRY_STRIDE)
    if not loc_data_ptr:
        return None
    loc_data_array = ru64(f, loc_data_ptr)
    if not loc_data_array:
        return None
    entry_ptr = loc_data_array + idx * 0x60

    seg_count = ri32(f, entry_ptr + 0x18)
    seg_array_base = ru64(f, entry_ptr + 0x10)
    if not seg_count or seg_count < 1 or not seg_array_base:
        return None
    text_ptr = ru64(f, seg_array_base + 8)   # segment 0: plain, non-parameterized string
    return rcstr(f, text_ptr, 64) if text_ptr else None


# ── local player's own car name (career-save -> garage -> vehicle-id chain) ──
CAREER_RESOURCE_NAME = "save/career.cres"


def _local_player_car_name(f, module_base: int, table_base: int) -> Optional[str]:
    career_obj = _hash_registry_lookup(f, table_base, CAREER_RESOURCE_NAME)
    if not career_obj:
        return None
    garage_idx = ri32(f, career_obj + 0x1c)
    vehicle_id = ri32(f, career_obj + 0x180)
    if garage_idx is None or vehicle_id is None or vehicle_id < 0:
        return None
    garage_obj = ru64(f, table_base + OBJ_ARR_OFF + garage_idx * REGISTRY_STRIDE)
    if not garage_obj:
        return None
    vehicles_base = ru64(f, garage_obj)
    if not vehicles_base:
        return None
    car_def = vehicles_base + vehicle_id * 0x90
    view_obj = ru64(f, car_def)
    if not view_obj:
        return None
    key_ptr = ru64(f, view_obj + 8)
    if not key_ptr:
        return None
    key = rcstr(f, key_ptr, 64)
    if not key:
        return None
    return _resolve_localized_string(f, module_base, table_base, key)


# ── track/variation name resolution ──────────────────────────────────────────
def _snake(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')


_TRACK_SNAKE      = {_snake(t): t for t in KNOWN_TRACKS}
_VAR_SNAKE        = {_snake(v): v for v in KNOWN_VARIATIONS}
_TRACK_SNAKE_KEYS = sorted(_TRACK_SNAKE, key=len, reverse=True)

# Some event_settings codenames are opaque internal names bearing no
# resemblance to the display name -- ground-truth mappings, add as found.
KNOWN_CODENAMES = {
    "crm02_1": ("Devil's Canyon", "Race Track"),
    "dirt_speedway_dirt_oval": ("Bloomfield Speedway", "Dirt Oval"),
}


def _split_track_variation(raw: str) -> tuple:
    if raw in KNOWN_CODENAMES:
        return KNOWN_CODENAMES[raw]
    for snake_track in _TRACK_SNAKE_KEYS:
        if raw == snake_track:
            return _TRACK_SNAKE[snake_track], ""
        prefix = snake_track + "_"
        if raw.startswith(prefix):
            variation = _VAR_SNAKE.get(raw[len(prefix):], "")
            return _TRACK_SNAKE[snake_track], variation
    return "", ""


ENVIRONMENT_SYS_IDX_OFF = 0x18fbd08   # module_base + this -> int32 registry idx for track/environment system


def _resolve_environment_object(f, module_base: int, table_base: int, base_codename: str) -> Optional[int]:
    """Resolves a raw track codename (e.g. "dirt_speedway") to its live "environment" object."""
    sys_idx = ri32(f, module_base + ENVIRONMENT_SYS_IDX_OFF)
    if sys_idx is None:
        return None
    category_obj = ru64(f, table_base + OBJ_ARR_OFF + sys_idx * REGISTRY_STRIDE)
    if not category_obj:
        return None
    count = ri32(f, category_obj + 8)
    array_base = ru64(f, category_obj)
    if not count or count <= 0 or not array_base:
        return None
    h = wf_hash(base_codename.encode())
    for i in range(count):
        idx = ri32(f, array_base + i * 0x20 + 8)
        if idx is None:
            continue
        candidate_obj = ru64(f, table_base + OBJ_ARR_OFF + idx * REGISTRY_STRIDE)
        if not candidate_obj:
            continue
        stored_hash = ri32(f, candidate_obj + 0x78)
        if stored_hash is None or (stored_hash & 0xFFFFFFFF) != h:
            continue
        str_ptr = ru64(f, candidate_obj + 0x70)
        if str_ptr and rcstr(f, str_ptr, 64) == base_codename:
            return candidate_obj
    return None


def _resolve_environment_display_name(f, env_obj: int) -> Optional[str]:
    """object + 0x8 -> + 0x18 -> display text (track name and variation name alike)."""
    sub = ru64(f, env_obj + 8)
    if not sub:
        return None
    name_ptr = ru64(f, sub + 0x18)
    return rcstr(f, name_ptr, 64) if name_ptr else None


def detect_track_and_variation(pid: int) -> tuple:
    """Reads the live event_settings object via the engine's name registry.
    Falls back to snake-case-splitting the raw codename if the display-name
    chain doesn't resolve, and to ("Unknown Track", "") if nothing resolves at all."""
    try:
        with _mem_and_bases(pid) as (f, module_base, table_base):
            if not table_base:
                return "Unknown Track", ""

            obj_ptr = _hash_registry_lookup(f, table_base, "event_settings")
            if obj_ptr:
                base_ptr = ru64(f, obj_ptr + EVENT_SETTINGS_BASE_TRACK_FIELD_OFF)
                base_codename = rcstr(f, base_ptr, 64) if base_ptr else ""

                if base_codename:
                    variation = _resolve_environment_display_name(f, obj_ptr) or ""
                    env_obj = _resolve_environment_object(f, module_base, table_base, base_codename)
                    track = _resolve_environment_display_name(f, env_obj) if env_obj else None
                    if track:
                        return track, variation

            if not obj_ptr:
                return "Unknown Track", ""
            name_ptr = ru64(f, obj_ptr + EVENT_SETTINGS_TRACK_FIELD_OFF)
            if not name_ptr:
                return "Unknown Track", ""
            raw = rcstr(f, name_ptr, 64)
    except _MEM_ERRORS:
        return "Unknown Track", ""
    if not raw:
        return "Unknown Track", ""
    track, variation = _split_track_variation(raw)
    if track:
        return track, variation
    return f"Unknown track (codename: {raw!r})", ""


# ── car tuning (pre-race setup screen) ───────────────────────────────────────
# Tuning sliders (0-4 index) resolve via a synthetic "menu/element/<hash>" name
# (FNV-1a of e.g. "TUNE_SLIDER_SUSPENSION_TRACK") through the same registry
# used above; current value lives on the widget's "_TRACK" child at +0x320 as
# a normalized float (index/4). A widget only exists once its tab has been
# visited this menu session.
TUNE_CATEGORIES      = ["SUSPENSION", "GEARING", "DIFFERENTIAL", "BRAKES"]
TUNE_TRACK_VALUE_OFF = 0x320
TUNE_MAX_INDEX       = 4

_FNV_OFFSET_BASIS = 0x811c9dc5
_FNV_PRIME        = 0x1000193


def fnv1a(data: bytes) -> int:
    h = _FNV_OFFSET_BASIS
    for b in data:
        h = ((h ^ b) * _FNV_PRIME) & 0xFFFFFFFF
    return h


def _tune_slider_track_ptr(f, table_base: int, category: str) -> Optional[int]:
    name = f"TUNE_SLIDER_{category}_TRACK"
    elem_name = f"menu/element/{fnv1a(name.encode())}"
    return _hash_registry_lookup(f, table_base, elem_name)


# DIFFERENTIAL never gets a menu/element registry entry -- all four categories'
# current index instead live together in one heap array of four 0x50-byte
# structs (magic 0x00090005 @+0x0, sequential id @+0x4, self-ref ptr @+0x10,
# 0-4 index @+0x18), relocated each session via structural signature since it
# has no discoverable stable pointer.
_TUNE_ARR_MAGIC   = 0x00090005
_TUNE_ARR_STRIDE  = 0x50
_TUNE_ARR_VAL_OFF = 0x18
_TUNE_ARR_LEN     = len(TUNE_CATEGORIES)

_tune_array_cache: dict = {}


def _tune_arr_struct_id(f, addr: int) -> Optional[int]:
    if ri32(f, addr) != _TUNE_ARR_MAGIC:
        return None
    return ri32(f, addr + 4)


def _tune_arr_values(f, base: int) -> Optional[list]:
    vals = [ri32(f, base + k * _TUNE_ARR_STRIDE + _TUNE_ARR_VAL_OFF) for k in range(_TUNE_ARR_LEN)]
    return vals if all(isinstance(v, int) for v in vals) else None


def _region_containing(pid: int, addr: int) -> Optional[tuple]:
    for start, end in _writable_regions(pid):
        if start <= addr < end:
            return (start, end)
    return None


def _find_tune_array(f, pid: int, anchor_addr: Optional[int] = None) -> Optional[int]:
    """Scans only the writable region containing anchor_addr (an already-
    resolved tuning widget) -- a whole-memory scan risks matching an unrelated
    engine array that happens to share the same magic."""
    if anchor_addr is None:
        return None
    region = _region_containing(pid, anchor_addr)
    regions = [region] if region else []
    CHUNK = 4 * 1024 * 1024
    for start, end in regions:
        remaining = end - start
        offset = start
        while remaining > 0:
            n = min(CHUNK, remaining)
            f.seek(offset)
            buf = f.read(n)
            if not buf:
                break
            usable = len(buf) - (len(buf) % 4)
            arr = array.array('i')
            arr.frombytes(buf[:usable])
            for i, val in enumerate(arr):
                if val != _TUNE_ARR_MAGIC:
                    continue
                base = offset + i * 4
                ids = []
                ok = True
                for k in range(_TUNE_ARR_LEN):
                    sid = _tune_arr_struct_id(f, base + k * _TUNE_ARR_STRIDE)
                    if sid is None:
                        ok = False
                        break
                    ids.append(sid)
                if not ok or not all(ids[j + 1] == ids[j] + 1 for j in range(len(ids) - 1)):
                    continue
                vals = _tune_arr_values(f, base)
                if not vals or not all(0 <= v <= TUNE_MAX_INDEX for v in vals):
                    continue
                before_id = _tune_arr_struct_id(f, base - _TUNE_ARR_STRIDE)
                after_id = _tune_arr_struct_id(f, base + _TUNE_ARR_LEN * _TUNE_ARR_STRIDE)
                if before_id is not None and before_id == ids[0] - 1:
                    continue
                if after_id is not None and after_id == ids[-1] + 1:
                    continue
                return base
            offset += len(buf)
            remaining -= len(buf)
    return None


def _read_differential(f, pid: int, anchor_addr: Optional[int]) -> Optional[int]:
    base = _tune_array_cache.get(pid)
    if base is not None and _tune_arr_struct_id(f, base) is None:
        base = None
    if base is None:
        base = _find_tune_array(f, pid, anchor_addr)
        if base is None:
            return None
        _tune_array_cache[pid] = base
    idx = TUNE_CATEGORIES.index("DIFFERENTIAL")
    return ri32(f, base + idx * _TUNE_ARR_STRIDE + _TUNE_ARR_VAL_OFF)


def _read_tuning_widgets(pid: int) -> dict:
    """Current 0-4 index per tuning category, read off the Tune-screen slider
    widgets; a not-yet-visited category is omitted, not guessed. This is the
    PRIMARY live tuning source again as of 2026-08-14 -- verified live in a
    real online race against on-screen ground truth for all 4 categories
    (including GEARING, which an earlier session had believed this couldn't
    track) and confirmed to follow a real live change correctly. The
    2026-08-06 "loadout array" source that briefly superseded this was found
    the same day to be unreliable, and has since been deleted outright (see
    PROJECT.md 2026-08-14 for the demotion and 2026-08-16 for its removal as
    dead code). This is the sole live source."""
    result = {}
    try:
        with _mem_and_bases(pid) as (f, _, table_base):
            if not table_base:
                return result
            # DIFFERENTIAL has no registry entry of its own (see _find_tune_array's
            # docstring) -- its value is located by scanning the heap region
            # around an already-resolved slider widget instead. Any of the other
            # three works equally well as that anchor (same arena), so try them
            # in order rather than hardcoding SUSPENSION specifically -- a
            # session where SUSPENSION's own tab was never visited would
            # otherwise permanently block DIFFERENTIAL too, even if GEARING or
            # BRAKES (and DIFFERENTIAL itself) were.
            track_ptrs = {c: _tune_slider_track_ptr(f, table_base, c)
                          for c in TUNE_CATEGORIES if c != "DIFFERENTIAL"}
            anchor_addr = next((p for p in track_ptrs.values() if p), None)
            for category in TUNE_CATEGORIES:
                if category == "DIFFERENTIAL":
                    raw_idx = _read_differential(f, pid, anchor_addr)
                    if raw_idx is not None:
                        result[category] = raw_idx
                    continue
                track_ptr = track_ptrs[category]
                if not track_ptr:
                    continue
                raw = ri32(f, track_ptr + TUNE_TRACK_VALUE_OFF)
                if raw is None:
                    continue
                frac = struct.unpack('<f', struct.pack('<i', raw))[0]
                result[category] = round(frac * TUNE_MAX_INDEX)
    except _MEM_ERRORS:
        return result
    return result


def read_tuning(pid: int) -> dict:
    """Current 0-4 index per tuning category, live. Reads the Tune-screen
    slider widgets (_read_tuning_widgets) -- the verified-correct live
    source as of 2026-08-14 (see that function's docstring). A category
    whose tab hasn't been visited this menu session is simply omitted, not
    guessed -- there is no reliable live source for that case (the
    equipped-part loadout array once used to cover this gap was found the
    same day to sometimes return a *wrong* value, not just a missing one,
    which is worse than omitting it; that code has since been removed as
    dead -- see PROJECT.md 2026-08-14 and 2026-08-16)."""
    return _read_tuning_widgets(pid)


def watch_tuning(pid: int, interval: float = 0.5):
    print("Watching pre-race tuning screen — press Ctrl+C to stop")
    print("(values appear as each tab is visited; 0-4 index, 0=lowest/shortest)\n")
    last: dict = {}
    try:
        while True:
            current = read_tuning(pid)
            for category in TUNE_CATEGORIES:
                if category in current and current.get(category) != last.get(category):
                    print(f"[{_ts()}] {category}: {current[category]}")
            last = current
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")


# ── car tuning, persisted (cars5.ccrs save file) ─────────────────────────────
# Reads a car's *persisted* tuning straight off disk -- no live game process,
# no memory reads, no Tune-screen visit required this session at all. This
# replaces read_tuning()'s role for the main polling pipeline (below);
# read_tuning()/watch_tuning() above are kept only for --watch-tuning, which
# is a genuinely different, still-useful thing (live slider-drag feedback
# on the Tune screen -- cars5.ccrs only updates when you back out).
#
# Found via a from-scratch reverse-engineering session (2026-08-05, see
# wf-memory-tool/PROJECT.md parts 7-12): the file is a small header plus a
# few LZ4-compressed chunks chained as one rolling stream (LZ4_compress_HC_
# continue -- the 2nd+ chunk's back-references reach into the *previous*
# chunk's own decompressed output, not just their own), which decompress to
# a plain, human-readable catalog: one block per owned car (keyed by an
# internal vehicle codename, e.g. "supervan"), each listing that car's
# current gearbox/suspension/brakes/transmission part as a literal path
# string (e.g. "data/vehicle/supervan/part/gearbox/short.vege"). Cross-
# validated live: all 4 categories x all 5 slider positions (20 points),
# every one an exact match.
CARS5_PATH_GLOB = os.path.expanduser(
    "~/.local/share/Steam/userdata/*/228380/local/wreckfest/cars5.ccrs"
)

# slot's part-path key -> (TUNE_CATEGORIES label, [preset name for index 0..4])
CARS5_TUNE_PRESETS = {
    "gearbox":      ("GEARING",      ["eshort", "short", "std", "wide", "ewide"]),
    "transmission": ("DIFFERENTIAL", ["open", "soft", "limited", "stiff", "locked"]),
    "suspension":   ("SUSPENSION",   ["soft", "msoft", "standard", "mhard", "hard"]),
    "brakes":       ("BRAKES",       ["rear", "mrear", "stock", "mfront", "front"]),
}

_CARS5_PART_PATH_RE = re.compile(
    rb"data/vehicle/([a-zA-Z0-9_]+)/part/(gearbox|transmission|suspension|brakes)/([a-zA-Z]+)\.ve"
)
# Each car's record starts with a "VEHICLE_NAME_<id>_<n>" template-key string,
# immediately followed by two length-prefixed fields (int32 len + bytes,
# twice): the human display name, then "<codename>:default...".
_CARS5_VEHICLE_NAME_RE = re.compile(rb"VEHICLE_NAME_\d+_\d+")


def _lz4_decompress_block(data: bytes, max_output: int = 1 << 22, history: bytes = b"") -> bytes:
    """Minimal pure-Python LZ4 *raw block* decompressor (no frame header, no
    dependency -- `pip install lz4` isn't available in every deployment
    environment, and this format doesn't need the real library's extra
    features). `history` is prior decompressed output this block's back-
    references may also reach into (see module docstring)."""
    out = bytearray(history)
    hist_len = len(history)
    i = 0
    n = len(data)
    while i < n:
        token = data[i]
        i += 1
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                b = data[i]
                i += 1
                lit_len += b
                if b != 255:
                    break
        out += data[i:i + lit_len]
        i += lit_len
        if i >= n:
            break  # final sequence has no match part
        offset = data[i] | (data[i + 1] << 8)
        i += 2
        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while True:
                b = data[i]
                i += 1
                match_len += b
                if b != 255:
                    break
        start = len(out) - offset
        if start < 0:
            raise ValueError(f"bad LZ4 offset {offset} at output len {len(out)}")
        for k in range(match_len):
            out.append(out[start + k])
        if len(out) - hist_len > max_output:
            raise ValueError("LZ4 output too large, probably desynced")
    return bytes(out[hist_len:])


def _find_cars5_path() -> Optional[str]:
    matches = glob.glob(CARS5_PATH_GLOB)
    if not matches:
        return None
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0]


def _decompress_cars5_chunks(buf: bytes) -> list:
    """Parses the 20-byte header + chained-LZ4 chunk structure, fully and
    exactly -- every chunk after the first is itself preceded by an 8-byte
    mini-header whose first 4 bytes are that chunk's exact compressed
    length (second 4 bytes: a per-chunk checksum, not needed to decompress).
    Found live 2026-08-05 by comparing an exact GDB WriteFile capture's known
    chunk boundaries against the mini-header bytes sitting between them in
    the file -- previously approximated with a "try a few small skip
    offsets" heuristic that turned out to only reliably reach 2 of the
    file's 3 real chunks (see PROJECT.md part 8's original discovery and
    part 17's correction) -- confirmed live that a whole 3rd chunk was being
    missed/mis-decoded that way, silently hiding real owned cars (a
    "<model> RS" race-spec variant sitting in chunk 3 as its own distinct
    car, separate from the base "<model>" in chunks 1-2, for example)."""
    if len(buf) < 20 or buf[4:8] != b"srcc":
        raise ValueError("doesn't look like a cars5.ccrs file (bad header)")
    field_a = struct.unpack_from("<I", buf, 12)[0]
    off = 20
    chunk = _lz4_decompress_block(buf[off:off + field_a])
    chunks = [chunk]
    off += field_a
    history = chunk

    while off + 8 <= len(buf):
        chunk_len = struct.unpack_from("<I", buf, off)[0]
        off += 8  # 4-byte length + 4-byte checksum
        if chunk_len <= 0 or off + chunk_len > len(buf):
            break
        chunk = _lz4_decompress_block(buf[off:off + chunk_len], history=history)
        chunks.append(chunk)
        off += chunk_len
        history += chunk

    return chunks


def _extract_cars5_tuning(chunks: list) -> dict:
    """codename -> {part-path key: preset name}"""
    cars: dict = {}
    for chunk in chunks:
        for m in _CARS5_PART_PATH_RE.finditer(chunk):
            codename, key, preset = m.group(1).decode(), m.group(2).decode(), m.group(3).decode()
            cars.setdefault(codename, {})[key] = preset
    return cars


def _extract_cars5_display_names(chunks: list) -> dict:
    """codename -> human display name (e.g. "supervan" -> "Supervan")."""
    names: dict = {}
    for chunk in chunks:
        for m in _CARS5_VEHICLE_NAME_RE.finditer(chunk):
            pos = m.end()
            try:
                name_len = struct.unpack_from("<I", chunk, pos)[0]
                if not (0 < name_len < 64):
                    continue
                pos += 4
                display_name = chunk[pos:pos + name_len].decode("utf-8", errors="replace")
                pos += name_len
                code_len = struct.unpack_from("<I", chunk, pos)[0]
                if not (0 < code_len < 64):
                    continue
                pos += 4
                codename = chunk[pos:pos + code_len].decode("utf-8", errors="replace").split(":")[0]
                names[codename] = display_name
            except (struct.error, IndexError):
                continue
    return names


def _match_cars5_codename(car_name: str, display_names: dict) -> Optional[str]:
    """Matches a live-memory car name against cars5.ccrs's catalog display
    names. Exact match only, deliberately -- a same-day earlier version of
    this function added a prefix-match fallback (in either direction) after
    "Hammerhead RS" appeared to only match the catalog's "Hammerhead" entry
    approximately. That was the wrong fix: it was actually compensating for
    _decompress_cars5_chunks() silently missing/mis-decoding the file's 3rd
    real chunk (now fixed -- see that function's docstring), which is where
    the *real*, separate "Hammerhead RS" entry (`16_race_car`, a distinct
    owned car from `16_european`'s "Hammerhead", with genuinely different
    tuning) actually lives. A prefix match is actively dangerous here, not
    just imprecise -- confirmed live it would have silently returned a
    *different real car's* tuning data (verified: "Hammerhead" and
    "Hammerhead RS" have different GEARING/SUSPENSION values). Once chunk
    parsing is complete and correct, every live-memory car name has always
    matched its catalog entry exactly in testing -- a missing exact match
    should fail quiet (see read_tuning_from_save's contract), not guess."""
    return next((c for c, n in display_names.items() if n == car_name), None)



def read_tuning_from_save(car_name: str) -> dict:
    """Current 0-4 index per tuning category for the owned car whose display
    name matches `car_name` exactly (see _match_cars5_codename), read
    straight from cars5.ccrs on disk -- persisted, so this works whether or
    not the Tune screen was ever visited this session. Returns {} if the
    file can't be found/parsed or no car matches (same fail-quiet contract
    as read_tuning())."""
    result: dict = {}
    if not car_name:
        return result
    try:
        path = _find_cars5_path()
        if not path:
            return result
        buf = open(path, "rb").read()
        chunks = _decompress_cars5_chunks(buf)
        cars = _extract_cars5_tuning(chunks)
        display_names = _extract_cars5_display_names(chunks)
        codename = _match_cars5_codename(car_name, display_names)
        if codename is None or codename not in cars:
            return result
        parts = cars[codename]
        for key, (label, presets) in CARS5_TUNE_PRESETS.items():
            preset = parts.get(key)
            if preset in presets:
                result[label] = presets.index(preset)
    except (OSError, ValueError):
        return result
    return result


def read_tuning_for_race(pid: int, car_name: str) -> dict:
    """Current 0-4 index per tuning category to attach to a just-finished
    race. Prefers the LIVE Tune-screen slider-widget read (_read_tuning_
    widgets -- see that function's docstring; verified live 2026-08-14
    against on-screen ground truth) over the persisted save file, falling
    back to read_tuning_from_save() only for categories the live read
    doesn't have (i.e. a tab that hasn't been visited this menu session).

    Confirmed live 2026-08-08: the previous behavior (read_tuning_from_save()
    alone, unconditionally, per part 13 in PROJECT.md) can log stale tuning
    when a race is raced and finishes *before* the player has backed out of
    the Tune screen since their last change -- cars5.ccrs only gets rewritten
    on backing out, not on every slider drag, so a race run against a
    just-changed-but-not-yet-saved setting gets logged with the old value.
    Caught directly in the user's own race_log.jsonl: a race at 14:42:30
    logged SUSPENSION=2 (`standard`), but cars5.ccrs's own mtime showed it
    wasn't written until 14:43:13 -- 43 seconds *after* that race was
    already logged -- and the very next race (14:50:59, after the save)
    correctly showed SUSPENSION=4 (`hard`), the value the player says was
    actually in use both times.

    The live widget read doesn't have this lag (it reflects the slider
    immediately, no save required), so it's used first here. An earlier
    version of this function used a different live source (the equipped-
    part "loadout array") that turned out to be actively unreliable, so it
    was dropped from this priority chain entirely rather than merged in as a
    second opinion, and the code has since been deleted as dead.
    Falls back to the save file per-category both for tabs not visited this
    session AND because the save file is keyed by the *race's own* car
    name, robust to the player having already moved on to tuning a
    different car in the garage by the time this race's result is
    processed."""
    live = _read_tuning_widgets(pid)
    if len(live) == len(TUNE_CATEGORIES):
        return live
    result = read_tuning_from_save(car_name)
    result.update(live)   # live values win per-category where both exist
    return result


# ── output ────────────────────────────────────────────────────────────────────
def _race_results_table_str(race: RaceResult, width: int = 82) -> str:
    """Plain-text results table -- header/rule/rows/closing rule, matching
    print_table()'s own table body exactly (factored out so it can also be
    reused for the API payload's `notes` field -- see _race_to_api_payload).
    Player names are already color-code-stripped at read time (see
    _strip_color_codes()), so this comes out clean with no extra work here.

    The trailing STATUS column carries "DNF" for any racer the engine flagged
    as not finishing (STATUS_DNF_BIT -- see OFF_STATUS_FLAGS). Because this
    same render is what gets posted as the API payload's `notes`, marking it
    here is what puts DNF status into the posted comment, not just the
    console. Rows stay in ranked order either way: DNFs are sorted below
    every finisher by _position_sort_key() and keep their real position
    number, so the column annotates the ranking rather than replacing it.
    Width widened from 76 to fit the extra column."""
    lines = [
        f"  {'POS':<4} {'NAME':<20} {'CAR':<18} {'CLASS':<7} {'BEST LAP':<11} {'TOTAL':<11} STATUS",
        f"  {'-' * (width - 2)}",
    ]
    for p in race.players:
        name = f"{p.name} (you)" if p.is_local else p.name
        lines.append(f"  {p.position:<4} {name:<20} {p.car:<18} {p.class_str():<7} "
                      f"{ms_to_str(p.best_lap_ms):<11} {ms_to_str(p.total_time_ms):<11} "
                      f"{'DNF' if p.dnf else ''}".rstrip())
    lines.append("=" * width)
    return "\n".join(lines)


def print_table(race: RaceResult):
    loc = race.track
    if race.variation:
        loc += f" — {race.variation}"
    W = 82   # matches _race_results_table_str()'s default -- STATUS column
    print()
    print("=" * W)
    print(f"  RACE RESULTS  {loc}")
    print(f"  {race.timestamp}")
    if race.tuning:
        tuning_str = "  ".join(f"{cat}={idx}" for cat, idx in race.tuning.items())
        print(f"  Tuning: {tuning_str}")
    print("=" * W)
    print(_race_results_table_str(race, W))
    print()


def _player_to_dict(p: PlayerResult) -> dict:
    return {
        "position":      p.position,
        "name":          p.name,
        "car":           p.car,
        "class":         p.class_str(),
        "best_lap_ms":   p.best_lap_ms,
        "total_time_ms": p.total_time_ms,
        "best_lap":      ms_to_str(p.best_lap_ms),
        "total_time":    ms_to_str(p.total_time_ms),
        "lap_times_ms":  p.lap_times_ms,
        "lap_times":     [ms_to_str(t) for t in p.lap_times_ms],
        # Engine's own DNF flag (STATUS_DNF_BIT) -- not inferred from times.
        # Every racer is in this payload now, including those who completed
        # zero laps, so without this a DNF is indistinguishable from a
        # finisher who simply has no lap time recorded.
        "dnf":           p.dnf,
        # Real laps completed. PlayerResult.laps_completed holds the engine's
        # CURRENT LAP NUMBER, which is 1-indexed and therefore one higher --
        # verified live (a 2-lap race freezes it at 3, a 3-lap race at 4, and
        # it resets to 1 pre-race). Corrected here so consumers get the
        # actual count rather than the raw internal value.
        "laps_completed": max(0, p.laps_completed - 1),
    }


def race_to_dict(race: RaceResult) -> dict:
    local = next((p for p in race.players if p.is_local), None)
    others = [p for p in race.players if not p.is_local]
    return {
        "track":     race.track,
        "variation": race.variation,
        "timestamp": race.timestamp,
        "tuning":    race.tuning or {},
        "player":    _player_to_dict(local) if local else None,
        "others":    [_player_to_dict(p) for p in others],
    }


def append_race_log(race: RaceResult, log_path: str):
    with open(log_path, "a") as f:
        f.write(json.dumps(race_to_dict(race)) + "\n")


# ── API config / posting (Wreckfest 2 Race Log backend) ──────────────────────
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config(path: str) -> dict:
    """Loads {api_key, supabase_url, supabase_anon_key} from JSON. supabase_url
    is the full endpoint URL, used as-is. Missing/invalid config disables API
    posting rather than crashing the tool."""
    try:
        with open(path) as f:
            config = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: couldn't read config file {path}: {e}")
        return {}
    if not isinstance(config, dict):
        print(f"WARNING: config file {path} must contain a JSON object")
        return {}
    return config


def _api_config_complete(config: dict) -> bool:
    return bool(config.get("api_key") and config.get("supabase_url") and config.get("supabase_anon_key"))


def _race_to_api_payload(race: RaceResult) -> Optional[dict]:
    """None if no local player identified in this race (caller skips posting)."""
    local = next((p for p in race.players if p.is_local), None)
    if local is None:
        return None
    tuning = race.tuning or {}

    def tuning_1indexed(category):
        v = tuning.get(category)
        return v + 1 if v is not None else None   # internal 0-4 -> API 1-5

    payload = {
        "track":   race.track,
        "variant": race.variation,
        "vehicle": local.car or "",
    }
    optional_fields = {
        "performance_index": local.class_rating,
        "place":             local.position,
        "lap_time_ms":       local.best_lap_ms,
        "total_time_ms":     local.total_time_ms,
        "suspension":        tuning_1indexed("SUSPENSION"),
        "gear_ratio":        tuning_1indexed("GEARING"),
        "differential":      tuning_1indexed("DIFFERENTIAL"),
        "brake_balance":     tuning_1indexed("BRAKES"),
        # Full field/finishing-order table, per user request 2026-08-08 --
        # same plain-text render used for console output (_race_results_table_str,
        # shared with print_table()). Names are already color-code-stripped
        # at read time, so this needs no extra cleanup here.
        "notes":             _race_results_table_str(race),
    }
    for key, value in optional_fields.items():
        if value is not None:
            payload[key] = value
    return payload


def post_race_result(config: dict, race: RaceResult, timeout: float = 10.0) -> bool:
    """POSTs to insert_race_with_api_key. Never raises -- network errors, non-
    2xx status, or in-body {"success": false} are reported and skipped. Note
    this endpoint always answers HTTP 200 even for validation failures, so the
    "success" field is the real signal, not HTTP status alone."""
    if not _api_config_complete(config):
        return False
    payload = _race_to_api_payload(race)
    if payload is None:
        print(f"[{_ts()}] API call skipped: no local player identified in this race")
        return False
    payload["api_key"] = config["api_key"]

    url = config["supabase_url"]
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "apikey": config["supabase_anon_key"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read()
    except urllib.error.URLError as e:
        print(f"[{_ts()}] API call failed: {e}")
        return False

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[{_ts()}] API call failed: HTTP {status}, non-JSON response")
        return False

    if not (200 <= status < 300):
        print(f"[{_ts()}] API call failed: HTTP {status}: {data.get('error', raw)}")
        return False
    if not data.get("success"):
        print(f"[{_ts()}] API submission failed: {data.get('error', 'unknown error')}")
        return False
    return True


# ── main ──────────────────────────────────────────────────────────────────────
def _race_is_final(players: list, pid: Optional[int] = None) -> bool:
    """True once the RESULTS SCREEN has been reached -- every real racer's
    status carries a terminal marker and nobody is still circulating. See the
    inline comment below for the two earlier, weaker signals this replaced
    and why each fired too early.

    The historical note that follows describes the ORIGINAL all-players
    check, kept because the failure it documents is still the reason the
    fallback path at the bottom is local-scoped rather than field-scoped.
    Deliberately checks only the local player, not every tracked racer (an
    earlier version required all(p.finished for p in players)) -- confirmed
    live 2026-08-08 in a real 24-player public lobby that requiring every
    single racer's bit means one straggler/AFK/slow finisher among 23 other
    people permanently blocks detection of the *local* player's own result:
    the local player sat `finished=True` with a frozen `total_time_ms` for
    9+ minutes while a handful of other racers were still out on track, and
    the tool never once considered the race final -- results had already
    cleared (player backed out) before the stragglers finished, so nothing
    was ever logged. Offline/solo races never hit this (just the player +
    AI, who finish within seconds of each other), which is why the bug was
    online-only. Falls back to the old all-players check only if the local
    player can't be identified at all (should be rare -- _mark_local_player()
    already has its own salvage path for a bad player_ptr specifically)."""
    local = next((p for p in players if p.is_local), None)

    # PRIMARY: the engine's own status field. STATUS_RUN_COMPLETE_BIT appears on the
    # local player's slot exactly when their result finalizes -- verified live
    # twice: in a 24-player online race it flipped 0->65 at the same instant
    # the local player crossed the line, and in an offline race where NOBODY
    # finished it appeared as results populated. That "nobody finished" case
    # is why this can't key off reaching a lap target: a race where everyone
    # DNFs is a perfectly normal outcome.
    #
    # This replaces FINISHED_BIT as the primary signal because FINISHED_BIT is
    # not a finish signal at all -- it is the low bit of the lap counter (see
    # OFF_FINISHED_FLAG), so it reads "finished" or "not finished" purely on
    # whether the counter happens to be odd. Confirmed live, twice: two
    # separate 3-lap races ended with every finisher frozen at counter=4
    # (even), so nothing was ever logged -- i.e. ANY race with an odd lap
    # count silently never logged at all.
    # PRIMARY: the RESULTS SCREEN -- every real racer's status has a terminal
    # marker (0x40 "run complete" or STATUS_DNF_BIT) and nobody is still
    # circulating. This is what the user asked for explicitly (2026-08-16):
    # capture whatever the results screen shows, players departed or not.
    #
    # Two weaker signals were tried first and both are wrong:
    #   - "ANY player has 0x40" fires when the LEADER crosses the line, which
    #     snapshots a mid-race field while the local player may still be laps
    #     from home. Confirmed live: seven slots carried 0x40 at once while
    #     the race ran on.
    #   - "the LOCAL player's own run is over" is better but still early: it
    #     fires the moment the local player finishes, capturing every other
    #     racer mid-race. Their rows -- and the API `notes` roster built from
    #     them -- are then not final results.
    #
    # `_STILL_RACING_COUNT` is what makes this reliable; see its comment for
    # why the returned player list alone cannot answer "is anyone still out
    # there". A departed racer simply leaves the slot array, so quitters
    # can't hold this open -- which is the behaviour the user wants, even
    # though it means positions may have renumbered around the departure.
    still_racing = _STILL_RACING_COUNT.get(pid) if pid is not None else None
    everyone_settled = all(p.status_flags & (STATUS_RUN_COMPLETE_BIT | STATUS_DNF_BIT)
                           for p in players)
    if players and everyone_settled and still_racing == 0:
        return True

    # FALLBACK: the old parity check, kept only for a mode where the status
    # field somehow never populates (none seen -- solo, offline AI and online
    # multiplayer were all verified to set it). Hardened with a completed-lap
    # requirement: without it, a local player with no lap time yet and an odd
    # counter reads "finished" one minute into a race, which is exactly how a
    # bogus mid-race result got logged in a 24-player lobby on 2026-08-15.
    if local is not None:
        return local.finished and MIN_LAP_MS <= local.best_lap_ms <= MAX_LAP_MS
    return all(p.finished for p in players)


def _emit_race(race: RaceResult, args, api_config: dict, allow_api: bool = True) -> None:
    """Print, optionally JSON-dump, always log, try the API unless allow_api
    is False -- the one sequence that happens for every newly-detected race.
    allow_api exists for main()'s local-player identity consistency check
    (see there): the file log always happens regardless, since that's safe
    to review/correct by hand, but a low-confidence local-player match
    should never get an unreviewable public API post attributed to the
    wrong account."""
    print_table(race)
    if args.json:
        print(json.dumps(race_to_dict(race), indent=2))
    append_race_log(race, args.log_file)
    print(f"[{_ts()}] Logged to {args.log_file}")
    if not allow_api:
        print(f"[{_ts()}] API post skipped -- local player identity unconfirmed this race (see warning above)")
    elif api_config and post_race_result(api_config, race):
        print(f"[{_ts()}] Posted to API")


def main():
    ap = argparse.ArgumentParser(description="Wreckfest race results scraper")
    ap.add_argument("--pid",      type=int, required=True, help="Game PID")
    ap.add_argument("--json",     action="store_true", help="Also emit JSON after the table")
    ap.add_argument("--interval", type=float, default=0.5, help="Poll interval in seconds (default: 0.5)")
    ap.add_argument("--debug",    action="store_true", help="Print raw slot addresses for each detected race")
    ap.add_argument("--watch-tuning", action="store_true",
                    help="Watch the pre-race tuning screen live and print each slider value as it's set")
    ap.add_argument("--log-file", default="race_log.jsonl",
                    help="JSON-lines file to append each completed race to. Default: race_log.jsonl")
    ap.add_argument("--config",   default=DEFAULT_CONFIG_PATH,
                    help="JSON config file with api_key/supabase_url/supabase_anon_key. "
                         "Default: config.json next to this script.")
    ap.add_argument("--no-api",   action="store_true", help="Don't POST results to the configured API")
    args = ap.parse_args()

    print("Wreckfest Race Results Scraper")
    print("-" * 40)

    api_config = {} if args.no_api else load_config(args.config)
    if _api_config_complete(api_config):
        print(f"API posting: enabled ({api_config['supabase_url']})")
    else:
        print("API posting: disabled (no config found)" if not args.no_api else "API posting: disabled (--no-api)")

    if args.watch_tuning:
        watch_tuning(args.pid, args.interval)
        return

    pid = args.pid
    print(f"Attached to PID {pid}")
    print(f"Polling every {args.interval}s — press Ctrl+C to stop")
    print("(Initial scan may take ~10s while memory is indexed)\n")

    # A race is only treated as truly over once BOTH signals agree, checked
    # against the LOCAL player only (see _race_is_final()'s docstring for why
    # -- in short, gating on every tracked racer instead of just the local
    # one means a single straggler in a big online lobby can block detection
    # of the local player's own, long-finished result forever):
    #   1. the local player's own FINISHED_BIT is set (_race_is_final()) --
    #      gates against a merely-paused game, since a pause never sets that
    #      bit (see the 2026-08-02 entries in PROJECT.md).
    #   2. the local player's own total_time_ms has stopped changing across
    #      consecutive polls (the user's own suggestion) -- confirmed live
    #      2026-08-06 that a real finish leaves total_time_ms dead-frozen
    #      (polled repeatedly with zero drift), matching the pre-FINISHED_BIT
    #      debounce this project used successfully before. Also confirmed
    #      live 2026-08-08: the local player's own clock freezes at their
    #      finish regardless of whether *other* racers are still mid-race --
    #      so scoping the fingerprint to the local player alone doesn't just
    #      avoid the other-racer trap, it's also the more accurate signal.
    # An edge-triggered check on FINISHED_BIT alone (the previous attempt at
    # this fix) wasn't enough for solo hot-lapping: confirmed live the same
    # session that a solo player's FINISHED_BIT can flip False->True->
    # False->True *twice* within one race -- a spurious blip partway
    # through (e.g. at lap 2 of 4, where total_time_ms keeps climbing right
    # through it, so it's clearly not really over yet) and then the real
    # one at the actual finish, where total_time_ms stops for good.
    # Requiring the clock to have actually stopped filters the mid-race
    # blip out cleanly without needing to know why the bit flickers there.
    last_fingerprint_seen   = None   # fingerprint from the previous poll, in any state
    last_logged_fingerprint = None   # fingerprint of the race already logged -- skip re-logging it
    cached_addrs = None
    scan_needed  = True
    # Session-established local-player identity, once resolved -- a safety
    # net against _mark_local_player()'s CLIENT-based resolution confidently
    # mislabeling a real *other* racer as local. Confirmed live 2026-08-08:
    # this happened twice in one session (two different real opponents each
    # logged, and API-posted, as the local player) while joining/spectating
    # multiplayer races -- root cause not fully pinned down (CLIENT's slot
    # index can plausibly go stale or get force-clamped in ways specific to
    # "connected but not actually seated this heat"), so rather than guess
    # at a targeted fix for a mechanism that couldn't be reproduced live to
    # confirm, this is a mechanism-independent guard: once a name has been
    # established as "you" this session, a later race resolving a DIFFERENT
    # name as local is treated as untrusted -- still logged to the file for
    # manual review, but the API post (the actually-harmful, public,
    # hard-to-undo action) is skipped. Bootstraps from whichever name
    # resolves first each session.
    confirmed_local_name = None

    while True:
        try:
            if scan_needed:
                players, cached_addrs = scrape_players(pid, None)
                scan_needed = False
            else:
                players, cached_addrs = scrape_players(pid, cached_addrs)
                if cached_addrs is None:
                    scan_needed = True

            if players:
                # Scoped to the LOCAL player only -- see _race_is_final()'s
                # docstring and the block comment above for why: keying this
                # off every tracked racer's total_time_ms meant a straggler
                # still out on track kept the fingerprint "unstable" forever,
                # blocking detection of the local player's own finish just
                # as badly as the old all-players _race_is_final() did.
                # Falls back to the whole-field fingerprint only if the local
                # player can't be identified at all (matches _race_is_final()'s
                # own fallback).
                local_player = next((p for p in players if p.is_local), None)
                if local_player is not None:
                    fingerprint = (local_player.name, local_player.total_time_ms)
                else:
                    fingerprint = tuple((p.name, p.total_time_ms) for p in players)
                is_stable = fingerprint == last_fingerprint_seen
                if (_race_is_final(players, pid) and is_stable
                        and fingerprint != last_logged_fingerprint):
                    if args.debug and cached_addrs:
                        print(f"[debug] cluster base = 0x{min(cached_addrs):016x}  ({len(cached_addrs)} slots)")
                        for a in cached_addrs:
                            print(f"[debug]   slot @ 0x{a:016x}")
                    resolve_local_car_name(pid, players)
                    resolve_car_names(pid, players)
                    track, variation = detect_track_and_variation(pid)
                    ts = time.strftime("%Y-%m-%d %H:%M:%S")
                    # Tuning to attach to this race -- live slider-widget
                    # read first, save-file fallback per category. See
                    # read_tuning_for_race()'s docstring for why: the save
                    # file alone can lag a real, already-raced tuning change
                    # until the Tune screen is backed out of. Needs the local
                    # player's car name, which resolve_local_car_name() just
                    # filled in above.
                    tuning = read_tuning_for_race(pid, local_player.car) if local_player else {}
                    race = RaceResult(track=track, variation=variation, timestamp=ts,
                                      players=players, tuning=tuning)
                    # Identity consistency check -- see confirmed_local_name's
                    # comment above for why this exists at all.
                    identity_trusted = True
                    if local_player is not None:
                        if confirmed_local_name is None:
                            confirmed_local_name = local_player.name
                        elif local_player.name != confirmed_local_name:
                            identity_trusted = False
                            print(f"[{_ts()}] WARNING: local player identity mismatch -- "
                                  f"expected {confirmed_local_name!r} (established earlier "
                                  f"this session) but resolved {local_player.name!r} as local "
                                  f"for this race. Logging to file, but skipping the API post "
                                  f"to avoid attributing someone else's result to your account.")
                    _emit_race(race, args, api_config, allow_api=identity_trusted)
                    last_logged_fingerprint = fingerprint
                last_fingerprint_seen = fingerprint
            else:
                if last_logged_fingerprint is not None:
                    print(f"[{_ts()}] Results cleared — waiting for next race...")
                last_fingerprint_seen = None
                last_logged_fingerprint = None

            time.sleep(args.interval)

        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except PermissionError:
            print(f"\nERROR: Permission denied reading /proc/{pid}/mem")
            print("Try: echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope")
            break
        except FileNotFoundError:
            print("\nERROR: Process disappeared — did the game close?")
            break


if __name__ == "__main__":
    main()
