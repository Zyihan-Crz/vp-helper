"""Read original CCPC eligibility and timelines from public SRK archives."""
from __future__ import annotations

import math
import json
import re
from urllib.parse import urlsplit

from .models import Cell, Contest, Submission, TeamScore, VPError
from .parsing import timestamp
from .scoring import score_submissions, verdict


def source_url(contest: Contest) -> str | None:
    match = re.fullmatch(r"(20\d{2}) CCPC ([A-Za-z][A-Za-z .-]*) Reg", contest.name)
    if not match:
        return None
    year, site = match.groups()
    slug = re.sub(r"[ .-]+", "", site.lower())
    return ("https://raw.githubusercontent.com/algoux/srk-collection/master/official/ccpc/ccpc"
            + year + "/ccpc" + year + slug + ".srk.json")


def _seconds(value) -> float:
    units = {"ms": 0.001, "s": 1, "min": 60, "h": 3600}
    if (not isinstance(value, list) or len(value) != 2 or type(value[0]) not in (int, float)
            or value[1] not in units or not math.isfinite(value[0]) or value[0] < 0):
        raise ValueError("invalid SRK duration")
    return value[0] * units[value[1]]


def _names(value) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value]
    if isinstance(value, dict):
        values = [value.get(k) for k in ("fallback", "zh-CN", "en-US")]
        names = list(dict.fromkeys(v for v in values if isinstance(v, str) and v.strip()))
        if names:
            return names
    raise ValueError("missing SRK identity")


