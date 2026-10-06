"""Offline tests for the weekly report (no Yahoo or Sleeper calls)."""

from __future__ import annotations

import json

from weekly_report.analysis import build_report, lineup_check, trade_ideas, team_strengths, waiver_moves
from weekly_report.lineup import best_lineup, starting_slots
from weekly_report.model import League, MatchupResult, Player, Team
from weekly_report.names import norm_name
from weekly_report.render import to_html, to_json, to_markdown
from weekly_report.sleeper import PlayerIndex
from weekly_report.yahoo import parse_roster, parse_scoreboard, parse_settings, parse_teams

WEEKS = [5, 6, 7]
SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1, "BN": 4, "IR": 1}


def P(name, pos, pts, slot="BN", bye=None, **kw):
    """Player projecting `pts` every week except an optional bye week."""
    weekly = {w: float(pts) for w in WEEKS if w != bye}
    return Player(name=name, position=pos, nfl_team=kw.pop("team", "AAA"), slot=slot, weekly=weekly, **kw)


def base_roster(prefix, rb=12, wr=12):
    return [
        P(f"{prefix} QB", "QB", 20, "QB"),
        P(f"{prefix} RB1", "RB", rb, "RB"),
        P(f"{prefix} RB2", "RB", rb - 2, "RB"),
        P(f"{prefix} WR1", "WR", wr, "WR"),
        P(f"{prefix} WR2", "WR", wr - 2, "WR"),
        P(f"{prefix} TE", "TE", 8, "TE"),
        P(f"{prefix} FLEX", "WR", 7, "W/R/T"),
        P(f"{prefix} K", "K", 8, "K"),
        P(f"{prefix} DEF", "DEF", 7, "DEF"),
        P(f"{prefix} BN1", "RB", 5),
        P(f"{prefix} BN2", "WR", 4),
    ]


def make_league(me_players, other_players=None, free_agents=()):
    me = Team("t.1", "Mine", is_me=True, players=me_players)
    other = Team("t.2", "Theirs", manager="Pat", players=other_players or base_roster("O"))
    return League(
        key="l.1", name="Test League", season=2026, week=5, end_week=7, scoring="ppr",
        slots=SLOTS, teams=[me, other], free_agents=list(free_agents),
        this_week=[MatchupResult(5, "t.1", "t.2")], last_week=[MatchupResult(4, "t.1", "t.2", 100.0, 90.0)],
    )


# ---- lineup -------------------------------------------------------------------


def test_best_lineup_fills_flex_with_best_leftover():
    slots = starting_slots({"RB": 1, "WR": 1, "W/R/T": 1})
    players = [P("rb1", "RB", 15), P("rb2", "RB", 11), P("wr1", "WR", 12), P("te", "TE", 9)]
    total, lineup = best_lineup(players, slots, 5)
    assert total == 38
    assert {(s, p.name) for s, p in lineup} == {("RB", "rb1"), ("WR", "wr1"), ("W/R/T", "rb2")}


def test_bye_week_player_sits():
    slots = starting_slots({"WR": 1})
    players = [P("star", "WR", 20, bye=6), P("backup", "WR", 5)]
    assert best_lineup(players, slots, 5)[1][0][1].name == "star"
    assert best_lineup(players, slots, 6)[1][0][1].name == "backup"


# ---- recommendations ---------------------------------------------------------


def test_waiver_adds_clear_upgrade_and_drops_dead_weight():
    stud = P("FA Stud", "RB", 18)
    league = make_league(base_roster("M"), free_agents=[stud, P("FA Meh", "WR", 3)])
    moves = waiver_moves(league)
    assert moves and moves[0].add is stud
    assert moves[0].drop.slot == "BN"  # never cut a starter for him
    assert moves[0].gain_ros > 0


def test_waiver_covers_bye_week_at_kicker():
    roster = base_roster("M")
    roster[7] = P("M K", "K", 8, "K", bye=5)
    streamer = P("FA K", "K", 6)
    moves = waiver_moves(make_league(roster, free_agents=[streamer]))
    assert moves and moves[0].add is streamer
    assert moves[0].gain_short > 0


def test_waiver_will_not_cut_useful_depth_to_stream_a_kicker():
    roster = base_roster("M")
    roster.append(P("M WR3", "WR", 9))  # useful depth
    roster = [p for p in roster if p.name not in ("M BN1", "M BN2")]
    kicker = P("FA K", "K", 8.5)  # +0.5/wk over current kicker
    moves = waiver_moves(make_league(roster, free_agents=[kicker]))
    assert not moves or moves[0].drop.name != "M WR3"


def test_trade_swaps_surplus_for_need():
    # I have four good RBs (one stuck on the bench) and weak WRs; they have the opposite.
    mine = base_roster("M", rb=16, wr=6)
    mine[6] = P("M FLEX", "RB", 14, "W/R/T")
    mine[9] = P("M RB4", "RB", 13)
    theirs = base_roster("O", rb=6, wr=16)
    theirs[6] = P("O FLEX", "WR", 14, "W/R/T")
    theirs[10] = P("O WR4", "WR", 13)
    league = make_league(mine, theirs)
    ideas = trade_ideas(league, team_strengths(league))
    assert ideas, "expected a complementary RB-for-WR trade"
    best = ideas[0]
    assert "M RB4" in [p.name for p in best.give] and {p.position for p in best.get} == {"WR"}
    assert best.my_gain > 0 and best.their_gain >= 0


