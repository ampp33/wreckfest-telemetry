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

# addr-32: int32, low bits are a per-slot base value (unrelated), bit 0x100 is
# set exactly once, at the moment THIS player crosses the finish line -- found
# by live-diffing the whole struct across a real race (incl. a deliberate
# pause, to rule out a false positive): each slot's own transition lands at a
# different real-world time matching actual finish order, and total_time_ms
# for every slot stops changing for good only once the LAST player's bit is
# set. This is the real, per-player "has finished" signal -- unlike
# total_time_ms (a shared, still-ticking clock until each player finishes),
# it can't be fooled by a pause, a tie, or another player finishing first.
OFF_FINISHED_FLAG = -32
FINISHED_BIT       = 0x100

POFF_NAME   = 0
POFF_CAR    = 48
POFF_ENGINE = 288

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
    engine:        str
    class_letter:  str
    class_rating:  int
    best_lap_ms:   int
    total_time_ms: int
    lap_times_ms:  list
    is_local:      bool = False
    finished:      bool = False   # this player's own OFF_FINISHED_FLAG bit is set

    def class_str(self) -> str:
        return f"{self.class_letter} {self.class_rating}"

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


_VEHICLE_NAME_RE = re.compile(rb'VEHICLE_NAME_[0-9]+_[0-9]+')


def _find_nearby_vehicle_name_key(f, near_addr: int, window: int = 256) -> Optional[str]:
    """Recovers an unresolved VEHICLE_NAME_<id>_<variant> template key sitting
    near a garbled car-name field (asset still streaming in)."""
    blob = _rd(f, near_addr, window)
    if not blob:
        return None
    m = _VEHICLE_NAME_RE.search(blob)
    return m.group(0).decode('ascii') if m else None


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

    name   = rcstr(f, ptr + POFF_NAME,   64)
    car    = rcstr(f, ptr + POFF_CAR,    64)
    engine = rcstr(f, ptr + POFF_ENGINE, 32)

    if not name:
        return None
    if _looks_garbled(engine):
        engine = ""
    if car and car[0] in ('+', '-') and ':' in car:
        return None   # looks like a time-delta string -- stray pointer

    laps = [d['best_lap_ms']] if MIN_LAP_MS <= d['best_lap_ms'] <= MAX_LAP_MS else []

    # Car name may be an unresolved "VEHICLE_NAME_<id>_<variant>" localization
    # key (asset still streaming in), or outright garbled with the key sitting
    # nearby instead -- resolve either case via the loc-string table.
    if module_base is not None and table_base is not None and car.startswith("VEHICLE_NAME_"):
        resolved = _resolve_localized_string(f, module_base, table_base, car)
        if resolved:
            car = resolved
    elif module_base is not None and table_base is not None and _looks_garbled(car):
        key = _find_nearby_vehicle_name_key(f, ptr + POFF_CAR)
        if key:
            resolved = _resolve_localized_string(f, module_base, table_base, key)
            car = resolved if resolved else ""
        else:
            car = ""

    flag = ri32(f, d['addr'] + OFF_FINISHED_FLAG)
    finished = bool(flag is not None and flag & FINISHED_BIT)

    return PlayerResult(
        position=0, name=name, car=car, engine=engine,
        class_letter=class_from_rating(d['class_rating']),
        class_rating=d['class_rating'], best_lap_ms=d['best_lap_ms'],
        total_time_ms=d['total_time_ms'], lap_times_ms=laps, is_local=False,
        finished=finished,
    )


def _local_player_slot_index(f, table_base: int) -> Optional[int]:
    """CLIENT singleton's first field is the local client's car slot index in
    multiplayer; -1 (no network client) means solo/AI, which the engine seats at slot 0."""
    client_obj = _hash_registry_lookup(f, table_base, "CLIENT")
    if not client_obj:
        return None
    raw = ri32(f, client_obj)
    if raw is None:
        return None
    return raw if raw > -1 else 0


def _mark_local_player(f, module_base: Optional[int], table_base: Optional[int], pairs: list, slot0: Optional[int]) -> None:
    """Marks the entry at the CLIENT-derived local slot index. Falls back to
    lowest address (wrong in general, but only reachable if CLIENT can't resolve)."""
    if not pairs:
        return
    local_player = None
    if module_base is not None and table_base is not None and slot0 is not None:
        local_slot = _local_player_slot_index(f, table_base)
        if local_slot is not None:
            for addr, p in pairs:
                if (addr - slot0) // SLOT_STRIDE == local_slot:
                    local_player = p
                    break
    if local_player is None:
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
                cached_pairs = []
                seen_names: set = set()
                for addr in cached_addrs:
                    d = validate_entry(f, addr)
                    if d is None:
                        continue
                    p = read_player(f, d, module_base, table_base)
                    if p and p.name not in seen_names:
                        seen_names.add(p.name)
                        cached_pairs.append((addr, p))
                if cached_pairs:
                    _mark_local_player(f, module_base, table_base, cached_pairs, min(cached_addrs))
                    cached_players = [p for _, p in cached_pairs]
                    cached_players.sort(key=lambda x: x.total_time_ms)
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
            for addr in hits:
                d = validate_entry(f, addr)
                if d is None:
                    continue
                p = read_player(f, d, module_base, table_base)
                if p is None or p.name in seen_names:
                    continue
                seen_names.add(p.name)
                pairs.append((addr, p))

            if not pairs:
                return None, None
            _mark_local_player(f, module_base, table_base, pairs, min(hits))
    except _PROC_ERRORS:
        return None, None

    players_raw = [p for _, p in pairs]
    good_addrs = [addr for addr, _ in pairs]
    players_raw.sort(key=lambda x: x.total_time_ms)
    for i, p in enumerate(players_raw):
        p.position = i + 1
    return players_raw, good_addrs


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


