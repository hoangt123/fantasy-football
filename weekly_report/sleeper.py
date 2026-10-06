"""Sleeper public API: week-by-week projections, season stats and trending adds.

Uses the ``api.sleeper.app/projections`` endpoint (not ``/v1/projections``,
which currently returns empty rows) because it carries real per-week fantasy
point projections plus the player's name, team and injury status inline.
No API key needed.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Optional, Tuple

from .names import norm_name, norm_team

BASE = "https://api.sleeper.app"
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
SCORING_COLUMN = {"ppr": "pts_ppr", "half_ppr": "pts_half_ppr", "std": "pts_std"}


def get_json(url: str, retries: int = 3) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "fantasy-weekly-report/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def _positions_query(positions: Iterable[str]) -> str:
    return "&".join("position[]=" + urllib.parse.quote(p) for p in positions)


def nfl_state() -> dict:
    return get_json(f"{BASE}/v1/state/nfl")  # type: ignore[return-value]


class SleeperData:
    """Projections and stats for every relevant NFL player, keyed by Sleeper id."""

    def __init__(self) -> None:
        self.info: Dict[str, dict] = {}
        self.weekly: Dict[str, Dict[int, dict]] = {}
        self.season: Dict[str, dict] = {}
        self.trending: Dict[str, int] = {}

    # ---- fetching -------------------------------------------------------
    @classmethod
    def fetch(cls, season: int, weeks: Iterable[int], positions: Iterable[str] = POSITIONS) -> "SleeperData":
        data = cls()
        positions = list(positions)
        query = _positions_query(positions)
        for week in weeks:
            rows = get_json(f"{BASE}/projections/nfl/{season}/{week}?season_type=regular&{query}")
            for row in rows or []:  # type: ignore[union-attr]
                data._remember(row)
                data.weekly.setdefault(row["player_id"], {})[week] = row.get("stats") or {}
        try:
            rows = get_json(f"{BASE}/stats/nfl/{season}?season_type=regular&{query}")
            for row in rows or []:  # type: ignore[union-attr]
                data._remember(row)
                data.season[row["player_id"]] = row.get("stats") or {}
        except Exception:
            pass  # season-to-date context is nice-to-have
        try:
            trending = get_json(f"{BASE}/v1/players/nfl/trending/add?lookback_hours=48&limit=100")
            data.trending = {str(t["player_id"]): int(t.get("count", 0)) for t in trending or []}  # type: ignore[union-attr]
        except Exception:
            pass
        return data

    def _remember(self, row: dict) -> None:
        pid = str(row.get("player_id"))
        player = row.get("player") or {}
        if pid in self.info or not player:
            return
        position = player.get("position") or ""
        # Defenses come through as first_name="Seattle", last_name="Seahawks".
        name = f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()
        self.info[pid] = {
            "name": name,
            "position": position,
            "eligible": [p for p in (player.get("fantasy_positions") or [position]) if p in POSITIONS],
            "team": norm_team(row.get("team") or player.get("team") or ""),
            "injury_status": player.get("injury_status") or "",
        }

    # ---- lookups --------------------------------------------------------
    def weekly_points(self, pid: str, scoring: str) -> Dict[int, float]:
        column = SCORING_COLUMN.get(scoring, "pts_ppr")
        return {
            week: float(stats.get(column) or 0.0)
            for week, stats in self.weekly.get(pid, {}).items()
            if stats.get(column)
        }

    def season_line(self, pid: str, scoring: str) -> Tuple[float, int]:
        stats = self.season.get(pid) or {}
        column = SCORING_COLUMN.get(scoring, "pts_ppr")
        return float(stats.get(column) or 0.0), int(stats.get("gp") or 0)

    def build_index(self) -> "PlayerIndex":
        return PlayerIndex(self.info)


class PlayerIndex:
    """Resolves Yahoo (name, position, team) to a Sleeper id."""

    def __init__(self, info: Dict[str, dict]) -> None:
        self.by_name: Dict[str, List[str]] = {}
        self.defense_by_team: Dict[str, str] = {}
        for pid, meta in info.items():
            if meta["position"] == "DEF":
                self.defense_by_team[meta["team"] or pid] = pid
            else:
                self.by_name.setdefault(norm_name(meta["name"]), []).append(pid)
        self.info = info

    def resolve(self, name: str, position: str, team: str = "") -> Optional[str]:
        if position == "DEF":
            return self.defense_by_team.get(norm_team(team))
        candidates = self.by_name.get(norm_name(name), [])
        if len(candidates) <= 1:
            return candidates[0] if candidates else None

        # Same name, several players: prefer position + team, then either one.
        def score(pid: str) -> int:
            meta = self.info[pid]
            return (2 if position in meta["eligible"] or meta["position"] == position else 0) + (
                1 if team and meta["team"] == norm_team(team) else 0
            )

        return max(candidates, key=score)
