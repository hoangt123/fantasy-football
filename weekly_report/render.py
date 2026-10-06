"""Render a Report as Markdown, HTML (email- and web-safe) or JSON."""

from __future__ import annotations

import datetime as dt
import html
import json
from dataclasses import asdict
from typing import List, Optional, Sequence

from .analysis import Report
from .lineup import best_lineup, starting_slots
from .model import Player

SCORING_LABEL = {"ppr": "PPR", "half_ppr": "Half-PPR", "std": "Standard"}


def _num(value: float, signed: bool = False) -> str:
    return f"{value:+.1f}" if signed else f"{value:.1f}"


def _per_week(report: Report, total: float) -> float:
    return total / max(1, len(report.league.remaining_weeks))


def _title(report: Report) -> str:
    return f"Week {report.league.week} Report — {report.league.me.name}"


def _subtitle(report: Report) -> str:
    lg = report.league
    return f"{lg.name} · {lg.season} · {SCORING_LABEL.get(lg.scoring, lg.scoring)} · generated {dt.date.today():%b %d, %Y}"


def _last_week_line(report: Report) -> Optional[str]:
    game = report.last_week
    if not game:
        return None
    me = report.league.me.key
    mine, theirs = (game.points_a, game.points_b) if game.team_a == me else (game.points_b, game.points_a)
    opponent = report.league.team(game.team_b if game.team_a == me else game.team_a)
    result = "Win" if mine > theirs else "Loss" if mine < theirs else "Tie"
    return f"Week {game.week}: {_num(mine)} – {_num(theirs)} vs {opponent.name} → {result}"


def _matchup_rows(report: Report) -> List[List[str]]:
    if not report.matchup:
        return []
    slots = starting_slots(report.league.slots)
    week = report.league.week
    _, mine = best_lineup(report.league.me.players, slots, week)
    _, theirs = best_lineup(report.matchup.opponent.players, slots, week)
    rows = []
    for (slot, a), (_, b) in zip(mine, theirs):
        rows.append([slot, a.name, _num(a.proj(week)), b.name, _num(b.proj(week))])
    return rows


def _player_row(report: Report, player: Player) -> List[str]:
    weeks = report.league.remaining_weeks
    return [
        player.name,
        player.nfl_team or "FA",
        _num(player.proj(report.league.week)),
        _num(_per_week(report, player.total(weeks))),
        _num(player.ppg) if player.games_played else "—",
        "🔥" if player.trending_adds else "",
    ]


def _standings_rows(report: Report) -> List[List[str]]:
    teams = sorted(report.league.teams, key=lambda t: (t.standing.rank or 99, -t.standing.points_for))
    return [
        [str(t.standing.rank or "—"), ("★ " if t.is_me else "") + t.name, t.standing.record, _num(t.standing.points_for), _num(t.standing.points_against)]
        for t in teams
    ]


def _strength_rows(report: Report) -> List[List[str]]:
    mine = report.strengths[report.league.me.key]
    n = len(report.league.teams)
    rows = []
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        if pos in mine.by_position or pos in mine.rank_by_position:
            rows.append([pos, _num(_per_week(report, mine.by_position.get(pos, 0.0))), f"{mine.rank_by_position.get(pos, n)} / {n}"])
    rows.append(["Total", _num(_per_week(report, mine.total)), f"{mine.rank} / {n}"])
    return rows


def _method_notes(report: Report) -> List[str]:
    lg = report.league
    weeks = lg.remaining_weeks
    notes = [
        f"Projections: Sleeper week-by-week {SCORING_LABEL.get(lg.scoring, lg.scoring)} projections for weeks "
        f"{weeks[0]}–{weeks[-1]}. 'Gain' = extra points your best possible starting lineup scores across those weeks "
        "(re-optimized every week, so byes and injuries count).",
        "Trade ideas require that both teams' lineups improve and that the other manager gets back at least 85% of the raw projected value they give up.",
    ]
    return notes + list(lg.notes)


# ---- Markdown -------------------------------------------------------------------


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(cell.replace("|", "/") for cell in row) + " |" for row in rows]
    return "\n".join(out)


