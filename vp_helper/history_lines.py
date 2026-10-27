"""Attach original medal boundaries to history without network or CSV edits."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .models import Contest, Submission, VPError
from .originals import archive_submissions, cache_path
from .scoring import medal_lines, rank_of, score_submissions
from .storage import read_json

LINE_FIELDS = ("gold_line", "silver_line", "bronze_line")


def _cached_lines(contest: Contest, state: dict, row: dict) -> dict[str, str]:
    fallback = medal_lines([], contest.medals, complete=False)
    if state.get("status") != "complete":
        return fallback
    metadata = contest.extra.get("original_eligibility") or read_json(cache_path("eligibility", contest.url))
    if (metadata.get("contest_name") != contest.name
            or metadata.get("duration") != contest.rules.duration
            or metadata.get("penalty_minutes") != contest.rules.penalty_minutes
            or metadata.get("problems") != list(contest.problems)):
        # Old extended/shifted imports lack the verified alignment snapshot.
        return fallback
    source = metadata.get("source", "")
    if not source:
        return fallback
    source = source if metadata.get("source_format") == "srk" else source + "/run.json"
    cached = read_json(cache_path("runs", source))
    teams = metadata.get("teams", [])
    if (cached.get("source") != source or not isinstance(cached.get("runs"), list)
            or not teams or len({str(t["id"]) for t in teams}) != len(teams)
            or any(type(t.get("official")) is not bool or str(t["id"]) == "me" for t in teams)):
        return fallback
    # This run cache is created only after every original final cell has been
    # checked against the judge. Reading it here never fetches future results.
    events = archive_submissions(cached["runs"], metadata, contest)
    grouped = {str(team["id"]): [] for team in teams}
    for event in events:
        grouped[event.team].append(event)
    scores = []
    for team in teams:
        score = score_submissions(grouped[str(team["id"])], list(contest.problems), contest.rules,
                                  contest.rules.duration, team_id=str(team["id"]), name=team["name"])
        score.eligible = team["official"]
        scores.append(score)
    # A legacy history row gives another check that the cached clock agrees.
    if all(str(row.get(key) or "").isdigit() for key in ("rank", "solved", "penalty")):
        own = score_submissions([Submission(**event) for event in state.get("submissions", [])],
                                list(contest.problems), contest.rules, contest.rules.duration)
        if ((own.solved, own.penalty) != (int(row["solved"]), int(row["penalty"]))
                or rank_of(own, scores) != int(row["rank"])):
            return fallback
    return medal_lines(scores, contest.medals)


def enrich_history(rows: list[dict], root: Path) -> list[dict]:
    states = {}
    for path in root.glob("vp-*.json"):
        try:
            state = read_json(path)
            started = datetime.fromtimestamp(state["start_time"], timezone.utc).isoformat()
            states[(started, state["url"])] = state
        except (VPError, KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
    enriched = []
    for row in rows:
        lines = dict.fromkeys(LINE_FIELDS, "—")
        state = states.get((row["start_time"], row["url"]))
        if state is not None:
            contest = None
            try:
                contest = Contest.from_dict(state["contest"])
                saved = state.get("medal_lines", {})
                if state.get("status") == "complete" and all(isinstance(saved.get(key), str) for key in LINE_FIELDS):
                    lines = {key: saved[key] for key in LINE_FIELDS}
                else:
                    lines = _cached_lines(contest, state, row)
            except (VPError, KeyError, TypeError, ValueError, OSError):
                if contest is not None:
                    lines = medal_lines([], contest.medals, complete=False)
        enriched.append({**row, **lines})
    return enriched
