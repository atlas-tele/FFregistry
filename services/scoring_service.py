"""Pure scoring and ranking logic (no database access)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Iterable, List

DEFAULT_PLACEMENT: Dict[int, int] = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}
DEFAULT_KILL_POINTS = 1
MAX_KILLS = 99


@dataclass(frozen=True)
class Rules:
    placement: Dict[int, int]
    kill_points: int = DEFAULT_KILL_POINTS

    def to_json(self) -> str:
        return json.dumps({str(k): v for k, v in sorted(self.placement.items())})

    @staticmethod
    def from_json(text: str, kill_points: int) -> "Rules":
        return Rules({int(k): int(v) for k, v in json.loads(text).items()}, kill_points)


DEFAULT_RULES = Rules(dict(DEFAULT_PLACEMENT), DEFAULT_KILL_POINTS)


@dataclass(frozen=True)
class Score:
    placement_points: int
    kill_points: int
    total: int


def calculate(rules: Rules, placement: int, kills: int) -> Score:
    """Total = placement points + kills x kill points. Unlisted placements score 0."""
    if placement < 1:
        raise ValueError("placement must be >= 1")
    if kills < 0:
        raise ValueError("kills must be >= 0")
    pp = rules.placement.get(placement, 0)
    kp = kills * rules.kill_points
    return Score(pp, kp, pp + kp)


def parse_placement_list(text: str) -> Dict[int, int]:
    """'12,9,8' -> {1:12, 2:9, 3:8}. Position in the list is the placement."""
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if not parts or len(parts) > 100:
        raise ValueError("Enter 1-100 comma-separated numbers, e.g. 12,9,8,7,6,5,4,3,2,1")
    out: Dict[int, int] = {}
    for i, p in enumerate(parts, start=1):
        if not p.lstrip("-").isdigit():
            raise ValueError(f"'{p}' is not a whole number")
        v = int(p)
        if v < 0 or v > 1000:
            raise ValueError("Points must be between 0 and 1000")
        out[i] = v
    return out


def rank_rows(rows: Iterable[dict]) -> List[dict]:
    """Deterministic ordering: total desc, kills desc, best placement asc, name.
    Rows equal on (total, kills, placement) share a rank (competition ranking)."""
    ordered = sorted(rows, key=lambda r: (-r["total"], -r["kills"], r["placement"], r["name"].lower(), str(r["key"])))
    ranked: List[dict] = []
    prev = None
    for i, r in enumerate(ordered, start=1):
        sig = (r["total"], r["kills"], r["placement"])
        rank = ranked[-1]["rank"] if prev == sig else i
        ranked.append({**r, "rank": rank})
        prev = sig
    return ranked


def aggregate(snapshots: Iterable[dict]) -> List[dict]:
    """Cumulative leaderboard across published, completed matches.

    Identity = Free Fire UID. Every (match, UID) pair is counted once:
    - individual rows credit their own player;
    - team rows credit the team's total points and team kills to every member
      listed on that team in that match (team score is shared, not split).
    """
    acc: Dict[str, dict] = {}
    for snap in snapshots:
        seen = set()
        for row in snap["rows"]:
            for m in row["members"]:
                if m["uid"] in seen:
                    continue
                seen.add(m["uid"])
                a = acc.setdefault(m["uid"], {"key": m["uid"], "name": m["name"], "total": 0,
                                              "kills": 0, "placement": 10 ** 6, "matches": 0})
                a["name"] = m["name"]
                a["total"] += row["total"]
                a["kills"] += row["kills"]
                a["placement"] = min(a["placement"], row["placement"])
                a["matches"] += 1
    return rank_rows(acc.values())
