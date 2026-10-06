"""Yahoo Fantasy Sports API: league structure for the report.

A small synchronous client (stdlib only) so the report runs from a cron job
without the MCP server's async stack. It needs the same env vars as the MCP
server: YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET, YAHOO_REFRESH_TOKEN (and
optionally YAHOO_ACCESS_TOKEN / YAHOO_GUID). The access token is refreshed up
front, so a stale one in CI secrets is fine.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

from . import sleeper as sleeper_api
from .model import League, MatchupResult, Player, Standing, Team
from .names import defense_team, norm_name, norm_team, normalize_position

API = "https://fantasysports.yahooapis.com/fantasy/v2"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
CORE_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

# Yahoo stat ids used to detect the scoring format.
STAT_RECEPTIONS = 11


class YahooError(RuntimeError):
    pass


class YahooClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, access_token: str = "") -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token = access_token

    @classmethod
    def from_env(cls) -> "YahooClient":
        missing = [k for k in ("YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET", "YAHOO_REFRESH_TOKEN") if not os.getenv(k)]
        if missing:
            raise YahooError(f"Missing environment variables: {', '.join(missing)}")
        return cls(
            os.environ["YAHOO_CLIENT_ID"],
            os.environ["YAHOO_CLIENT_SECRET"],
            os.environ["YAHOO_REFRESH_TOKEN"],
            os.getenv("YAHOO_ACCESS_TOKEN", ""),
        )

    def refresh(self) -> None:
        # Same form-encoded refresh the MCP server uses (src/api/yahoo_client.py).
        body = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode()
        request = urllib.request.Request(
            TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise YahooError(f"Yahoo token refresh failed ({exc.code}): {exc.read()[:300]!r}") from exc
        self.access_token = payload["access_token"]
        new_refresh = payload.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            self.refresh_token = new_refresh
            print(
                "warning: Yahoo issued a new refresh token; update YAHOO_REFRESH_TOKEN if the old one stops working",
                file=sys.stderr,
            )

    def get(self, endpoint: str) -> dict:
        if not self.access_token:
            self.refresh()
        for attempt in range(3):
            request = urllib.request.Request(
                f"{API}/{endpoint}?format=json",
                headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                text = exc.read().decode(errors="replace")
                if exc.code == 401 and "additional_authorization_required" in text:
                    raise YahooError(
                        "Your Yahoo app is not approved for the Fantasy Sports API yet. Apply at "
                        "https://sports.yahoo.com/developer/access/ with your existing Client ID."
                    ) from exc
                if exc.code == 401 and attempt == 0:
                    self.refresh()
                    continue
                if exc.code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise YahooError(f"Yahoo API {exc.code} for {endpoint}: {text[:300]}") from exc
        raise YahooError(f"Yahoo API retries exhausted for {endpoint}")


# ---- Yahoo JSON helpers ----------------------------------------------------
# Yahoo's JSON is an XML translation: entities are lists of single-key dicts
# (sometimes nested a level deeper) and collections are {"count": n, "0": {...}}.


def merge(obj: Any) -> Dict[str, Any]:
    """Flatten an entity's list-of-dicts into one dict (first value wins)."""
    if isinstance(obj, dict):
        return obj
    merged: Dict[str, Any] = {}
    if isinstance(obj, list):
        for item in obj:
            for key, value in merge(item).items():
                merged.setdefault(key, value)
    return merged


def items(collection: Any, key: str) -> Iterator[Any]:
    """Yield each `key` entity from a {"count": n, "0": {key: ...}} collection (or a list)."""
    if isinstance(collection, list):
        for entry in collection:
            if isinstance(entry, dict) and key in entry:
                yield entry[key]
            elif isinstance(entry, dict):
                yield from items(entry, key)
        return
    if isinstance(collection, dict):
        for name, entry in collection.items():
            if name != "count" and isinstance(entry, dict) and key in entry:
                yield entry[key]


