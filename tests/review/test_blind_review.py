import csv
import json
import tempfile
import unittest
from pathlib import Path

from story_patterns.analysis.export import export_csv, export_json
from story_patterns.anonymization import POLICY_ID
from story_patterns.application import StoryPatternApplication
from story_patterns.catalog.validate import digest_json
from story_patterns.review.validate import normalize_decision
from tests.support import AppCase, batch, decision, story


class BlindReviewCodeTests(AppCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app.import_batch(batch([story("tale-a"), story("tale-b")], "blind"))

    def record_round(self, annotator, pattern="parallel", sequence=1, story_id="tale-a"):
        return self.app.record_decision(decision(
            story_id, annotator=annotator, pattern=pattern, sequence=sequence,
            decision_id=f"{story_id}-vote-{sequence}-{annotator[-1]}",
        ))

    def codes_for(self, story_id="tale-a"):
        row = next(item for item in self.app.review_queue()["stories"] if item["story_id"] == story_id)
        return row["reviewer_codes"]

    @staticmethod
    def without_codes(pairs):
        return [{key: value for key, value in pair.items() if key != "reviewer_codes"} for pair in pairs]

    def test_names_never_appear_in_queue_report_or_exports(self):
        for name in ("alice", "bob"):
            self.record_round(name)
        queue = self.app.review_queue()
        report = self.app.create_report()
        rendered = json.dumps(queue, ensure_ascii=False) + json.dumps(report, ensure_ascii=False)
        for name in ("alice", "bob"):
            self.assertNotIn(name, rendered)
        for code in self.codes_for():
            self.assertRegex(code, r"^R-[0-9A-F]{8}$")

    def test_same_reader_keeps_one_identity_across_stories_and_rounds(self):
        for story_id in ("tale-a", "tale-b"):
            self.record_round("alice", story_id=story_id)
            self.record_round("bob", story_id=story_id)
        report = self.app.create_report()
        alice = next(item for item in report["reader_vote_distribution"]["reader_workload"]
                     if item["story_revisions_reviewed"] == 2)
        self.assertEqual(len([item for item in report["reader_vote_distribution"]["reader_workload"]
                              if item["story_revisions_reviewed"] == 2]), 2)
        self.assertIn(alice["reviewer_code"], report["reviewer_anonymization"]["codes_in_report"])
        pair = report["reviewer_agreement"]["pairs"][0]
        self.assertEqual(pair["compared_story_revisions"], 2)
        self.assertEqual(len(pair["reviewer_codes"]), 2)

    def test_agreeing_and_disagreeing_votes_both_group_by_stable_identity(self):
        self.record_round("alice", pattern="parallel")
        self.record_round("bob", pattern="harm")
        self.record_round("carol", pattern="parallel")
        pairs = self.app.create_report()["reviewer_agreement"]["pairs"]
        self.assertEqual(len(pairs), 3)
        self.assertEqual(sorted(pair["pattern_agreement"] for pair in pairs), [0.0, 0.0, 1.0])
        alice_like = [pair for pair in pairs if pair["pattern_agreement"] == 1.0][0]
        self.assertEqual(alice_like["compared_story_revisions"], 1)

    def test_code_rotation_does_not_change_any_existing_vote_attribution(self):
        self.record_round("alice")
        self.record_round("bob")
        before = self.app.create_report()
        old_code = self.codes_for()[0]
        rotated = self.app.rotate_reviewer_code(old_code)
        self.assertEqual(rotated["previous_code"], old_code)
        after = self.app.create_report()
        self.assertEqual(self.without_codes(before["reviewer_agreement"]["pairs"]),
                         self.without_codes(after["reviewer_agreement"]["pairs"]))
        self.assertEqual(before["reader_vote_distribution"]["pattern_votes"],
                         after["reader_vote_distribution"]["pattern_votes"])
        before_codes = set(before["reviewer_anonymization"]["codes_in_report"])
        after_codes = set(after["reviewer_anonymization"]["codes_in_report"])
        self.assertEqual(len(before_codes), len(after_codes))
        self.assertEqual(before_codes - after_codes, {old_code})
        self.assertEqual(after_codes - before_codes, {rotated["reviewer_code"]})
        self.assertIn(rotated["reviewer_code"], self.codes_for())
        self.assertNotIn(old_code, self.codes_for())

    def test_old_report_and_export_keep_the_codes_they_froze(self):
        self.record_round("alice")
        self.record_round("bob")
        first = self.app.create_report()
        frozen = self.app.stored_report(first["report_id"])
        old_code = self.codes_for()[0]
        self.app.rotate_reviewer_code(old_code)
        self.assertEqual(self.app.stored_report(first["report_id"]), frozen)
        self.assertEqual(frozen["reviewer_anonymization"]["codes_in_report"],
                         first["reviewer_anonymization"]["codes_in_report"])
        self.assertIn(old_code, frozen["reviewer_anonymization"]["codes_in_report"])
        with tempfile.TemporaryDirectory() as directory:
            path = export_json(frozen, Path(directory) / "old.json")
            loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["reviewer_anonymization"]["codes_in_report"],
                         first["reviewer_anonymization"]["codes_in_report"])

    def test_rotation_accepts_a_previous_code_or_the_local_label(self):
        self.record_round("alice")
        first_code = self.codes_for()[0]
        second = self.app.rotate_reviewer_code(first_code)["reviewer_code"]
        third = self.app.rotate_reviewer_code(first_code)["reviewer_code"]
        fourth = self.app.rotate_reviewer_code("alice")["reviewer_code"]
        self.assertEqual(len({first_code, second, third, fourth}), 4)
        self.assertEqual(self.app.review_queue()["stories"][0]["reviewer_count"], 1)
        self.assertIn(fourth, self.codes_for())
        self.assertEqual(self.app.review_queue()["stories"][0]["reviewer_codes"], [fourth])

    def test_duplicate_submission_does_not_change_vote_ownership(self):
        self.record_round("alice")
        self.record_round("bob")
        self.app.rotate_reviewer_code(self.codes_for()[0])
        codes = self.codes_for()
        status = self.record_round("alice")
        self.assertEqual(status["status"], "already_recorded")
        self.assertEqual(self.codes_for(), codes)
        self.assertEqual(self.app.review_queue()["stories"][0]["reviewer_count"], 2)

    def test_resubmission_using_a_rotated_code_is_the_same_vote(self):
        self.record_round("alice")
        code = self.codes_for()[0]
        rotated = self.app.rotate_reviewer_code(code)["reviewer_code"]
        # 把新代号或旧代号当作本地口令重新提交，仍解析回同一位读者。
        for label in (rotated, code, "alice"):
            with self.subTest(label=label):
                record = decision("tale-a", annotator=label, decision_id="tale-a-vote-1-e")
                self.assertEqual(self.app.record_decision(record)["status"], "already_recorded")
        self.assertEqual(self.app.review_queue()["stories"][0]["reviewer_count"], 1)
        self.assertEqual(len(self.app.create_report()["reviewer_anonymization"]["codes_in_report"]), 1)

    def test_invalid_submission_does_not_register_a_reviewer(self):
        from story_patterns.errors import ValidationError

        with self.assertRaises(ValidationError):
            self.app.record_decision(decision(annotator="mallory", evidence=["missing"]))
        self.assertEqual(self.app.audit()["reviewers_checked"], 0)
        self.assertEqual(self.app.review_queue()["stories"][0]["reviewer_count"], 0)

    def test_later_sequence_supersedes_same_stable_reader_after_rotation(self):
        self.record_round("alice", pattern="parallel")
        self.record_round("bob", pattern="parallel")
        self.app.rotate_reviewer_code("alice")
        self.record_round("alice", pattern="harm", sequence=2)
        row = self.app.review_queue()["stories"][0]
        self.assertEqual(row["reviewer_count"], 2)
        self.assertEqual(row["status"], "disputed")
        history = self.app.review_history("tale-a", 1)["decisions"]
        self.assertEqual(len(history), 3)
        self.assertEqual([item["current"] for item in history].count(False), 1)
        # Same reader's two rounds use one display code, so the dispute is between two codes.
        self.assertEqual(len(set(row["reviewer_codes"])), 2)

    def test_rotation_on_unknown_code_is_a_domain_error(self):
        from story_patterns.errors import NotFoundError

        with self.assertRaises(NotFoundError):
            self.app.rotate_reviewer_code("R-00000000")

    def test_anonymous_identity_cannot_be_used_as_authentication(self):
        report = self.app.create_report()
        policy = report["reviewer_anonymization"]
        self.assertEqual(policy["policy"], POLICY_ID)
        self.assertFalse(policy["names_or_local_aliases_exported"])
        self.assertIn("不提供用户认证", policy["authentication"])

    def test_csv_export_carries_only_display_codes(self):
        self.record_round("alice")
        self.record_round("bob")
        report = self.app.create_report()
        with tempfile.TemporaryDirectory() as directory:
            path = export_csv(report, Path(directory) / "report.csv")
            content = path.read_text(encoding="utf-8")
        self.assertNotIn("alice", content)
        with tempfile.TemporaryDirectory() as directory:
            path = export_csv(self.app.create_report(), Path(directory) / "report.csv")
            with path.open(encoding="utf-8", newline="") as source:
                row = next(csv.DictReader(source))
        codes = row["reviewer_codes"].split(";")
        self.assertEqual(codes, sorted(codes))
        self.assertTrue(all(code.startswith("R-") for code in codes))

    def test_report_policy_is_frozen_into_the_identifier(self):
        self.record_round("alice")
        first = self.app.create_report()
        self.app.rotate_reviewer_code(self.codes_for()[0])
        second = self.app.create_report()
        self.assertNotEqual(first["report_id"], second["report_id"])
        self.assertNotEqual(first["reviewer_anonymization"]["codes_in_report"],
                            second["reviewer_anonymization"]["codes_in_report"])

    def test_audit_checks_identity_links_and_active_codes(self):
        self.record_round("alice")
        self.app.create_report()
        self.assertTrue(self.app.audit()["ok"])
        import sqlite3

        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE decisions SET reviewer_id='reviewer-unknown'")
        result = self.app.audit()
        self.assertFalse(result["ok"])
        self.assertTrue(any(issue["scope"] == "decision" for issue in result["issues"]))

    def test_audit_detects_tampered_display_code_directory(self):
        self.record_round("alice")
        with self.app.store.transaction() as connection:
            connection.execute("UPDATE reviewer_codes SET active=0")
        result = self.app.audit()
        self.assertFalse(result["ok"])
        self.assertTrue(any(issue["scope"] == "reviewer" for issue in result["issues"]))