def to_markdown(report: Report) -> str:
    lg = report.league
    week = lg.week
    parts = [f"# 🏈 {_title(report)}", f"_{_subtitle(report)}_", "## TL;DR"]
    parts.append("\n".join(f"- {line}" for line in report.headlines) or "- Nothing urgent this week.")

    if report.narrative.strip():
        parts += ["## Analyst take", report.narrative.strip()]

    last = _last_week_line(report)
    if last:
        parts += ["## Last week", last]

    if report.matchup:
        m = report.matchup
        parts += [
            f"## Week {week} matchup: vs {m.opponent.name}",
            f"Projected **{_num(m.my_projection)} – {_num(m.their_projection)}** · win probability **{m.win_probability:.0%}** (best lineups)",
            _md_table(["Slot", "You", "Proj", m.opponent.name, "Proj"], _matchup_rows(report)),
        ]

    parts.append("## Lineup check")
    if report.lineup_changes:
        parts.append(
            "\n".join(
                f"- Start **{c.start.label}** ({_num(c.start.proj(week))})"
                + (f" over {c.bench.name} ({_num(c.bench.proj(week))})" if c.bench else "")
                + f" → {_num(c.gain, True)}"
                for c in report.lineup_changes
            )
        )
    else:
        parts.append("Your current lineup is already the best projected lineup.")
    if report.lineup_alerts:
        parts.append("\n".join(f"- ⚠️ {a}" for a in report.lineup_alerts))

    parts.append("## Waiver wire")
    if report.waiver_moves:
        headers = ["#", "Add", "Drop", f"Next {min(3, len(lg.remaining_weeks))} wks", "Rest of season"]
        if any(m.faab_bid for m in report.waiver_moves):
            headers.append("FAAB bid")
        rows = []
        for i, move in enumerate(report.waiver_moves, 1):
            row = [str(i), move.add.label + (" 🔥" if move.trending else ""), move.drop.label, _num(move.gain_short, True), _num(move.gain_ros, True)]
            if "FAAB bid" in headers:
                row.append(f"${move.faab_bid}" if move.faab_bid else "—")
            rows.append(row)
        parts += ["### Recommended moves (in order)", _md_table(headers, rows)]
        me = lg.me
        extra = []
        if me.waiver_priority:
            extra.append(f"waiver priority #{me.waiver_priority}")
        if me.faab_balance is not None:
            extra.append(f"FAAB remaining ${me.faab_balance:.0f}")
        if extra:
            parts.append("_You: " + ", ".join(extra) + ". FAAB bids are a rough guide scaled to the projected gain._")
    else:
        parts.append("No free agent improves your projected lineup right now.")
    if report.best_available:
        parts.append("### Best available by position")
        rows = []
        for pos, players in report.best_available.items():
            rows += [[pos] + _player_row(report, p) for p in players]
        parts.append(_md_table(["Pos", "Player", "Team", f"Wk {week}", "ROS/wk", "PPG", "Trend"], rows))

    parts.append("## Trade ideas")
    if report.trade_ideas:
        for i, idea in enumerate(report.trade_ideas, 1):
            block = [
                f"### {i}. {', '.join(p.name for p in idea.get)} from {idea.partner.name}"
                + (f" ({idea.partner.manager})" if idea.partner.manager else ""),
                f"- **You give:** {', '.join(p.label for p in idea.give)}",
                f"- **You get:** {', '.join(p.label for p in idea.get)}",
                f"- **Impact:** {_num(idea.my_gain, True)} starter pts for you ({_num(_per_week(report, idea.my_gain), True)}/wk), "
                f"{_num(idea.their_gain, True)} for them",
                f"- **Why:** {idea.rationale}",
            ]
            if idea.my_drop:
                block.append(f"- You'd need to drop {idea.my_drop.label} to make room.")
            parts.append("\n".join(block))
    else:
        parts.append("No trade found that clearly helps both sides this week.")

    parts.append("## Roster strength")
    parts.append(_md_table(["Position", "Your starters (pts/wk)", "League rank"], _strength_rows(report)))
    if report.needs or report.surpluses:
        parts.append(
            f"Needs: **{', '.join(report.needs) or 'none'}** · Surplus to deal from: **{', '.join(report.surpluses) or 'none'}**"
        )

    parts += ["## Standings", _md_table(["Rk", "Team", "W-L", "PF", "PA"], _standings_rows(report))]
    parts += ["## Notes", "\n".join(f"- {n}" for n in _method_notes(report))]
    return "\n\n".join(parts) + "\n"


# ---- HTML -----------------------------------------------------------------------
# Inline styles only: email clients drop <style> blocks and external CSS.

INK = "#1f2933"
MUTED = "#616e7c"
LINE = "#e4e7eb"
ACCENT = "#0b7a53"
SOFT = "#f5f7fa"

TABLE = f"width:100%;border-collapse:collapse;font-size:14px;margin:8px 0 4px"
TH = f"text-align:left;padding:6px 8px;border-bottom:2px solid {LINE};color:{MUTED};font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em"
TD = f"padding:6px 8px;border-bottom:1px solid {LINE};vertical-align:top"
H2 = f"font-size:18px;margin:28px 0 8px;color:{INK}"
H3 = f"font-size:15px;margin:18px 0 6px;color:{INK}"
P = f"margin:6px 0;line-height:1.5"


def _e(text: str) -> str:
    return html.escape(str(text))