def find(obj: Any, key: str) -> Any:
    """Depth-first search for the first value stored under `key`."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = find(child, key)
        if found is not None:
            return found
    return None


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _managers(meta: dict) -> List[dict]:
    managers = meta.get("managers") or []
    return [m["manager"] for m in (managers.values() if isinstance(managers, dict) else managers) if isinstance(m, dict) and "manager" in m]


# ---- parsing -----------------------------------------------------------------


def parse_leagues(data: dict) -> List[dict]:
    leagues = []
    user = merge(find(data, "user"))
    for game in items(user.get("games"), "game"):
        game_meta = merge(game)
        for league in items(game_meta.get("leagues"), "league"):
            meta = merge(league)
            leagues.append(
                {
                    "key": meta.get("league_key"),
                    "name": meta.get("name"),
                    "season": _int(meta.get("season")),
                    "current_week": _int(meta.get("current_week")),
                    "end_week": _int(meta.get("end_week")) or 17,
                    "is_finished": str(meta.get("is_finished")) == "1",
                }
            )
    return leagues


def parse_settings(data: dict) -> dict:
    league = merge(data["fantasy_content"]["league"])
    settings = merge(league.get("settings"))
    slots: Dict[str, int] = {}
    for entry in settings.get("roster_positions") or []:
        pos = entry.get("roster_position", entry) if isinstance(entry, dict) else {}
        name = pos.get("position")
        if name:
            slots[normalize_position(name)] = slots.get(normalize_position(name), 0) + _int(pos.get("count") or 1)
    modifiers = {}
    for entry in (settings.get("stat_modifiers") or {}).get("stats") or []:
        stat = entry.get("stat", {})
        modifiers[_int(stat.get("stat_id"))] = _float(stat.get("value"))
    reception = modifiers.get(STAT_RECEPTIONS, 0.0)
    scoring = "ppr" if reception >= 0.95 else "half_ppr" if reception >= 0.45 else "std"
    return {
        "name": league.get("name"),
        "season": _int(league.get("season")),
        "current_week": _int(league.get("current_week")),
        "end_week": _int(league.get("end_week")) or 17,
        "slots": slots,
        "scoring": scoring,
        "reception_points": reception,
        "uses_faab": str(settings.get("uses_faab")) == "1",
    }


def parse_teams(data: dict, my_guid: str = "") -> List[Team]:
    teams = []
    for team in items(find(data, "teams"), "team"):
        meta = merge(team)
        managers = _managers(meta)
        is_me = str(meta.get("is_owned_by_current_login")) == "1" or any(
            str(m.get("is_current_login")) == "1" or (my_guid and m.get("guid") == my_guid) for m in managers
        )
        stats = meta.get("team_standings") or {}
        outcome = stats.get("outcome_totals") or {}
        teams.append(
            Team(
                key=meta.get("team_key", ""),
                name=meta.get("name", "Unknown"),
                manager=managers[0].get("nickname", "") if managers else "",
                is_me=is_me,
                standing=Standing(
                    rank=_int(stats.get("rank")),
                    wins=_int(outcome.get("wins")),
                    losses=_int(outcome.get("losses")),
                    ties=_int(outcome.get("ties")),
                    points_for=_float(stats.get("points_for")),
                    points_against=_float(stats.get("points_against")),
                ),
                faab_balance=_float(meta["faab_balance"]) if meta.get("faab_balance") not in (None, "") else None,
                waiver_priority=_int(meta.get("waiver_priority")) or None,
            )
        )
    return teams


def parse_roster(data: dict) -> List[dict]:
    """[{name, position, eligible, nfl_team, slot, status}] from team/{key}/roster."""
    players = []
    roster = find(data, "roster") or {}
    for player in items(find(roster, "players"), "player"):
        meta = merge(player)
        name = (meta.get("name") or {}).get("full", "")
        if not name:
            continue
        display = normalize_position((meta.get("display_position") or meta.get("primary_position") or "").split(",")[0])
        eligible = []
        for entry in meta.get("eligible_positions") or []:
            pos = normalize_position(entry.get("position", "") if isinstance(entry, dict) else str(entry))
            if pos in CORE_POSITIONS and pos not in eligible:
                eligible.append(pos)
        slot = merge(meta.get("selected_position")).get("position", "")
        players.append(
            {
                "name": name,
                "position": display or (eligible[0] if eligible else ""),
                "eligible": eligible or [display],
                "nfl_team": norm_team(meta.get("editorial_team_abbr", "")),
                "slot": normalize_position(slot),
                "status": meta.get("status", "") or "",
            }
        )
    return players


def parse_scoreboard(data: dict, week: int) -> List[MatchupResult]:
    results = []
    for matchup in items(find(data, "matchups"), "matchup"):
        sides = []
        for team in items(find(matchup, "teams"), "team"):
            meta = merge(team)
            sides.append((meta.get("team_key", ""), _float((meta.get("team_points") or {}).get("total"))))
        if len(sides) == 2:
            (a, pa), (b, pb) = sides
            results.append(MatchupResult(week=week, team_a=a, team_b=b, points_a=pa, points_b=pb))
    return results


# ---- assembling the league -----------------------------------------------------


def discover_leagues(client: YahooClient) -> List[dict]:
    leagues = parse_leagues(client.get("users;use_login=1/games;game_codes=nfl/leagues"))
    if not leagues:
        return []
    latest = max(l["season"] for l in leagues)
    return [l for l in leagues if l["season"] == latest and not l["is_finished"]]


def load_league(client: YahooClient, league_key: str, week: Optional[int] = None, fa_per_position: int = 40) -> League:
    settings = parse_settings(client.get(f"league/{league_key}/settings"))
    season = settings["season"]
    if week is None:
        week = settings["current_week"]
        try:
            state = sleeper_api.nfl_state()
            # On a Tuesday Sleeper has already rolled to the upcoming week; Yahoo may not have.
            if str(state.get("season")) == str(season) and state.get("season_type") == "regular":
                week = max(week, _int(state.get("week")))
        except Exception:
            pass
    end_week = settings["end_week"]
    weeks = list(range(week, end_week + 1))

    my_guid = os.getenv("YAHOO_GUID", "")
    teams = parse_teams(client.get(f"league/{league_key}/standings"), my_guid)
    rosters = {team.key: parse_roster(client.get(f"team/{team.key}/roster")) for team in teams}

    positions = [p for p in CORE_POSITIONS if p in settings["slots"]]
    projections = sleeper_api.SleeperData.fetch(season, weeks, positions)

    notes = []
    if settings["scoring"] == "std" and settings["reception_points"] not in (0.0,):
        notes.append(f"Unusual reception scoring ({settings['reception_points']}/rec); projections use standard.")
    league = build_league(
        key=league_key,
        name=settings["name"],
        season=season,
        week=week,
        end_week=end_week,
        scoring=settings["scoring"],
        slots=settings["slots"],
        teams=teams,
        rosters=rosters,
        projections=projections,
        uses_faab=settings["uses_faab"],
        fa_per_position=fa_per_position,
        notes=notes,
    )
    for target, wk in ((league.last_week, week - 1), (league.this_week, week)):
        if wk >= 1:
            try:
                target.extend(parse_scoreboard(client.get(f"league/{league_key}/scoreboard;week={wk}"), wk))
            except YahooError as exc:
                league.notes.append(f"Scoreboard for week {wk} unavailable: {exc}")
    return league


def build_league(
    *,
    key: str,
    name: str,
    season: int,
    week: int,
    end_week: int,
    scoring: str,
    slots: Dict[str, int],
    teams: List[Team],
    rosters: Dict[str, List[dict]],
    projections: "sleeper_api.SleeperData",
    uses_faab: bool = False,
    fa_per_position: int = 40,
    notes: Optional[List[str]] = None,
) -> League:
    """Join Yahoo rosters with Sleeper projections; everything unrostered becomes the FA pool."""
    index = projections.build_index()
    weeks = set(range(week, end_week + 1))
    rostered_ids = set()
    rostered_names = set()
    unmatched = []

    def make_player(pid: Optional[str], **fields: Any) -> Player:
        player = Player(**fields)
        if pid:
            player.sleeper_id = pid
            player.weekly = {w: p for w, p in projections.weekly_points(pid, scoring).items() if w in weeks}
            player.season_points, player.games_played = projections.season_line(pid, scoring)
            player.trending_adds = projections.trending.get(pid, 0)
            if not player.status:
                player.status = projections.info.get(pid, {}).get("injury_status", "")
        return player

    for team in teams:
        team.players = []
        for entry in rosters.get(team.key, []):
            team_hint = entry["nfl_team"]
            if entry["position"] == "DEF" and not team_hint:
                team_hint = defense_team(entry["name"])
            pid = index.resolve(entry["name"], entry["position"], team_hint)
            if pid:
                rostered_ids.add(pid)
            else:
                unmatched.append(f"{entry['name']} ({entry['position']})")
            rostered_names.add(norm_name(entry["name"]))
            team.players.append(make_player(pid, **entry))

    pool: Dict[str, List[Player]] = {}
    for pid, meta in projections.info.items():
        if pid in rostered_ids or meta["position"] not in slots:
            continue
        if meta["position"] != "DEF" and norm_name(meta["name"]) in rostered_names:
            continue  # name collision with a rostered player we may have mis-resolved
        player = make_player(
            pid, name=meta["name"], position=meta["position"], nfl_team=meta["team"], eligible=meta["eligible"] or [meta["position"]]
        )
        if player.weekly:
            pool.setdefault(player.position, []).append(player)
    free_agents = []
    for players in pool.values():
        players.sort(key=lambda p: p.total(sorted(weeks)), reverse=True)
        free_agents.extend(players[:fa_per_position])

    notes = list(notes or [])
    if unmatched:
        notes.append("No Sleeper projection found for: " + ", ".join(sorted(unmatched)) + ".")
    return League(
        key=key,
        name=name,
        season=season,
        week=week,
        end_week=end_week,
        scoring=scoring,
        slots=slots,
        teams=teams,
        free_agents=free_agents,
        uses_faab=uses_faab,
        notes=notes,
    )
