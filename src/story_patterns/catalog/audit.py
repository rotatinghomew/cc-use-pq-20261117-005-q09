from __future__ import annotations

import json
from collections import defaultdict

from story_patterns.anonymization import POLICY_ID
from story_patterns.catalog.validate import digest_json, normalize_story
from story_patterns.errors import ValidationError
from story_patterns.review.adjudication import normalize_adjudication
from story_patterns.review.validate import decision_digest, normalize_decision


def audit_catalog(snapshot: dict) -> dict:
    issues = []
    reviewer_ids = {row["reviewer_id"] for row in snapshot.get("reviewers", [])}
    code_values = {row["code"] for row in snapshot.get("reviewer_codes", [])}
    if reviewer_ids:
        _audit_reviewer_directory(snapshot, reviewer_ids, issues)
    stories = {}
    for row in snapshot["stories"]:
        identity = (row["story_id"], row["revision"])
        try:
            document = json.loads(row["document"])
            normalized = normalize_story(document)
        except (json.JSONDecodeError, ValidationError, TypeError) as error:
            issues.append(_issue("story", identity, f"invalid story document: {error}"))
            continue
        if normalized != document or (normalized["title"], normalized["tradition"], normalized["source"]["kind"]) != (
                row["title"], row["tradition"], row["source_kind"]):
            issues.append(_issue("story", identity, "document and indexed columns disagree"))
        stories[identity] = normalized
    batch_rows = {row["batch_id"]: row for row in snapshot["batches"]}
    grouped = defaultdict(list)
    for relation in snapshot["batch_stories"]:
        identity = (relation["story_id"], relation["revision"])
        grouped[relation["batch_id"]].append(identity)
        if identity not in stories:
            issues.append(_issue("batch", relation["batch_id"], f"references missing story revision {identity}"))
    for batch_id, row in batch_rows.items():
        identities = sorted(grouped.get(batch_id, []))
        if row["story_count"] != len(identities):
            issues.append(_issue("batch", batch_id, "declared story count differs from linked records"))
        if not identities or any(identity not in stories for identity in identities):
            continue
        payload = {"batch_id": batch_id, "stories": [stories[identity] for identity in identities]}
        if digest_json(payload) != row["digest"]:
            issues.append(_issue("batch", batch_id, "content digest differs from linked story revisions"))
    origins_by_identity = grouped_by_identity(grouped)
    for identity in stories:
        if not origins_by_identity.get(identity):
            issues.append(_issue("story", identity, "story revision has no import-batch provenance"))
    for row in snapshot["decisions"]:
        identity = (row["story_id"], row["story_revision"])
        story = stories.get(identity)
        if story is None:
            issues.append(_issue("decision", row["decision_id"], "references missing story revision"))
            continue
        if row.get("reviewer_id") is None:
            issues.append(_issue("decision", row["decision_id"], "missing stable reviewer identity"))
            continue
        if row["reviewer_id"] not in reviewer_ids:
            issues.append(_issue("decision", row["decision_id"], "references an unknown reviewer identity"))
        try:
            payload = {key: row[key] for key in ("decision_id", "story_id", "story_revision", "annotator", "sequence",
                                                  "pattern", "youngest_outcome", "note")}
            payload["evidence_event_ids"] = json.loads(row["evidence_event_ids"])
            normalized = normalize_decision(payload, story)
            if row.get("digest_version", 1) == 1:
                # 旧版摘要绑定本地名字与故事标识；保持旧算法以便仍能发现历史篡改。
                if digest_json(normalized) != row["digest"]:
                    issues.append(_issue("decision", row["decision_id"], "stored decision digest does not match content"))
            else:
                bound = normalized | {"reviewer_id": row["reviewer_id"]}
                if decision_digest(bound) != row["digest"]:
                    issues.append(_issue("decision", row["decision_id"], "stored decision digest does not match content"))
        except (json.JSONDecodeError, ValidationError, TypeError) as error:
            issues.append(_issue("decision", row["decision_id"], f"invalid decision: {error}"))
    for row in snapshot["adjudications"]:
        identity = (row["story_id"], row["story_revision"])
        story = stories.get(identity)
        if story is None:
            issues.append(_issue("adjudication", row["adjudication_id"], "references missing story revision"))
            continue
        try:
            payload = {key: row[key] for key in ("adjudication_id", "story_id", "story_revision", "moderator",
                                                  "sequence", "pattern", "youngest_outcome", "rationale")}
            payload["evidence_event_ids"] = json.loads(row["evidence_event_ids"])
            normalized = normalize_adjudication(payload, story)
            if digest_json(normalized) != row["digest"]:
                issues.append(_issue("adjudication", row["adjudication_id"], "stored digest does not match content"))
        except (json.JSONDecodeError, ValidationError, TypeError) as error:
            issues.append(_issue("adjudication", row["adjudication_id"], f"invalid adjudication: {error}"))
    story_keys = {f"{story_id}@{revision}" for story_id, revision in stories}
    decision_ids = {row["decision_id"] for row in snapshot["decisions"]}
    adjudication_ids = {row["adjudication_id"] for row in snapshot["adjudications"]}
    for row in snapshot["reports"]:
        try:
            document = json.loads(row["document"])
            body = {key: value for key, value in document.items() if key != "report_id"}
            if document.get("report_id") != row["report_id"] or digest_json(body)[:24] != row["report_id"]:
                issues.append(_issue("report", row["report_id"], "report content does not match its identifier"))
                continue
            provenance = document["provenance"]
            if not set(provenance["story_revisions"]).issubset(story_keys):
                issues.append(_issue("report", row["report_id"], "references missing story revisions"))
            if not set(provenance["decision_ids"]).issubset(decision_ids):
                issues.append(_issue("report", row["report_id"], "references missing reader decisions"))
            if not set(provenance["adjudication_ids"]).issubset(adjudication_ids):
                issues.append(_issue("report", row["report_id"], "references missing adjudications"))
            for identity, batch_ids in provenance.get("batch_ids_by_story", {}).items():
                if identity not in story_keys or not set(batch_ids).issubset(batch_rows):
                    issues.append(_issue("report", row["report_id"], "references missing batch provenance"))
            policy = document.get("reviewer_anonymization")
            if policy is not None:
                if policy.get("policy") != POLICY_ID:
                    issues.append(_issue("report", row["report_id"], "unknown reviewer anonymization policy"))
                reported_codes = set(policy.get("codes_in_report", []))
                if not reported_codes.issubset(code_values):
                    issues.append(_issue("report", row["report_id"], "references unknown reviewer codes"))
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            issues.append(_issue("report", row["report_id"], f"invalid report document: {error}"))
    issues.sort(key=lambda item: (item["scope"], str(item["identity"]), item["problem"]))
    return {"ok": not issues, "story_revisions_checked": len(snapshot["stories"]),
            "batches_checked": len(snapshot["batches"]), "decisions_checked": len(snapshot["decisions"]),
            "batch_links_checked": len(snapshot["batch_stories"]),
            "adjudications_checked": len(snapshot["adjudications"]), "reports_checked": len(snapshot["reports"]),
            "reviewers_checked": len(reviewer_ids), "reviewer_codes_checked": len(code_values),
            "issue_count": len(issues), "issues": issues}


