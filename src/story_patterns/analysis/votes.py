from __future__ import annotations

from collections import Counter, defaultdict

from story_patterns.anonymization import decision_reader_code, decision_reader_key


PATTERN_LABELS = ("parallel", "harm", "unclear")
OUTCOME_LABELS = ("wins", "loses", "mixed", "unknown")


def vote_distribution(stories: list[dict], decisions: dict) -> dict:
    pattern_totals = Counter()
    outcome_totals = Counter()
    reviewers_per_story = Counter()
    workload = defaultdict(set)
    codes = {}
    story_rows = []
    for story in stories:
        identity = (story["story_id"], story["revision"])
        votes = decisions.get(identity, [])
        pattern_counts = Counter(item["pattern"] for item in votes)
        outcome_counts = Counter(item["youngest_outcome"] for item in votes)
        pattern_totals.update(pattern_counts)
        outcome_totals.update(outcome_counts)
        reviewers_per_story[len(votes)] += 1
        for vote in votes:
            reader = decision_reader_key(vote)
            workload[reader].add(f"{identity[0]}@{identity[1]}")
            codes[reader] = decision_reader_code(vote)
        story_rows.append({
            "story_id": identity[0], "revision": identity[1], "reader_count": len(votes),
            "pattern_votes": {label: pattern_counts.get(label, 0) for label in PATTERN_LABELS},
            "outcome_votes": {label: outcome_counts.get(label, 0) for label in OUTCOME_LABELS},
        })
    return {
        "active_reader_votes": sum(pattern_totals.values()),
        "pattern_votes": {label: pattern_totals.get(label, 0) for label in PATTERN_LABELS},
        "outcome_votes": {label: outcome_totals.get(label, 0) for label in OUTCOME_LABELS},
        "stories_by_reader_count": {str(count): amount for count, amount in sorted(reviewers_per_story.items())},
        "reader_workload": [
            {"reviewer_code": codes[name], "story_revisions_reviewed": len(identities)}
            for name, identities in sorted(workload.items(), key=lambda item: (codes[item[0]], str(item[0])))
        ],
        "stories": story_rows,
    }