def metadata(data: dict, contest: Contest, source: str) -> dict:
    try:
        if data["sorter"]["algorithm"] != "ICPC" or data["sorter"]["config"].get("rankingTimePrecision") != "min":
            raise ValueError("unsupported SRK scoring")
        duration = _seconds(data["contest"]["duration"])
        penalty = _seconds(data["sorter"]["config"]["penalty"])
        labels = [p["alias"] for p in data["problems"]]
        if (int(duration) != duration or penalty % 60 or labels != list(contest.problems)
                or duration < contest.rules.duration):
            raise ValueError("SRK rules do not match")
        # The archive must identify the exact judge problems, not just a city.
        for problem in data["problems"]:
            link = urlsplit(problem["link"])
            base = urlsplit(contest.url)
            if (link.hostname != base.hostname or link.path != base.path + "/problem/" + contest.problems[problem["alias"]]):
                raise ValueError("SRK problems do not match this contest")
        teams = []
        groups = {}
        for row in data["rows"]:
            user = {k: v for k, v in row["user"].items() if k != "id"}
            groups.setdefault(json.dumps(user, sort_keys=True), []).append(row)
        ignored, empty_names = [], []
        for group in groups.values():
            if len(group) < 2:
                continue
            if not group[0]["user"].get("teamMembers"):
                raise ValueError("duplicate team without member identity")
            active = [r for r in group if r["score"]["value"] or any(
                s.get("solutions") or s.get("tries") or s.get("result") for s in r["statuses"])]
            if len(active) > 1:
                raise ValueError("duplicate active team histories")
            keep = active[0] if active else group[0]
            for duplicate in group:
                if duplicate is keep:
                    continue
                if _seconds(duplicate["score"]["time"]) != 0:
                    raise ValueError("duplicate team has a nonempty score")
                ignored.append(str(duplicate["user"]["id"]))
                empty_names.append(_names(keep["user"]["name"])[0] + " (" + _names(keep["user"]["organization"])[0] + ")")
        identities = set()
        for row in data["rows"]:
            user = row["user"]
            ident = str(user["id"])
            if ident in ignored:
                continue
            if not ident or ident == "me" or ident in identities or type(user.get("official")) is not bool:
                raise ValueError("ambiguous SRK team")
            identities.add(ident)
            names, organizations = _names(user["name"]), _names(user["organization"])
            aliases = [name + " (" + organization + ")" for name in names for organization in organizations]
            # QOJ's comma-delimited ghost import can retain only the last part
            # of a comma-containing name. School, eligibility and every final
            # problem cell must still match; ambiguous aliases are rejected.
            aliases += [name.rsplit(",", 1)[-1].strip() + " (" + organization + ")"
                        for name in names if "," in name and name.rsplit(",", 1)[-1].strip()
                        for organization in organizations]
            aliases = list(dict.fromkeys(aliases))
            teams.append({"id": ident, "name": aliases[0], "aliases": aliases,
                          "official": user["official"]})
        medals = None
        for series in data.get("series", []):
            rule = series.get("rule", {})
            counts = rule.get("options", {}).get("count", {}).get("value")
            styles = [s.get("style") for s in series.get("segments", [])]
            if (rule.get("preset") == "ICPC" and styles == ["gold", "silver", "bronze"]
                    and isinstance(counts, list) and len(counts) == 3
                    and all(type(n) is int and n >= 0 for n in counts)):
                medals = dict(zip(("gold", "silver", "bronze"), counts))
                break
        return {"version": 1, "contest_name": contest.name, "duration": int(duration),
                "penalty_minutes": int(penalty // 60), "problems": labels,
                "problem_ids": {str(i): p for i, p in enumerate(labels)},
                "source": source, "source_format": "srk", "teams": teams, "medals": medals,
                "ignored_empty_team_ids": ignored, "ignored_empty_team_names": empty_names,
                "original_start": timestamp(data["contest"]["startAt"])}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise VPError("The SRK archive does not match the contest, judge problem IDs, or explicit team eligibility.") from None


def submissions(data: dict, metadata: dict, contest: Contest) -> list[Submission]:
    events, expected = [], []
    teams = {team["id"]: team for team in metadata["teams"]}
    try:
        for row in data["rows"]:
            if str(row["user"]["id"]) in metadata.get("ignored_empty_team_ids", []):
                continue
            team = teams[str(row["user"]["id"])]
            if len(row["statuses"]) != len(contest.problems):
                raise ValueError("incomplete SRK statuses")
            cells, raw_time = {}, 0.0
            for label, status in zip(contest.problems, row["statuses"]):
                accepted = status.get("result") in ("AC", "FB")
                tries = status.get("tries", 0)
                if type(tries) is not int or tries < 0 or status.get("result") == "?":
                    raise ValueError("incomplete SRK result")
                at = _seconds(status["time"]) if accepted else None
                cells[label] = Cell(tries, 0, accepted, int(at // 60) if at is not None else None)
                if accepted:
                    if not tries:
                        raise ValueError("accepted SRK problem without attempt")
                    raw_time += at + contest.rules.penalty_minutes * 60 * (tries - 1)
                for solution in status.get("solutions", []):
                    code = solution["result"]
                    if code in ("NOUT", "UKE") and code in data["sorter"]["config"].get("noPenaltyResults", []):
                        continue
                    result = verdict({"FB": "AC", "RTE": "RE", "PE": "WA"}.get(code, code))
                    event_time = _seconds(solution["time"])
                    if result in ("PENDING", "TOO-LATE") or not 0 <= event_time < contest.rules.duration:
                        raise ValueError("nonfinal or out-of-window SRK event")
                    events.append(Submission(str(len(events) + 1), team["id"], label, event_time, result))
            final = TeamScore(team["id"], team["name"], cells, eligible=team["official"],
                              penalty_minutes=contest.rules.penalty_minutes)
            if final.solved != row["score"]["value"] or not math.isclose(raw_time, _seconds(row["score"]["time"]), abs_tol=0.001):
                raise ValueError("SRK declared score is inconsistent")
            expected.append(final)
        grouped = {team: [] for team in teams}
        for event in events:
            grouped[event.team].append(event)
        for final in expected:
            actual = score_submissions(grouped[final.id], list(contest.problems), contest.rules,
                                       contest.rules.duration, team_id=final.id)
            if actual.cells != final.cells:
                raise ValueError("SRK histories do not reproduce every final cell")
        return events
    except (KeyError, TypeError, ValueError, AttributeError, VPError):
        raise VPError("The SRK submission histories do not reproduce the original final scores.") from None