def test_lineup_check_flags_bye_week_starter():
    roster = base_roster("M")
    roster[3] = P("M WR1", "WR", 12, "WR", bye=5)
    changes, alerts = lineup_check(make_league(roster))
    assert any(c.bench and c.bench.name == "M WR1" for c in changes)
    assert any("M WR1" in a and "projects 0" in a for a in alerts)


# ---- rendering ---------------------------------------------------------------


def test_render_all_formats():
    league = make_league(base_roster("M"), free_agents=[P("FA Stud", "RB", 18)])
    report = build_report(league, narrative="Go get the stud.")
    md = to_markdown(report)
    for heading in ("## TL;DR", "## Analyst take", "## Waiver wire", "## Trade ideas", "## Standings", "FA Stud"):
        assert heading in md
    page = to_html(report)
    assert page.startswith("<!doctype html>") and "<style" not in page  # inline styles only (email-safe)
    assert json.loads(to_json(report))["waiver_moves"][0]["add"].startswith("FA Stud")


def test_league_round_trips_through_json():
    league = make_league(base_roster("M"))
    again = League.from_dict(json.loads(json.dumps(league.to_dict())))
    assert again.me.players[0].proj(5) == 20.0
    assert again.this_week[0].team_a == "t.1"


# ---- Yahoo parsing (payload shapes as returned by the Fantasy API) ------------


def test_parse_settings_detects_slots_and_half_ppr():
    data = {"fantasy_content": {"league": [
        {"league_key": "461.l.1", "name": "Office", "season": "2026", "current_week": 5, "end_week": 17},
        {"settings": [{
            "uses_faab": "1",
            "roster_positions": [
                {"roster_position": {"position": "QB", "count": 1}},
                {"roster_position": {"position": "WR", "count": 3}},
                {"roster_position": {"position": "W/R/T", "count": 1}},
                {"roster_position": {"position": "BN", "count": 6}},
            ],
            "stat_modifiers": {"stats": [{"stat": {"stat_id": 11, "value": "0.5"}}]},
        }]},
    ]}}
    s = parse_settings(data)
    assert s["slots"] == {"QB": 1, "WR": 3, "W/R/T": 1, "BN": 6}
    assert s["scoring"] == "half_ppr" and s["uses_faab"] and s["end_week"] == 17


def test_parse_teams_finds_me_and_standings():
    team = [
        [{"team_key": "461.l.1.t.3"}, {"name": "Hoang's Heroes"}, {"waiver_priority": 4}, {"faab_balance": "61"},
         {"managers": [{"manager": {"nickname": "Hoang", "guid": "G1", "is_current_login": "1"}}]}],
        {"team_standings": {"rank": "2", "outcome_totals": {"wins": "3", "losses": "1", "ties": 0},
                            "points_for": "480.5", "points_against": "400.1"}},
    ]
    data = {"fantasy_content": {"league": [{}, {"standings": [{"teams": {"0": {"team": team}, "count": 1}}]}]}}
    (t,) = parse_teams(data)
    assert t.is_me and t.name == "Hoang's Heroes" and t.manager == "Hoang"
    assert t.standing.record == "3-1" and t.standing.rank == 2 and t.faab_balance == 61.0 and t.waiver_priority == 4


def test_parse_roster_reads_slot_eligibility_and_status():
    player = [
        [{"player_key": "461.p.1"}, {"name": {"full": "Kenneth Walker III"}}, {"editorial_team_abbr": "Sea"},
         {"display_position": "RB"}, {"status": "Q"},
         {"eligible_positions": [{"position": "RB"}, {"position": "W/R/T"}]}],
        {"selected_position": [{"coverage_type": "week", "week": "5"}, {"position": "W/R/T"}]},
    ]
    data = {"fantasy_content": {"team": [[{"team_key": "t"}], {"roster": {"0": {"players": {"0": {"player": player}, "count": 1}}}}]}}
    (p,) = parse_roster(data)
    assert p == {"name": "Kenneth Walker III", "position": "RB", "eligible": ["RB"], "nfl_team": "SEA", "slot": "W/R/T", "status": "Q"}


def test_parse_scoreboard():
    def side(key, pts):
        return {"team": [[{"team_key": key}], {"team_points": {"total": pts}}]}

    data = {"fantasy_content": {"league": [{}, {"scoreboard": {"0": {"matchups": {
        "0": {"matchup": {"week": "4", "0": {"teams": {"0": side("a", "101.2"), "1": side("b", "99"), "count": 2}}}},
        "count": 1,
    }}}}]}}
    (m,) = parse_scoreboard(data, 4)
    assert (m.team_a, m.team_b, m.points_a, m.points_b) == ("a", "b", 101.2, 99.0)


def test_player_index_matching():
    index = PlayerIndex({
        "1": {"name": "Kenneth Walker", "position": "RB", "eligible": ["RB"], "team": "SEA"},
        "2": {"name": "Mike Williams", "position": "WR", "eligible": ["WR"], "team": "PIT"},
        "3": {"name": "Mike Williams", "position": "WR", "eligible": ["WR"], "team": "NYJ"},
        "SEA": {"name": "Seattle Seahawks", "position": "DEF", "eligible": ["DEF"], "team": "SEA"},
    })
    assert index.resolve("Kenneth Walker III", "RB", "SEA") == "1"
    assert index.resolve("Mike Williams", "WR", "NYJ") == "3"
    assert index.resolve("Seattle", "DEF", "Sea") == "SEA"
    assert norm_name("D.K. Metcalf") == "dk metcalf"
