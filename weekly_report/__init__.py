"""Weekly fantasy football report: waiver and trade recommendations.

Pulls league structure (rosters, standings, matchups, roster slots, scoring)
from Yahoo and week-by-week projections from Sleeper, then scores every
candidate move by how much it changes your *optimal starting lineup* over the
coming weeks. Bye weeks, injuries and positional scarcity fall out of that
naturally, because a player only adds value when he would actually start.

Run ``python -m weekly_report --help`` for usage.
"""

__all__ = ["model", "analysis", "render", "deliver"]