def _h_table(headers: Sequence[str], rows: Sequence[Sequence[str]], highlight: Optional[Sequence[bool]] = None, numeric_from: int = 99) -> str:
    head = "".join(
        f'<th style="{TH}{";text-align:right" if i >= numeric_from else ""}">{_e(h)}</th>' for i, h in enumerate(headers)
    )
    body = []
    for r, row in enumerate(rows):
        bg = f"background:{SOFT};font-weight:600;" if highlight and highlight[r] else ""
        cells = "".join(
            f'<td style="{TD};{bg}{"text-align:right;font-variant-numeric:tabular-nums" if i >= numeric_from else ""}">{_e(c)}</td>'
            for i, c in enumerate(row)
        )
        body.append(f"<tr>{cells}</tr>")
    return f'<div style="overflow-x:auto"><table role="presentation" style="{TABLE}"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _h_list(items: Sequence[str]) -> str:
    return "<ul style='margin:6px 0;padding-left:20px;line-height:1.6'>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"


def _gain(value: float) -> str:
    color = ACCENT if value > 0 else MUTED
    return f'<b style="color:{color}">{_num(value, True)}</b>'


def to_html(report: Report) -> str:
    lg = report.league
    week = lg.week
    out = [
        f'<h1 style="font-size:24px;margin:0 0 4px;color:{INK}">🏈 {_e(_title(report))}</h1>',
        f'<p style="margin:0 0 16px;color:{MUTED};font-size:13px">{_e(_subtitle(report))}</p>',
    ]
    if report.headlines:
        out.append(
            f'<div style="background:{SOFT};border-left:4px solid {ACCENT};padding:10px 14px;border-radius:6px">'
            f'<div style="font-weight:700;margin-bottom:4px">TL;DR</div>{_h_list([_e(h) for h in report.headlines])}</div>'
        )
    if report.narrative.strip():
        paragraphs = [p.strip() for p in report.narrative.strip().split("\n\n") if p.strip()]
        out.append(f'<h2 style="{H2}">Analyst take</h2>' + "".join(f'<p style="{P}">{_e(p)}</p>' for p in paragraphs))

    last = _last_week_line(report)
    if last:
        out.append(f'<h2 style="{H2}">Last week</h2><p style="{P}">{_e(last)}</p>')

    if report.matchup:
        m = report.matchup
        out.append(f'<h2 style="{H2}">Week {week} matchup: vs {_e(m.opponent.name)}</h2>')
        out.append(
            f'<p style="{P}">Projected <b>{_num(m.my_projection)} – {_num(m.their_projection)}</b> · '
            f'win probability <b style="color:{ACCENT if m.win_probability >= .5 else "#b42318"}">{m.win_probability:.0%}</b></p>'
        )
        out.append(_h_table(["Slot", "You", "Proj", m.opponent.name, "Proj"], _matchup_rows(report)))

    out.append(f'<h2 style="{H2}">Lineup check</h2>')
    if report.lineup_changes:
        out.append(
            _h_list(
                [
                    f"Start <b>{_e(c.start.label)}</b> ({_num(c.start.proj(week))})"
                    + (f" over {_e(c.bench.name)} ({_num(c.bench.proj(week))})" if c.bench else "")
                    + f" → {_gain(c.gain)}"
                    for c in report.lineup_changes
                ]
            )
        )
    else:
        out.append(f'<p style="{P}">Your current lineup is already the best projected lineup.</p>')
    if report.lineup_alerts:
        out.append(_h_list([f"⚠️ {_e(a)}" for a in report.lineup_alerts]))

    out.append(f'<h2 style="{H2}">Waiver wire</h2>')
    if report.waiver_moves:
        faab = any(m.faab_bid for m in report.waiver_moves)
        headers = ["#", "Add", "Drop", "Next 3 wks", "Rest of season"] + (["FAAB"] if faab else [])
        rows = []
        for i, move in enumerate(report.waiver_moves, 1):
            row = [str(i), move.add.label + (" 🔥" if move.trending else ""), move.drop.label, _num(move.gain_short, True), _num(move.gain_ros, True)]
            if faab:
                row.append(f"${move.faab_bid}" if move.faab_bid else "—")
            rows.append(row)
        out.append(f'<h3 style="{H3}">Recommended moves (in order)</h3>' + _h_table(headers, rows, numeric_from=3))
    else:
        out.append(f'<p style="{P}">No free agent improves your projected lineup right now.</p>')
    if report.best_available:
        rows = [[pos] + _player_row(report, p) for pos, players in report.best_available.items() for p in players]
        out.append(f'<h3 style="{H3}">Best available by position</h3>')
        out.append(_h_table(["Pos", "Player", "Team", f"Wk {week}", "ROS/wk", "PPG", ""], rows, numeric_from=3))

    out.append(f'<h2 style="{H2}">Trade ideas</h2>')
    if report.trade_ideas:
        for i, idea in enumerate(report.trade_ideas, 1):
            manager = f" ({_e(idea.partner.manager)})" if idea.partner.manager else ""
            lines = [
                f"<b>You give:</b> {_e(', '.join(p.label for p in idea.give))}",
                f"<b>You get:</b> {_e(', '.join(p.label for p in idea.get))}",
                f"<b>Impact:</b> {_gain(idea.my_gain)} starter pts for you ({_num(_per_week(report, idea.my_gain), True)}/wk), {_gain(idea.their_gain)} for them",
                f"<b>Why:</b> {_e(idea.rationale)}",
            ]
            if idea.my_drop:
                lines.append(f"You'd need to drop {_e(idea.my_drop.label)} to make room.")
            out.append(
                f'<div style="border:1px solid {LINE};border-radius:8px;padding:10px 14px;margin:10px 0">'
                f'<div style="font-weight:700">{i}. {_e(", ".join(p.name for p in idea.get))} from {_e(idea.partner.name)}{manager}</div>'
                f"{_h_list(lines)}</div>"
            )
    else:
        out.append(f'<p style="{P}">No trade found that clearly helps both sides this week.</p>')

    out.append(f'<h2 style="{H2}">Roster strength</h2>')
    rows = _strength_rows(report)
    out.append(_h_table(["Position", "Your starters (pts/wk)", "League rank"], rows, highlight=[r[0] == "Total" for r in rows], numeric_from=1))
    if report.needs or report.surpluses:
        out.append(
            f'<p style="{P}">Needs: <b>{_e(", ".join(report.needs) or "none")}</b> · '
            f'Surplus to deal from: <b>{_e(", ".join(report.surpluses) or "none")}</b></p>'
        )

    teams_sorted = sorted(lg.teams, key=lambda t: (t.standing.rank or 99, -t.standing.points_for))
    out.append(f'<h2 style="{H2}">Standings</h2>')
    out.append(_h_table(["Rk", "Team", "W-L", "PF", "PA"], _standings_rows(report), highlight=[t.is_me for t in teams_sorted], numeric_from=3))

    out.append(
        f'<h2 style="{H2}">Notes</h2><div style="color:{MUTED};font-size:12px">'
        + _h_list([_e(n) for n in _method_notes(report)])
        + "</div>"
    )

    body = "\n".join(out)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(_title(report))}</title></head>
