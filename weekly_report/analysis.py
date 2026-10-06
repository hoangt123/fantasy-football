"""Turn a League into recommendations.

Every move is judged the same way: rebuild the roster with the move applied,
re-optimize the starting lineup for each remaining week, and compare total
projected starter points against the current roster. A bench upgrade that
never cracks the lineup is worth ~0; a fill-in for a starter's bye week is
worth exactly that week's difference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations
from typing import Dict, List, Optional, Sequence, Tuple

from .lineup import best_lineup, lineup_points, points_by_position, starting_slots
from .model import League, MatchupResult, Player, Team

SHORT_HORIZON = 3  # weeks that count as "near term" for waiver pickups
TRADE_POOL = 9  # top players per side considered for trade packages
SKILL_POSITIONS = ("QB", "RB", "WR", "TE")
# Pure lineup math values bench players at ~0, but bench depth is what covers
# injuries. Credit 10% of the rest-of-season projection of your first few
# backups at each position as insurance value, so the report won't cut a useful
# WR4 to stream a kicker, and won't value a QB3 who will never play.
DEPTH_WEIGHT = 0.10
USEFUL_BACKUPS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}


@dataclass
class WaiverMove:
    add: Player
    drop: Player
    gain_short: float  # starter points gained over the next SHORT_HORIZON weeks
    gain_ros: float  # ... over the rest of the season
    faab_bid: Optional[int] = None
    trending: bool = False


@dataclass
class TradeIdea:
    partner: Team
    give: List[Player]
    get: List[Player]
    my_gain: float
    their_gain: float
    my_drop: Optional[Player] = None
    their_drop: Optional[Player] = None
    rationale: str = ""
    my_value: float = 0.0  # my_gain plus depth credit; used for ranking
    their_value: float = 0.0

    @property
    def score(self) -> float:
        return self.my_value + 0.5 * min(self.their_value, self.my_value)


@dataclass
class LineupChange:
    start: Player
    bench: Optional[Player]
    gain: float


@dataclass
class Strength:
    total: float
    rank: int
    by_position: Dict[str, float]
    rank_by_position: Dict[str, int]


@dataclass
class MatchupPreview:
    opponent: Team
    my_projection: float
    their_projection: float
    win_probability: float


@dataclass
class Report:
    league: League
    strengths: Dict[str, Strength]
    needs: List[str]
    surpluses: List[str]
    waiver_moves: List[WaiverMove]
    best_available: Dict[str, List[Player]]
    trade_ideas: List[TradeIdea]
    lineup_changes: List[LineupChange]
    lineup_alerts: List[str]
    matchup: Optional[MatchupPreview]
    last_week: Optional[MatchupResult]
    narrative: str = ""
    headlines: List[str] = field(default_factory=list)


# ---- helpers -------------------------------------------------------------------


def _active(players: Sequence[Player]) -> List[Player]:
    return [p for p in players if not p.on_ir]


def _swap(
    roster: Sequence[Player], out: Sequence[Player], incoming: Sequence[Player], weeks: Sequence[int]
) -> Tuple[List[Player], Optional[Player]]:
    """Apply a swap and, if the active roster grew, release the least valuable extra player."""
    out_ids = {id(p) for p in out}
    new = [p for p in roster if id(p) not in out_ids] + list(incoming)
    dropped = None
    if len(_active(new)) > len(_active(roster)):
        incoming_ids = {id(p) for p in incoming}
        candidates = [p for p in _active(new) if id(p) not in incoming_ids]
        dropped = min(candidates, key=lambda p: p.total(weeks))
        new = [p for p in new if p is not dropped]
    return new, dropped


def depth_value(roster: Sequence[Player], slots: Dict[str, int], weeks: Sequence[int]) -> float:
    """Insurance value of the top backups at each position.

    Backups = players outside the season-long best lineup (ranked by rest-of-season total).
    """
    active = _active(roster)
    _, lineup = best_lineup(active, starting_slots(slots), weeks[0], value=lambda p: p.total(weeks))
    starters = {id(p) for _, p in lineup}
    total = 0.0
    for pos, backups in USEFUL_BACKUPS.items():
        ranked = sorted((p.total(weeks) for p in active if p.position == pos and id(p) not in starters), reverse=True)
        total += sum(ranked[:backups])
    return DEPTH_WEIGHT * total


def win_probability(mine: float, theirs: float) -> float:
    # Weekly team scores swing ~20% around projection; the difference of two such
    # scores has sd = sqrt(2) * that.
    sd = math.sqrt(2) * max(15.0, 0.2 * (mine + theirs) / 2)
    return 0.5 * (1 + math.erf((mine - theirs) / (sd * math.sqrt(2))))


# ---- sections ------------------------------------------------------------------


def team_strengths(league: League) -> Dict[str, Strength]:
    slots = starting_slots(league.slots)
    weeks = league.remaining_weeks
    raw = {}
    for team in league.teams:
        by_pos = points_by_position(team.players, slots, weeks)
        raw[team.key] = (sum(by_pos.values()), by_pos)
    positions = sorted({pos for _, by_pos in raw.values() for pos in by_pos} | set(SKILL_POSITIONS))

    def rank(values: Dict[str, float], key: str) -> int:
        return 1 + sum(1 for v in values.values() if v > values[key])

    totals = {k: v[0] for k, v in raw.items()}
    strengths = {}
    for key, (total, by_pos) in raw.items():
        ranks = {}
        for pos in positions:
            column = {k: v[1].get(pos, 0.0) for k, v in raw.items()}
            ranks[pos] = rank(column, key)
        strengths[key] = Strength(total=total, rank=rank(totals, key), by_position=by_pos, rank_by_position=ranks)
    return strengths


def needs_and_surpluses(league: League, strengths: Dict[str, Strength]) -> Tuple[List[str], List[str]]:
    mine = strengths[league.me.key]
    n = len(league.teams)
    positions = [p for p in SKILL_POSITIONS if p in mine.rank_by_position and p in league.slots]
    needs = sorted((p for p in positions if mine.rank_by_position[p] > n * 0.6), key=lambda p: -mine.rank_by_position[p])
    surpluses = sorted((p for p in positions if mine.rank_by_position[p] <= max(2, n * 0.3)), key=lambda p: mine.rank_by_position[p])
    return needs, surpluses


def lineup_check(league: League) -> Tuple[List[LineupChange], List[str]]:
    me = league.me
    slots = starting_slots(league.slots)
    week = league.week
    _, optimal = best_lineup(me.players, slots, week)
    current_starters = [p for p in me.players if p.slot and p.slot not in ("BN", "IR", "IR+", "NA")]
    current_ids = {id(p) for p in current_starters}
    optimal_ids = {id(p) for _, p in optimal}
    # Diff by slot so each "start X over Y" is a swap in the same slot type; a player
    # merely shuffling between starting slots (WR -> W/R/T) is not a change.
    ins = [(slot, p) for slot, p in optimal if id(p) not in current_ids]
    outs = [(p.slot, p) for p in current_starters if id(p) not in optimal_ids]
    changes = []
    for slot, player in sorted(ins, key=lambda item: -item[1].proj(week)):
        match = next((o for o in outs if o[0] == slot), None) or (outs[0] if outs else None)
        benched = None
        if match is not None:
            outs.remove(match)
            benched = match[1]
        changes.append(LineupChange(player, benched, player.proj(week) - (benched.proj(week) if benched else 0.0)))
    changes.sort(key=lambda c: -c.gain)

    alerts = []
    for player in current_starters:
        if player.proj(week) == 0:
            reason = "on bye" if player.status in ("", "Healthy") else f"status {player.status}"
            alerts.append(f"{player.label} is in your lineup but projects 0 ({reason}).")
        elif player.status in ("O", "IR", "D", "PUP-R", "SUSP", "Out", "Doubtful"):
            alerts.append(f"{player.label} is starting with status {player.status}.")
        elif player.status in ("Q", "Questionable"):
            alerts.append(f"{player.label} is Questionable — check news before kickoff.")
    return changes, alerts


def matchup_preview(league: League) -> Optional[MatchupPreview]:
    me = league.me
    for game in league.this_week:
        if me.key in (game.team_a, game.team_b):
            opponent = league.team(game.team_b if game.team_a == me.key else game.team_a)
            slots = starting_slots(league.slots)
            mine = best_lineup(me.players, slots, league.week)[0]
            theirs = best_lineup(opponent.players, slots, league.week)[0]
            return MatchupPreview(opponent, mine, theirs, win_probability(mine, theirs))
    return None


def last_week_result(league: League) -> Optional[MatchupResult]:
    for game in league.last_week:
        if league.me.key in (game.team_a, game.team_b):
            return game
    return None


def waiver_moves(league: League, max_moves: int = 3, candidates_per_position: int = 12) -> List[WaiverMove]:
    """Greedy sequence of add/drop pairs, each re-evaluated after the previous one."""
    slots = starting_slots(league.slots)
    weeks = league.remaining_weeks
    short = weeks[:SHORT_HORIZON]
    me = league.me
    roster = list(me.players)

    by_pos: Dict[str, List[Player]] = {}
    for player in league.free_agents:
        by_pos.setdefault(player.position, []).append(player)
    candidates = []
    for players in by_pos.values():
        seen = set()
        for player in sorted(players, key=lambda p: -p.total(weeks))[:candidates_per_position] + sorted(
            players, key=lambda p: -p.total(short)
        )[: candidates_per_position // 2]:
            if id(player) not in seen:
                seen.add(id(player))
                candidates.append(player)

    my_total = lineup_points(roster, slots, weeks)
    moves: List[WaiverMove] = []
    taken = set()
    for _ in range(max_moves):
        base_short = lineup_points(roster, slots, short)
        base_ros = lineup_points(roster, slots, weeks)
        base_depth = depth_value(roster, league.slots, weeks)
        best: Optional[WaiverMove] = None
        best_value = 1.5  # minimum worthwhile move
        for add in candidates:
            if id(add) in taken:
                continue
            for drop in _active(roster):
                new = [p for p in roster if p is not drop] + [add]
                gain_short = lineup_points(new, slots, short) - base_short
                gain_ros = lineup_points(new, slots, weeks) - base_ros
                value = gain_short + gain_ros + depth_value(new, league.slots, weeks) - base_depth
                value -= 1e-6 * drop.total(weeks)  # on ties, cut the weaker player
                if value > best_value:
                    best, best_value = WaiverMove(add, drop, gain_short, gain_ros), value
        if best is None:
            break
        best.trending = best.add.trending_adds > 0
        if league.uses_faab and me.faab_balance:
            share = min(0.5, max(0.01, 4 * best.gain_ros / max(my_total, 1.0)))
            best.faab_bid = max(1, int(round(me.faab_balance * share)))
        moves.append(best)
        taken.add(id(best.add))
        roster = [p for p in roster if p is not best.drop] + [best.add]
    return moves


def best_available(league: League, per_position: int = 4) -> Dict[str, List[Player]]:
    weeks = league.remaining_weeks
    grouped: Dict[str, List[Player]] = {}
    for player in league.free_agents:
        grouped.setdefault(player.position, []).append(player)
    order = [p for p in ("QB", "RB", "WR", "TE", "K", "DEF") if p in grouped]
    return {pos: sorted(grouped[pos], key=lambda p: -p.total(weeks))[:per_position] for pos in order}


def trade_ideas(league: League, strengths: Dict[str, Strength], max_ideas: int = 5) -> List[TradeIdea]:
    slots = starting_slots(league.slots)
    weeks = league.remaining_weeks
    me = league.me
    base_me = lineup_points(me.players, slots, weeks)
    depth_me = depth_value(me.players, league.slots, weeks)
    threshold = max(5.0, 0.005 * base_me)

    def pool(team: Team) -> List[Player]:
        tradeable = [p for p in _active(team.players) if p.position in SKILL_POSITIONS]
        return sorted(tradeable, key=lambda p: -p.total(weeks))[:TRADE_POOL]

    mine = pool(me)
    ideas: List[TradeIdea] = []
    for partner in league.teams:
        if partner.is_me:
            continue
        theirs = pool(partner)
        base_them = lineup_points(partner.players, slots, weeks)
        depth_them = depth_value(partner.players, league.slots, weeks)
        packages = [([g], [t]) for g in mine for t in theirs]
        packages += [(list(gs), [t]) for gs in combinations(mine, 2) for t in theirs]
        packages += [([g], list(ts)) for g in mine for ts in combinations(theirs, 2)]
        found = []
        for give, get in packages:
            give_value = sum(p.total(weeks) for p in give)
            get_value = sum(p.total(weeks) for p in get)
            # Managers judge by raw value: they must get back at least 85% of what they give.
            if give_value < 0.85 * get_value:
                continue
            my_new, my_drop = _swap(me.players, give, get, weeks)
            my_gain = lineup_points(my_new, slots, weeks) - base_me
            my_value = my_gain + depth_value(my_new, league.slots, weeks) - depth_me
            if my_gain <= 0 or my_value < threshold:
                continue
            their_new, their_drop = _swap(partner.players, get, give, weeks)
            their_gain = lineup_points(their_new, slots, weeks) - base_them
            their_value = their_gain + depth_value(their_new, league.slots, weeks) - depth_them
            if their_gain < 0 or their_value < 0:
                continue
            found.append(
                TradeIdea(partner, give, get, my_gain, their_gain, my_drop, their_drop, my_value=my_value, their_value=their_value)
            )
        found.sort(key=lambda idea: -idea.score)
        ideas.extend(found[:2])

    ideas.sort(key=lambda idea: -idea.score)
    picked: List[TradeIdea] = []
    uses: Dict[int, int] = {}
    for idea in ideas:
        if any(uses.get(id(p), 0) >= 2 for p in idea.give) or any(uses.get(id(p), 0) >= 1 for p in idea.get):
            continue
        for p in idea.give + idea.get:
            uses[id(p)] = uses.get(id(p), 0) + 1
        idea.rationale = _trade_rationale(league, strengths, idea)
        picked.append(idea)
        if len(picked) == max_ideas:
            break
    return picked


def _trade_rationale(league: League, strengths: Dict[str, Strength], idea: TradeIdea) -> str:
    n = len(league.teams)
    mine = strengths[league.me.key].rank_by_position
    theirs = strengths[idea.partner.key].rank_by_position
    get_pos = sorted({p.position for p in idea.get})
    give_pos = sorted({p.position for p in idea.give})
    parts = []
    for pos in get_pos:
        if pos not in give_pos:
            parts.append(f"your {pos} group ranks {mine.get(pos, n)}/{n}")
    for pos in give_pos:
        if pos not in get_pos and theirs.get(pos, 0) > n / 2:
            parts.append(f"their {pos} group ranks {theirs[pos]}/{n}")
    if get_pos == give_pos and len(idea.get) == len(idea.give):
        parts.append("same-position swap whose schedule and bye weeks fit your roster better")
    if len(idea.get) == 1 and len(idea.give) == 2:
        parts.append("2-for-1 consolidation frees a roster spot for a waiver add")
    elif len(idea.get) == 2 and len(idea.give) == 1:
        parts.append("adds depth for byes and injuries")
    if not parts:
        parts.append("both starting lineups project higher after the swap")
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."


def build_report(league: League, narrative: str = "") -> Report:
    strengths = team_strengths(league)
    needs, surpluses = needs_and_surpluses(league, strengths)
    changes, alerts = lineup_check(league)
    report = Report(
        league=league,
        strengths=strengths,
        needs=needs,
        surpluses=surpluses,
        waiver_moves=waiver_moves(league),
        best_available=best_available(league),
        trade_ideas=trade_ideas(league, strengths),
        lineup_changes=changes,
        lineup_alerts=alerts,
        matchup=matchup_preview(league),
        last_week=last_week_result(league),
        narrative=narrative,
    )
    report.headlines = _headlines(report)
    return report


def _headlines(report: Report) -> List[str]:
    lines = []
    if report.matchup:
        m = report.matchup
        lines.append(
            f"Week {report.league.week}: vs {m.opponent.name}, projected {m.my_projection:.1f}–{m.their_projection:.1f} "
            f"({m.win_probability:.0%} to win)."
        )
    if report.lineup_changes:
        c = report.lineup_changes[0]
        lines.append(f"Lineup: start {c.start.name}" + (f" over {c.bench.name}" if c.bench else "") + f" (+{c.gain:.1f}).")
    if report.waiver_moves:
        w = report.waiver_moves[0]
        lines.append(f"Top waiver add: {w.add.label}, drop {w.drop.name} (+{w.gain_ros:.1f} pts rest of season).")
    if report.trade_ideas:
        t = report.trade_ideas[0]
        lines.append(
            f"Trade target: {', '.join(p.name for p in t.get)} from {t.partner.name} for "
            f"{', '.join(p.name for p in t.give)} (+{t.my_gain:.1f} for you, +{t.their_gain:.1f} for them)."
        )
    if report.needs:
        lines.append("Biggest needs: " + ", ".join(report.needs) + ".")
    return lines