def read_tuning(pid: int) -> dict:
    """Current 0-4 index per tuning category; a not-yet-visited category is omitted, not guessed."""
    result = {}
    try:
        with _mem_and_bases(pid) as (f, _, table_base):
            if not table_base:
                return result
            anchor_addr = _tune_slider_track_ptr(f, table_base, "SUSPENSION")
            for category in TUNE_CATEGORIES:
                if category == "DIFFERENTIAL":
                    raw_idx = _read_differential(f, pid, anchor_addr)
                    if raw_idx is not None:
                        result[category] = raw_idx
                    continue
                track_ptr = anchor_addr if category == "SUSPENSION" else _tune_slider_track_ptr(f, table_base, category)
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


# ── output ────────────────────────────────────────────────────────────────────
def print_table(race: RaceResult):
    loc = race.track
    if race.variation:
        loc += f" — {race.variation}"
    W = 76
    print()
    print("=" * W)
    print(f"  RACE RESULTS  {loc}")
    print(f"  {race.timestamp}")
    if race.tuning:
        tuning_str = "  ".join(f"{cat}={idx}" for cat, idx in race.tuning.items())
        print(f"  Tuning: {tuning_str}")
    print("=" * W)
    print(f"  {'POS':<4} {'NAME':<20} {'CAR':<18} {'CLASS':<7} {'BEST LAP':<11} TOTAL")
    print(f"  {'-' * (W - 2)}")
    for p in race.players:
        laps = ""
        if p.lap_times_ms:
            laps = "  [" + "  ".join(ms_to_str(t) for t in p.lap_times_ms) + "]"
        name = f"{p.name} (you)" if p.is_local else p.name
        print(f"  {p.position:<4} {name:<20} {p.car:<18} {p.class_str():<7} "
              f"{ms_to_str(p.best_lap_ms):<11} {ms_to_str(p.total_time_ms)}{laps}")
    print("=" * W)
    print()


def _player_to_dict(p: PlayerResult) -> dict:
    return {
        "position":      p.position,
        "name":          p.name,
        "car":           p.car,
        "engine":        p.engine,
        "class":         p.class_str(),
        "best_lap_ms":   p.best_lap_ms,
        "total_time_ms": p.total_time_ms,
        "best_lap":      ms_to_str(p.best_lap_ms),
        "total_time":    ms_to_str(p.total_time_ms),
        "lap_times_ms":  p.lap_times_ms,
        "lap_times":     [ms_to_str(t) for t in p.lap_times_ms],
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
def _race_is_final(players: list) -> bool:
    """True once every player's own OFF_FINISHED_FLAG bit is set -- see the
    comment there for how this was found and verified live. Replaces an
    earlier total_time_ms-based heuristic (comparing values across players
    and polls) that had no reliable way to tell "everyone genuinely finished"
    apart from "the game is paused" -- both look identical when all you have
    is a clock. This is a real per-player signal instead of an inference."""
    return all(p.finished for p in players)


def _emit_race(race: RaceResult, args, api_config: dict) -> None:
    """Print, optionally JSON-dump, always log, always try the API -- the one
    sequence that happens for every newly-detected race."""
    print_table(race)
    if args.json:
        print(json.dumps(race_to_dict(race), indent=2))
    append_race_log(race, args.log_file)
    print(f"[{_ts()}] Logged to {args.log_file}")
    if api_config and post_race_result(api_config, race):
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

    last_fingerprint = None   # last CONFIRMED (already-logged) race
    cached_addrs     = None
    scan_needed      = True
    cached_tuning    = {}

    while True:
        try:
            if scan_needed:
                players, cached_addrs = scrape_players(pid, None)
                scan_needed = False
            else:
                players, cached_addrs = scrape_players(pid, cached_addrs)
                if cached_addrs is None:
                    scan_needed = True

            if not players:
                # Tuning only exists on the pre-race setup screen -- captured
                # opportunistically here, before results ever appear. Once
                # `players` is non-empty (results showing), the tuning
                # widgets are gone/reset, so reading here would silently
                # clobber the already-captured values with stale zeros.
                current_tuning = read_tuning(pid)
                if current_tuning:
                    cached_tuning.update(current_tuning)

            if players:
                fingerprint = tuple((p.name, p.total_time_ms) for p in players)
                if fingerprint != last_fingerprint and _race_is_final(players):
                    if args.debug and cached_addrs:
                        print(f"[debug] cluster base = 0x{min(cached_addrs):016x}  ({len(cached_addrs)} slots)")
                        for a in cached_addrs:
                            print(f"[debug]   slot @ 0x{a:016x}")
                    resolve_local_car_name(pid, players)
                    track, variation = detect_track_and_variation(pid)
                    ts = time.strftime("%Y-%m-%d %H:%M:%S")
                    race = RaceResult(track=track, variation=variation, timestamp=ts,
                                      players=players, tuning=dict(cached_tuning))
                    _emit_race(race, args, api_config)
                    last_fingerprint = fingerprint
            else:
                if last_fingerprint is not None:
                    print(f"[{_ts()}] Results cleared — waiting for next race...")
                    last_fingerprint = None
                    cached_tuning = {}   # next race starts a fresh setup capture

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