<body style="margin:0;background:#ffffff;color:{INK};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
<div style="max-width:760px;margin:0 auto;padding:24px 16px">
{body}
</div></body></html>
"""


# ---- JSON -----------------------------------------------------------------------


def to_json(report: Report) -> str:
    """Machine-readable dump (also what the optional Claude 'analyst take' step reads)."""

    def players(ps: Sequence[Player]) -> List[str]:
        return [p.label for p in ps]

    weeks = report.league.remaining_weeks
    data = {
        "league": {
            "key": report.league.key,
            "name": report.league.name,
            "season": report.league.season,
            "week": report.league.week,
            "my_team": report.league.me.name,
            "scoring": report.league.scoring,
        },
        "headlines": report.headlines,
        "needs": report.needs,
        "surpluses": report.surpluses,
        "strengths": {k: asdict(v) for k, v in report.strengths.items()},
        "lineup_changes": [
            {"start": c.start.label, "bench": c.bench.label if c.bench else None, "gain": round(c.gain, 1)} for c in report.lineup_changes
        ],
        "lineup_alerts": report.lineup_alerts,
        "waiver_moves": [
            {
                "add": m.add.label,
                "drop": m.drop.label,
                "gain_next_weeks": round(m.gain_short, 1),
                "gain_rest_of_season": round(m.gain_ros, 1),
                "faab_bid": m.faab_bid,
                "trending": m.trending,
            }
            for m in report.waiver_moves
        ],
        "best_available": {
            pos: [{"player": p.label, "next_week": p.proj(report.league.week), "ros_total": round(p.total(weeks), 1)} for p in ps]
            for pos, ps in report.best_available.items()
        },
        "trade_ideas": [
            {
                "partner": t.partner.name,
                "give": players(t.give),
                "get": players(t.get),
                "my_gain": round(t.my_gain, 1),
                "their_gain": round(t.their_gain, 1),
                "my_drop": t.my_drop.label if t.my_drop else None,
                "rationale": t.rationale,
            }
            for t in report.trade_ideas
        ],
        "matchup": (
            {
                "opponent": report.matchup.opponent.name,
                "my_projection": round(report.matchup.my_projection, 1),
                "their_projection": round(report.matchup.their_projection, 1),
                "win_probability": round(report.matchup.win_probability, 3),
            }
            if report.matchup
            else None
        ),
    }
    return json.dumps(data, indent=2, default=str)
