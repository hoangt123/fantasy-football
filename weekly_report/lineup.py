"""Optimal-lineup math: the yardstick every recommendation is measured with."""

from __future__ import annotations

from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .model import Player

NON_STARTING = {"BN", "IR", "IR+", "NA"}

FLEX_ELIGIBILITY: Dict[str, FrozenSet[str]] = {
    "W/R/T": frozenset({"WR", "RB", "TE"}),
    "W/R": frozenset({"WR", "RB"}),
    "W/T": frozenset({"WR", "TE"}),
    "R/T": frozenset({"RB", "TE"}),
    "Q/W/R/T": frozenset({"QB", "WR", "RB", "TE"}),
    "SUPERFLEX": frozenset({"QB", "WR", "RB", "TE"}),
    "FLEX": frozenset({"WR", "RB", "TE"}),
}


def starting_slots(slots: Dict[str, int]) -> List[Tuple[str, FrozenSet[str]]]:
    """Expand {"RB": 2, "W/R/T": 1} into one entry per starting slot.

    Narrowest slots come first so the greedy fill below never spends a player
    on a flex spot that only he could have filled at his own position.
    """
    expanded = []
    for name, count in slots.items():
        if name in NON_STARTING:
            continue
        eligible = FLEX_ELIGIBILITY.get(name, frozenset({name}))
        expanded.extend([(name, eligible)] * int(count))
    expanded.sort(key=lambda item: len(item[1]))
    return expanded


def best_lineup(
    players: Iterable[Player],
    slots: Sequence[Tuple[str, FrozenSet[str]]],
    week: int,
    value: Optional[Callable[[Player], float]] = None,
) -> Tuple[float, List[Tuple[str, Player]]]:
    """Highest-projected legal lineup for one week, as (points, [(slot, player)]).

    `value` overrides the per-player score (e.g. rest-of-season total)."""
    score = value or (lambda p: p.proj(week))
    ranked = sorted(players, key=score, reverse=True)
    used = set()
    total = 0.0
    lineup = []
    for slot_name, eligible in slots:
        for player in ranked:
            if id(player) in used or not eligible.intersection(player.eligible):
                continue
            used.add(id(player))
            total += score(player)
            lineup.append((slot_name, player))
            break
    return total, lineup


def lineup_points(
    players: Sequence[Player], slots: Sequence[Tuple[str, FrozenSet[str]]], weeks: Iterable[int]
) -> float:
    """Sum of best-lineup points across weeks (each week re-optimized, so byes count)."""
    return sum(best_lineup(players, slots, w)[0] for w in weeks)


def points_by_position(
    players: Sequence[Player], slots: Sequence[Tuple[str, FrozenSet[str]]], weeks: Iterable[int]
) -> Dict[str, float]:
    """Starting-lineup points attributed to each player's primary position."""
    totals: Dict[str, float] = {}
    for week in weeks:
        for _slot, player in best_lineup(players, slots, week)[1]:
            totals[player.position] = totals.get(player.position, 0.0) + player.proj(week)
    return totals
