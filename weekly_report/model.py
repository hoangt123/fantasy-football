"""Plain data containers shared by the fetchers, the analysis and the renderers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


@dataclass
class Player:
    name: str
    position: str  # primary fantasy position: QB, RB, WR, TE, K, DEF
    nfl_team: str = ""
    eligible: List[str] = field(default_factory=list)  # every position he can fill
    slot: str = ""  # current Yahoo roster slot (QB, BN, IR, W/R/T, ...); "" for free agents
    status: str = ""  # injury designation (Q, D, O, IR, PUP, ...)
    sleeper_id: str = ""
    # Projected fantasy points keyed by week, for the upcoming week through the
    # league's last fantasy week. Missing week means 0 (bye, injured, not projected).
    weekly: Dict[int, float] = field(default_factory=dict)
    season_points: float = 0.0
    games_played: int = 0
    trending_adds: int = 0  # Sleeper adds in the last 48h

    def __post_init__(self) -> None:
        if not self.eligible:
            self.eligible = [self.position]
        # JSON round-trips turn int keys into strings.
        self.weekly = {int(k): float(v) for k, v in self.weekly.items()}

    def proj(self, week: int) -> float:
        return self.weekly.get(week, 0.0)

    def total(self, weeks: List[int]) -> float:
        return sum(self.weekly.get(w, 0.0) for w in weeks)

    @property
    def ppg(self) -> float:
        return self.season_points / self.games_played if self.games_played else 0.0

    @property
    def on_ir(self) -> bool:
        return self.slot in ("IR", "IR+")

    @property
    def label(self) -> str:
        return f"{self.name} ({self.position}, {self.nfl_team or 'FA'})"


@dataclass
class Standing:
    rank: int = 0
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: float = 0.0
    points_against: float = 0.0

    @property
    def record(self) -> str:
        return f"{self.wins}-{self.losses}" + (f"-{self.ties}" if self.ties else "")


@dataclass
class Team:
    key: str
    name: str
    manager: str = ""
    is_me: bool = False
    players: List[Player] = field(default_factory=list)
    standing: Standing = field(default_factory=Standing)
    faab_balance: Optional[float] = None
    waiver_priority: Optional[int] = None


@dataclass
class MatchupResult:
    week: int
    team_a: str  # team keys
    team_b: str
    points_a: float = 0.0
    points_b: float = 0.0


@dataclass
class League:
    key: str
    name: str
    season: int
    week: int  # the upcoming week the report is for
    end_week: int  # last fantasy week (incl. playoffs)
    scoring: str  # "ppr", "half_ppr" or "std"
    slots: Dict[str, int]  # {"QB": 1, "RB": 2, "W/R/T": 1, "BN": 6, "IR": 1, ...}
    teams: List[Team]
    free_agents: List[Player] = field(default_factory=list)
    uses_faab: bool = False
    last_week: List[MatchupResult] = field(default_factory=list)
    this_week: List[MatchupResult] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)  # data-quality caveats for the report

    @property
    def me(self) -> Team:
        for team in self.teams:
            if team.is_me:
                return team
        raise LookupError("Could not identify your team in this league")

    def team(self, key: str) -> Team:
        return next(t for t in self.teams if t.key == key)

    @property
    def remaining_weeks(self) -> List[int]:
        return list(range(self.week, self.end_week + 1))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "League":
        def player(d: dict) -> Player:
            return Player(**d)

        teams = [
            Team(
                **{
                    **t,
                    "players": [player(p) for p in t["players"]],
                    "standing": Standing(**t["standing"]),
                }
            )
            for t in data["teams"]
        ]
        return cls(
            **{
                **data,
                "teams": teams,
                "free_agents": [player(p) for p in data.get("free_agents", [])],
                "last_week": [MatchupResult(**m) for m in data.get("last_week", [])],
                "this_week": [MatchupResult(**m) for m in data.get("this_week", [])],
            }
        )