class LegacyDatabaseMigrationTests(unittest.TestCase):
    def test_existing_named_votes_are_linked_to_one_stable_identity(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "legacy.sqlite3")
            app = StoryPatternApplication(database)
            app.import_batch(batch([story("tale-a")], "legacy"))
            app.record_decision(decision(annotator="alice"))
            story_document = app.store.story_document("tale-a", 1)
            legacy_digest = digest_json(normalize_decision(decision(annotator="alice"), story_document))
            # 模拟旧版数据库：摘要按本地名字计算，没有 reviewer_id 和代号目录。
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE decisions SET digest=?, digest_version=1, reviewer_id=NULL", (legacy_digest,))
                connection.execute("DELETE FROM reviewer_aliases")
                connection.execute("DELETE FROM reviewer_codes")
                connection.execute("DELETE FROM reviewers")
            reopened = StoryPatternApplication(database)
            queue = reopened.review_queue()
            self.assertEqual(queue["stories"][0]["reviewer_count"], 1)
            self.assertEqual(len(queue["stories"][0]["reviewer_codes"]), 1)
            self.assertTrue(reopened.audit()["ok"])
            # 旧名字仍可作为口令把同一读者解析回原身份。
            reopened.record_decision(decision(annotator="alice", sequence=2, pattern="harm",
                                              decision_id="alice-second"))
            self.assertEqual(reopened.review_queue()["stories"][0]["reviewer_count"], 1)

    def test_legacy_migration_keeps_old_digest_so_tampering_stays_visible(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "legacy.sqlite3")
            app = StoryPatternApplication(database)
            app.import_batch(batch([story("tale-a")], "legacy"))
            app.record_decision(decision(annotator="alice"))
            with sqlite3.connect(database) as connection:
                # 旧摘要配旧内容：迁移应换算摘要而不是掩盖不匹配。
                story_document = app.store.story_document("tale-a", 1)
                legacy_digest = digest_json(normalize_decision(decision(annotator="alice"), story_document))
                connection.execute(
                    "UPDATE decisions SET digest=?, digest_version=1, reviewer_id=NULL", (legacy_digest,))
                connection.execute("DELETE FROM reviewer_aliases")
                connection.execute("DELETE FROM reviewer_codes")
                connection.execute("DELETE FROM reviewers")
            reopened = StoryPatternApplication(database)
            issue = reopened.audit()
            self.assertTrue(issue["ok"], issue["issues"])
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE decisions SET pattern='harm'")
            self.assertFalse(reopened.audit()["ok"])


if __name__ == "__main__":
    unittest.main()
