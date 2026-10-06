"""Normalization used to match Yahoo players to Sleeper players."""

from __future__ import annotations

import re
import unicodedata

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Yahoo / legacy abbreviations -> Sleeper abbreviations.
_TEAM_ALIASES = {
    "JAC": "JAX",
    "WSH": "WAS",
    "LA": "LAR",
    "STL": "LAR",
    "OAK": "LV",
    "SD": "LAC",
}

# Yahoo shows defenses by city or full name; Sleeper keys them by abbreviation.
_DEF_NAMES = {
    "arizona": "ARI", "atlanta": "ATL", "baltimore": "BAL", "buffalo": "BUF",
    "carolina": "CAR", "chicago": "CHI", "cincinnati": "CIN", "cleveland": "CLE",
    "dallas": "DAL", "denver": "DEN", "detroit": "DET", "green bay": "GB",
    "houston": "HOU", "indianapolis": "IND", "jacksonville": "JAX", "kansas city": "KC",
    "las vegas": "LV", "los angeles chargers": "LAC", "los angeles rams": "LAR",
    "miami": "MIA", "minnesota": "MIN", "new england": "NE", "new orleans": "NO",
    "new york giants": "NYG", "new york jets": "NYJ", "philadelphia": "PHI",
    "pittsburgh": "PIT", "san francisco": "SF", "seattle": "SEA", "tampa bay": "TB",
    "tennessee": "TEN", "washington": "WAS",
}


def norm_team(abbr: str) -> str:
    abbr = (abbr or "").strip().upper()
    return _TEAM_ALIASES.get(abbr, abbr)


def norm_name(name: str) -> str:
    """'Kenneth Walker III' -> 'kenneth walker'; 'D.K. Metcalf' -> 'dk metcalf'."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[.'’`]", "", text.lower())
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w and w not in _SUFFIXES]
    return " ".join(words)


def defense_team(name: str, team_abbr: str = "") -> str:
    """Resolve a Yahoo defense entry to a Sleeper team abbreviation."""
    if team_abbr:
        return norm_team(team_abbr)
    lowered = (name or "").lower()
    # Longest key first so "los angeles rams" beats a bare city match.
    for city in sorted(_DEF_NAMES, key=len, reverse=True):
        if lowered.startswith(city):
            return _DEF_NAMES[city]
    return ""


def normalize_position(pos: str) -> str:
    pos = (pos or "").upper().strip()
    return {"D/ST": "DEF", "DST": "DEF", "PK": "K"}.get(pos, pos)