def grouped_by_identity(grouped: dict) -> dict:
    result = defaultdict(list)
    for batch_id, identities in grouped.items():
        for identity in identities:
            result[identity].append(batch_id)
    return result


def _audit_reviewer_directory(snapshot: dict, reviewer_ids: set[str], issues: list) -> None:
    """复核代号目录：身份存在、每人恰有一个有效代号、别名不与代号冲突。"""
    codes = snapshot.get("reviewer_codes", [])
    aliases = snapshot.get("reviewer_aliases", [])
    active_counts = defaultdict(int)
    for row in codes:
        if row["reviewer_id"] not in reviewer_ids:
            issues.append(_issue("reviewer", row["code"], "code references an unknown reviewer identity"))
        if row["active"]:
            active_counts[row["reviewer_id"]] += 1
    for reviewer_id in reviewer_ids:
        if active_counts.get(reviewer_id, 0) != 1:
            issues.append(_issue("reviewer", reviewer_id, "reviewer does not have exactly one active code"))
    code_values = {row["code"] for row in codes}
    aliased_codes = defaultdict(list)
    for row in aliases:
        if row["reviewer_id"] not in reviewer_ids:
            issues.append(_issue("reviewer-alias", row["alias"], "alias references an unknown reviewer identity"))
        if row["alias"] in code_values:
            aliased_codes[row["alias"]].append(row["reviewer_id"])
    for code, linked in aliased_codes.items():
        owner = {row["reviewer_id"] for row in codes if row["code"] == code}
        if set(linked) - owner:
            issues.append(_issue("reviewer-code", code, "code is aliased to a different reviewer identity"))
    labels = defaultdict(set)
    for row in snapshot.get("decisions", []):
        if row.get("reviewer_id"):
            labels[row["annotator"]].add(row["reviewer_id"])
    for label, owners in labels.items():
        if len(owners) > 1:
            issues.append(_issue("reviewer-alias", label, "one local label is linked to multiple reviewer identities"))


def _issue(scope: str, identity, problem: str) -> dict:
    return {"scope": scope, "identity": identity, "problem": problem}
