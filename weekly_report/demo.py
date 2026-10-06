"""A made-up 10-team league built from *live* Sleeper projections.

Lets you see a real-looking report end to end (and test email/Pages delivery)
before Yahoo approves your API access: ``python -m weekly_report --demo``.
"""

from __future__ import annotations

import random
from typing import Dict, List

from . import sleeper as sleeper_api
from .lineup import best_lineup, starting_slots
from .model import MatchupResult, Standing, Team
from .yahoo import build_league

SLOTS = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1, "BN": 6, "IR": 1}
ROSTER_SHAPE = {"QB": 2, "RB": 5, "WR": 5, "TE": 2, "K": 1, "DEF": 1}
TEAM_NAMES = [
    "Hoang's Heroes", "Mahomes Alone", "Bijan Mustard", "Lamb Chops", "Chase the Dream",
    "Kelce Grammer", "The Bucky Irvings", "Nacua Matata", "Puka Shells", "Waddle Waddle",
]


def build(season: int = 0, week: int = 0, end_week: int = 17, seed: int = 7):
    state = sleeper_api.nfl_state()
    season = season or int(state.get("season") or 0)
    week = week or int(state.get("week") or 1)
    data = sleeper_api.SleeperData.fetch(season, range(week, end_week + 1))
    weeks = list(range(week, end_week + 1))
    rng = random.Random(seed)

    value = {pid: sum(data.weekly_points(pid, "ppr").values()) for pid in data.info}
    pool = sorted((pid for pid in data.info if data.info[pid]["position"] in ROSTER_SHAPE), key=lambda pid: -value[pid])

    # Snake draft: each pick takes one of the 3 best players at a position the team still needs.
    managers = ["You", "Alex", "Sam", "Jordan", "Taylor", "Casey", "Riley", "Morgan", "Jamie", "Drew"]
    teams = [Team(key=f"demo.t.{i + 1}", name=name, manager=managers[i], is_me=(i == 0)) for i, name in enumerate(TEAM_NAMES)]
    rosters: Dict[str, List[dict]] = {t.key: [] for t in teams}
    counts = {t.key: {pos: 0 for pos in ROSTER_SHAPE} for t in teams}
    taken = set()
    rounds = sum(ROSTER_SHAPE.values())
    for rnd in range(rounds):
        order = teams if rnd % 2 == 0 else list(reversed(teams))
        for team in order:
            need = counts[team.key]
            options = [pid for pid in pool if pid not in taken and need[data.info[pid]["position"]] < ROSTER_SHAPE[data.info[pid]["position"]]]
            pid = rng.choice(options[:3])
            taken.add(pid)
            meta = data.info[pid]
            need[meta["position"]] += 1
            rosters[team.key].append(
                {"name": meta["name"], "position": meta["position"], "eligible": meta["eligible"] or [meta["position"]],
                 "nfl_team": meta["team"], "slot": "BN", "status": meta["injury_status"]}
            )

    league = build_league(
        key="demo.l.1", name="Demo League", season=season, week=week, end_week=end_week, scoring="ppr",
        slots=SLOTS, teams=teams, rosters=rosters, projections=data, uses_faab=True,
        notes=["DEMO: teams, standings and scores are invented; player projections are live from Sleeper."],
    )

    # Set lineups the way a busy manager would: best lineup by name recognition (season-long value),
    # which leaves a few start/sit improvements for the report to catch.
    slots = starting_slots(SLOTS)
    for team in league.teams:
        _, lineup = best_lineup(team.players, slots, weeks[-1])
        for slot, player in lineup:
            player.slot = slot

    # Invented standings and scores.
    played = week - 1
    for i, team in enumerate(league.teams):
        wins = rng.randint(0, played)
        team.standing = Standing(wins=wins, losses=played - wins, points_for=round(rng.uniform(95, 125) * played, 1))
        team.standing.points_against = round(rng.uniform(95, 125) * played, 1)
        team.faab_balance = float(rng.randint(20, 100))
        team.waiver_priority = 10 - i
    for rank, team in enumerate(sorted(league.teams, key=lambda t: (-t.standing.wins, -t.standing.points_for)), 1):
        team.standing.rank = rank
    keys = [t.key for t in league.teams]
    for target, wk, shift in ((league.last_week, week - 1, 3), (league.this_week, week, 1)):
        rotated = keys[:1] + keys[1:][shift:] + keys[1:][:shift]
        for a, b in zip(rotated[:5], reversed(rotated[5:])):
            target.append(MatchupResult(wk, a, b, round(rng.uniform(85, 140), 1), round(rng.uniform(85, 140), 1)))
    return league
