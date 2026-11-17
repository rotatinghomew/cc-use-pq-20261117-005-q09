from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batches (
    batch_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    story_count INTEGER NOT NULL CHECK(story_count > 0)
);
CREATE TABLE IF NOT EXISTS stories (
    story_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    title TEXT NOT NULL,
    tradition TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    document TEXT NOT NULL,
    PRIMARY KEY(story_id, revision)
);
CREATE INDEX IF NOT EXISTS stories_by_tradition ON stories(tradition, story_id, revision);
CREATE TABLE IF NOT EXISTS batch_stories (
    batch_id TEXT NOT NULL REFERENCES import_batches(batch_id) ON DELETE RESTRICT,
    story_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY(batch_id, story_id, revision),
    FOREIGN KEY(story_id, revision) REFERENCES stories(story_id, revision) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS batch_stories_by_story ON batch_stories(story_id, revision, batch_id);
CREATE TABLE IF NOT EXISTS reviewers (
    reviewer_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS reviewer_codes (
    code TEXT PRIMARY KEY,
    reviewer_id TEXT NOT NULL REFERENCES reviewers(reviewer_id) ON DELETE RESTRICT,
    active INTEGER NOT NULL CHECK(active IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS reviewer_codes_one_active
    ON reviewer_codes(reviewer_id) WHERE active = 1;
CREATE INDEX IF NOT EXISTS reviewer_codes_by_reviewer ON reviewer_codes(reviewer_id);
CREATE TABLE IF NOT EXISTS reviewer_aliases (
    alias TEXT PRIMARY KEY,
    reviewer_id TEXT NOT NULL REFERENCES reviewers(reviewer_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS reviewer_aliases_by_reviewer ON reviewer_aliases(reviewer_id);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    story_id TEXT NOT NULL,
    story_revision INTEGER NOT NULL,
    annotator TEXT NOT NULL,
    reviewer_id TEXT REFERENCES reviewers(reviewer_id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    pattern TEXT NOT NULL,
    youngest_outcome TEXT NOT NULL,
    evidence_event_ids TEXT NOT NULL,
    note TEXT NOT NULL,
    digest TEXT NOT NULL,
    digest_version INTEGER NOT NULL DEFAULT 2 CHECK(digest_version IN (1, 2)),
    UNIQUE(story_id, story_revision, annotator, sequence),
    FOREIGN KEY(story_id, story_revision) REFERENCES stories(story_id, revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS decisions_by_reviewer_sequence
    ON decisions(story_id, story_revision, reviewer_id, sequence) WHERE reviewer_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS decisions_by_story ON decisions(story_id, story_revision, annotator, sequence);
CREATE TABLE IF NOT EXISTS adjudications (
    adjudication_id TEXT PRIMARY KEY,
    story_id TEXT NOT NULL,
    story_revision INTEGER NOT NULL,
    moderator TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    pattern TEXT NOT NULL,
    youngest_outcome TEXT NOT NULL,
    evidence_event_ids TEXT NOT NULL,
    rationale TEXT NOT NULL,
    digest TEXT NOT NULL,
    UNIQUE(story_id, story_revision, sequence),
    FOREIGN KEY(story_id, story_revision) REFERENCES stories(story_id, revision)
);
CREATE INDEX IF NOT EXISTS adjudications_by_story ON adjudications(story_id, story_revision, sequence);
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    document TEXT NOT NULL
);
"""


class StoryStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("a file-backed database is required for durable local reports")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "decisions" in tables:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(decisions)").fetchall()}
                if "reviewer_id" not in columns:
                    connection.execute("ALTER TABLE decisions ADD COLUMN reviewer_id TEXT")
                if "digest_version" not in columns:
                    connection.execute(
                        "ALTER TABLE decisions ADD COLUMN digest_version INTEGER NOT NULL DEFAULT 1")
            connection.executescript(SCHEMA)
            self._backfill_reviewer_identities(connection)
            self._link_legacy_local_labels(connection)
        finally:
            connection.close()

    def _link_legacy_local_labels(self, connection: sqlite3.Connection) -> None:
        """把旧版遗留的本地标签登记为历史别名，但不改写稳定身份或既有投票。"""
        rows = connection.execute(
            "SELECT DISTINCT d.annotator FROM decisions d WHERE d.reviewer_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM reviewer_aliases a WHERE a.alias=d.annotator)"
        ).fetchall()
        if not rows:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                reviewer = connection.execute(
                    "SELECT reviewer_id FROM decisions WHERE annotator=? AND reviewer_id IS NOT NULL LIMIT 1",
                    (row["annotator"],),
                ).fetchone()
                if reviewer is None:
                    continue
                taken = connection.execute(
                    "SELECT reviewer_id FROM reviewer_aliases WHERE alias=?", (row["annotator"],)
                ).fetchone()
                if taken is None:
                    connection.execute(
                        "INSERT INTO reviewer_aliases(alias, reviewer_id) VALUES(?,?)",
                        (row["annotator"], reviewer["reviewer_id"]),
                    )
                elif taken["reviewer_id"] != reviewer["reviewer_id"]:
                    # 同名标签已被另一个稳定身份占用：不猜测，保留现状让审计发现。
                    continue
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _backfill_reviewer_identities(self, connection: sqlite3.Connection) -> None:
        """为旧版数据库中只有本地名字的既有投票补上稳定身份。

        补录只分配身份；只有当旧摘要仍与既有内容一致时才把摘要换算为当前口径。
        若旧摘要已不匹配，就保留原值让 `audit` 报告篡改，而不是借迁移掩盖它。
        """
        from story_patterns.anonymization import resolve_reviewer
        from story_patterns.catalog.validate import digest_json
        from story_patterns.review.validate import bind_reviewer, decision_digest, normalize_decision

        legacy_rows = connection.execute(
            "SELECT decision_id FROM decisions WHERE reviewer_id IS NULL LIMIT 1"
        ).fetchone()
        if legacy_rows is None:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            annotators = [row[0] for row in connection.execute(
                "SELECT DISTINCT annotator FROM decisions WHERE reviewer_id IS NULL ORDER BY annotator"
            ).fetchall()]
            for annotator in annotators:
                reviewer_id = resolve_reviewer(connection, annotator)
                # 代号目录已经在新审阅者登记时把旧名字写成别名；这里补上的是
                # “旧名字本身就是历史代号”的情况，不修改 decisions.annotator。
                connection.execute(
                    "INSERT OR IGNORE INTO reviewer_aliases(alias, reviewer_id) VALUES(?,?)",
                    (annotator, reviewer_id),
                )
                rows = connection.execute(
                    "SELECT * FROM decisions WHERE annotator=? AND reviewer_id IS NULL ORDER BY decision_id",
                    (annotator,),
                ).fetchall()
                for row in rows:
                    stored_digest = row["digest"]
                    digest = stored_digest
                    if row["digest_version"] == 1:
                        story = connection.execute(
                            "SELECT document FROM stories WHERE story_id=? AND revision=?",
                            (row["story_id"], row["story_revision"]),
                        ).fetchone()
                        if story is not None:
                            try:
                                payload = {
                                    key: row[key] for key in (
                                        "decision_id", "story_id", "story_revision", "annotator", "sequence",
                                        "pattern", "youngest_outcome", "note")
                                }
                                payload["evidence_event_ids"] = json.loads(row["evidence_event_ids"])
                                normalized = normalize_decision(payload, json.loads(story["document"]))
                                if digest_json(normalized) == stored_digest:
                                    digest = decision_digest(bind_reviewer(normalized, reviewer_id))
                                    connection.execute(
                                        "UPDATE decisions SET digest_version=2 WHERE decision_id=?",
                                        (row["decision_id"],),
                                    )
                            except (json.JSONDecodeError, ValueError):
                                pass
                    connection.execute(
                        "UPDATE decisions SET reviewer_id=?, digest=? WHERE decision_id=?",
                        (reviewer_id, digest, row["decision_id"]),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read_connection(self):
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def story_document(self, story_id: str, revision: int) -> dict | None:
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT document FROM stories WHERE story_id=? AND revision=?", (story_id, revision)
            ).fetchone()
        return json.loads(row["document"]) if row else None

    def list_stories(self, tradition: str | None = None) -> list[dict]:
        with self.read_connection() as connection:
            if tradition is None:
                rows = connection.execute(
                    "SELECT document FROM stories ORDER BY tradition, story_id, revision"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT document FROM stories WHERE tradition=? ORDER BY story_id, revision",
                    (tradition,),
                ).fetchall()
        return [json.loads(row["document"]) for row in rows]

    def latest_decisions(self, story_id: str, revision: int) -> list[dict]:
        with self.read_connection() as connection:
            return self._latest_decisions(connection, story_id, revision)

    @staticmethod
    def _latest_decisions(connection: sqlite3.Connection, story_id: str, revision: int,
                          with_codes: bool = True) -> list[dict]:
        code_column = (
            "(SELECT c.code FROM reviewer_codes c WHERE c.reviewer_id=d.reviewer_id AND c.active=1)"
            if with_codes else "NULL"
        )
        rows = connection.execute(
            "SELECT d.*, " + code_column + " AS reviewer_code FROM decisions d JOIN ("
            "SELECT reviewer_id, MAX(sequence) AS latest FROM decisions "
            "WHERE story_id=? AND story_revision=? AND reviewer_id IS NOT NULL GROUP BY reviewer_id"
            ") newest ON d.reviewer_id=newest.reviewer_id AND d.sequence=newest.latest "
            "WHERE d.story_id=? AND d.story_revision=? ORDER BY d.reviewer_id",
            (story_id, revision, story_id, revision),
        ).fetchall()
        output = []
        for row in rows:
            item = dict(row) | {"evidence_event_ids": json.loads(row["evidence_event_ids"])}
            # 读取视图只提供稳定身份和展示代号；本地录入标签留在数据库中供解析别名。
            item.pop("annotator", None)
            output.append(item)
        return output

    def decision_history(self, story_id: str, revision: int) -> tuple[list[dict], list[dict]]:
        with self.read_connection() as connection:
            votes = connection.execute(
                "SELECT d.*, "
                "(SELECT c.code FROM reviewer_codes c WHERE c.reviewer_id=d.reviewer_id AND c.active=1) "
                "AS reviewer_code FROM decisions d WHERE story_id=? AND story_revision=? "
                "ORDER BY reviewer_id, sequence",
                (story_id, revision),
            ).fetchall()
            adjudications = connection.execute(
                "SELECT * FROM adjudications WHERE story_id=? AND story_revision=? ORDER BY sequence",
                (story_id, revision),
            ).fetchall()
        return (
            [dict(row) | {"evidence_event_ids": json.loads(row["evidence_event_ids"])} for row in votes],
            [dict(row) | {"evidence_event_ids": json.loads(row["evidence_event_ids"])} for row in adjudications],
        )

    def latest_decisions_for_stories(self, identities: list[tuple[str, int]]) -> dict[tuple[str, int], list[dict]]:
        result = {}
        for story_id, revision in identities:
            result[(story_id, revision)] = self.latest_decisions(story_id, revision)
        return result

    def analysis_snapshot(self, tradition: str | None) -> tuple[list[dict], dict, dict, dict]:
        """返回报告所需的只读快照。

        每张投票同时带 ``reviewer_id``（稳定身份）和 ``reviewer_code``（当前展示代号）。
        统计只按前者分组；报告会把当时的代号写进内容指纹，因此之后的代号轮换
        不会回写已保存或已导出的历史报告。
        """
        with self.read_connection() as connection:
            connection.execute("BEGIN")
            if tradition is None:
                rows = connection.execute(
                    "SELECT document FROM stories ORDER BY tradition, story_id, revision"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT document FROM stories WHERE tradition=? ORDER BY story_id, revision", (tradition,)
                ).fetchall()
            stories = [json.loads(row["document"]) for row in rows]
            identities = [(item["story_id"], item["revision"]) for item in stories]
            decisions = {}
            adjudications = {}
            for story_id, revision in identities:
                decisions[(story_id, revision)] = self._latest_decisions(connection, story_id, revision)
                adjudication = connection.execute(
                    "SELECT * FROM adjudications WHERE story_id=? AND story_revision=? "
                    "ORDER BY sequence DESC LIMIT 1", (story_id, revision)
                ).fetchone()
                adjudications[(story_id, revision)] = (
                    dict(adjudication) | {"evidence_event_ids": json.loads(adjudication["evidence_event_ids"])}
                    if adjudication else None
                )
            origins = {identity: [] for identity in identities}
            if identities:
                rows = connection.execute(
                    "SELECT batch_id, story_id, revision FROM batch_stories ORDER BY batch_id"
                ).fetchall()
                for row in rows:
                    identity = (row["story_id"], row["revision"])
                    if identity in origins:
                        origins[identity].append(row["batch_id"])
        return stories, decisions, origins, adjudications

    def catalog_manifest(self) -> tuple[list[dict], list[dict]]:
        with self.read_connection() as connection:
            batches = connection.execute(
                "SELECT b.batch_id, b.digest, b.story_count, COUNT(bs.story_id) AS linked_story_count "
                "FROM import_batches b LEFT JOIN batch_stories bs ON bs.batch_id=b.batch_id "
                "GROUP BY b.batch_id ORDER BY b.batch_id"
            ).fetchall()
            stories = connection.execute(
                "SELECT document FROM stories ORDER BY story_id, revision"
            ).fetchall()
        return [dict(row) for row in batches], [json.loads(row["document"]) for row in stories]

    def catalog_audit_snapshot(self) -> dict:
        with self.read_connection() as connection:
            return {
                "stories": [dict(row) for row in connection.execute("SELECT * FROM stories ORDER BY story_id, revision")],
                "batches": [dict(row) for row in connection.execute("SELECT * FROM import_batches ORDER BY batch_id")],
                "batch_stories": [dict(row) for row in connection.execute(
                    "SELECT * FROM batch_stories ORDER BY batch_id, story_id, revision")],
                "decisions": [dict(row) for row in connection.execute("SELECT * FROM decisions ORDER BY decision_id")],
                "adjudications": [dict(row) for row in connection.execute(
                    "SELECT * FROM adjudications ORDER BY adjudication_id")],
                "reports": [dict(row) for row in connection.execute("SELECT * FROM reports ORDER BY report_id")],
                "reviewers": [dict(row) for row in connection.execute("SELECT * FROM reviewers ORDER BY reviewer_id")],
                "reviewer_codes": [dict(row) for row in connection.execute(
                    "SELECT * FROM reviewer_codes ORDER BY code")],
                "reviewer_aliases": [dict(row) for row in connection.execute(
                    "SELECT * FROM reviewer_aliases ORDER BY alias")],
            }

    def report_document(self, report_id: str) -> dict | None:
        with self.read_connection() as connection:
            row = connection.execute("SELECT document FROM reports WHERE report_id=?", (report_id,)).fetchone()
        return json.loads(row["document"]) if row else None

    def integrity_issues(self) -> list[str]:
        with self.read_connection() as connection:
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        issues = [f"foreign key violation: {tuple(row)}" for row in foreign_keys]
        if quick_check != "ok":
            issues.append(f"SQLite quick_check: {quick_check}")
        return issues
