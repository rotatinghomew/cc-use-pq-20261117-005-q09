from __future__ import annotations

from story_patterns.anonymization import resolve_reviewer
from story_patterns.catalog.validate import canonical_json
from story_patterns.errors import ConflictError, NotFoundError
from story_patterns.review.validate import bind_reviewer, decision_digest, normalize_decision
from story_patterns.review.adjudication import AdjudicationService


class ReviewService:
    def __init__(self, store):
        self.store = store
        self.adjudications = AdjudicationService(store)

    def record_decision(self, raw: dict) -> dict:
        story_id = raw.get("story_id") if isinstance(raw, dict) else None
        revision = raw.get("story_revision") if isinstance(raw, dict) else None
        story = self.store.story_document(story_id, revision)
        if story is None:
            raise NotFoundError(f"story revision {story_id!r}/{revision!r} was not found")
        # 先完整校验请求，避免格式错误或假证据的提交顺手登记一个审阅者。
        normalized = normalize_decision(raw, story)
        with self.store.transaction() as connection:
            reviewer_id = resolve_reviewer(connection, normalized["annotator"])
            decision = bind_reviewer(normalized, reviewer_id)
            digest = decision_digest(decision)
            previous = connection.execute(
                "SELECT digest FROM decisions WHERE decision_id = ?", (decision["decision_id"],)
            ).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise ConflictError(f"decision {decision['decision_id']} already identifies different content")
                return {"decision_id": decision["decision_id"], "status": "already_recorded"}
            reused = connection.execute(
                "SELECT decision_id FROM decisions WHERE story_id=? AND story_revision=? "
                "AND reviewer_id=? AND sequence=?",
                (story_id, revision, reviewer_id, decision["sequence"]),
            ).fetchone()
            if reused:
                raise ConflictError("annotator sequence is already occupied for this story revision")
            connection.execute(
                "INSERT INTO decisions(decision_id, story_id, story_revision, annotator, reviewer_id, sequence, "
                "pattern, youngest_outcome, evidence_event_ids, note, digest, digest_version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,2)",
                (decision["decision_id"], story_id, revision, decision["annotator"], reviewer_id,
                 decision["sequence"], decision["pattern"], decision["youngest_outcome"],
                 canonical_json(decision["evidence_event_ids"]), decision["note"], digest),
            )
        return {"decision_id": decision["decision_id"], "status": "recorded"}

    def decisions_for(self, story_id: str, revision: int) -> list[dict]:
        return self.store.latest_decisions(story_id, revision)

    def queue(self, minimum_reviewers: int = 2, tradition: str | None = None) -> dict:
        from story_patterns.analysis.cohorts import select_cohort, validate_minimum
        from story_patterns.review.readiness import build_review_queue, queue_totals

        minimum = validate_minimum(minimum_reviewers)
        stories, decisions, _origins, adjudications = self.store.analysis_snapshot(tradition)
        rows = build_review_queue(select_cohort(stories, tradition), decisions, minimum, adjudications)
        return {"minimum_reviewers": minimum, "total": len(rows), "by_status": queue_totals(rows), "stories": rows}

    def record_adjudication(self, record: dict) -> dict:
        return self.adjudications.record(record)

    def history(self, story_id: str, revision: int) -> dict:
        from story_patterns.errors import NotFoundError

        if self.store.story_document(story_id, revision) is None:
            raise NotFoundError(f"story revision {story_id!r}/{revision!r} was not found")
        votes, adjudications = self.store.decision_history(story_id, revision)
        latest_by_reader = {}
        for item in votes:
            key = item["reviewer_id"]
            latest_by_reader[key] = max(latest_by_reader.get(key, 0), item["sequence"])
        for item in votes:
            item["current"] = item["sequence"] == latest_by_reader[item["reviewer_id"]]
            # 本地录入标签只留在数据库里；面向课堂的读取视图使用展示代号。
            item.pop("annotator", None)
            item.pop("reviewer_id", None)
        return {"story_id": story_id, "revision": revision, "decisions": votes,
                "adjudications": adjudications,
                "active_adjudication_id": adjudications[-1]["adjudication_id"] if adjudications else None}

    def rotate_code(self, reference: str) -> dict:
        from story_patterns.anonymization import rotate_code

        with self.store.transaction() as connection:
            return rotate_code(connection, reference)
