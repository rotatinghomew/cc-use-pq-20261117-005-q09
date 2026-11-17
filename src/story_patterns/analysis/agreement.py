from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from story_patterns.anonymization import decision_reader_code, decision_reader_key


def pairwise_agreement(decisions: dict) -> dict:
    comparisons = defaultdict(lambda: {"compared": 0, "pattern_matches": 0, "outcome_matches": 0})
    codes = {}
    for votes in decisions.values():
        by_reader = {}
        for vote in votes:
            reader = decision_reader_key(vote)
            by_reader[reader] = vote
            codes[reader] = decision_reader_code(vote)
        for first, second in combinations(sorted(by_reader), 2):
            key = (first, second)
            summary = comparisons[key]
            summary["compared"] += 1
            summary["pattern_matches"] += by_reader[first]["pattern"] == by_reader[second]["pattern"]
            summary["outcome_matches"] += by_reader[first]["youngest_outcome"] == by_reader[second]["youngest_outcome"]
    output = []
    for (first, second), summary in sorted(comparisons.items(), key=lambda item: (codes[item[0][0]], codes[item[0][1]])):
        compared = summary["compared"]
        output.append({
            "reviewer_codes": [codes[first], codes[second]],
            "compared_story_revisions": compared,
            "pattern_matches": summary["pattern_matches"],
            "pattern_agreement": summary["pattern_matches"] / compared,
            "outcome_matches": summary["outcome_matches"],
            "outcome_agreement": summary["outcome_matches"] / compared,
        })
    return {
        "method": "exact pairwise agreement; grouping uses a stable local reviewer identity, not the display code",
        "limitation": "descriptive only",
        "pairs": output,
    }
