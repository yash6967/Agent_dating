import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core
import autonomy


class CoreFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(core, "DB_PATH", Path(self.temp.name) / "test.sqlite3")
        self.path_patch.start()
        self.key_patch = patch.dict("os.environ", {"OPENAI_API_KEY": ""})
        self.key_patch.start()
        self.db = core.connect()

    def tearDown(self):
        self.db.close()
        self.key_patch.stop()
        self.path_patch.stop()
        self.temp.cleanup()

    def add_profile(self, slug, name, interest):
        person_id = core.add_person(
            self.db,
            f"https://www.linkedin.com/in/{slug}/",
            f"https://www.instagram.com/{slug}/",
        )
        sources = core.sources(self.db, person_id)
        records = {
            "linkedin": {"name": name, "headline": f"Founder and {interest} enthusiast", "about": "", "location": "", "experience": [], "skills": [], "posts": [], "url": sources["linkedin"]["url"]},
            "instagram": {"name": name, "username": slug, "biography": f"I enjoy {interest}", "private": False, "verified": False, "posts": [], "url": sources["instagram"]["url"]},
        }
        with self.db:
            for platform, source in sources.items():
                self.db.execute(
                    "INSERT INTO source_snapshots(id, source_id, observed_at, actor_id, content_hash, content_json) VALUES(?, ?, ?, ?, ?, ?)",
                    (core.uid(), source["id"], core.now(), "test/actor", core.uid(), json.dumps(records[platform])),
                )
                self.db.execute("UPDATE profile_sources SET status='ready' WHERE id=?", (source["id"],))
        core.analyze_person(self.db, person_id)
        return person_id

    def test_urls_require_exact_profile_domains(self):
        with self.assertRaises(ValueError):
            core.canonical_url("https://instagram.com.evil.example/alice", "instagram")
        with self.assertRaises(ValueError):
            core.canonical_url("https://www.instagram.com/p/abc", "instagram")
        self.assertEqual(core.canonical_url("https://in.linkedin.com/in/alice/?x=1", "linkedin"), "https://www.linkedin.com/in/alice/")

    def test_default_model_uses_low_cost_bounded_response(self):
        response = {"output": [{"content": [{"type": "output_text", "text": '{"ok": true}'}]}]}
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": ""}):
            with patch.object(core, "_request_json", return_value=response) as request:
                self.assertTrue(core._model_json("Return JSON", {"sample": "text"}, max_output_tokens=300)["ok"])
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["model"], "gpt-6-luna")
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["max_output_tokens"], 300)
        self.assertFalse(payload["store"])

    def test_model_retries_temporary_rate_limit(self):
        response = {"output": [{"content": [{"type": "output_text", "text": '{"ok": true}'}]}]}
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": ""}):
            with patch.object(core, "_request_json", side_effect=[RuntimeError("rate_limit_exceeded: try again in 6s"), response]) as request:
                with patch.object(core.time, "sleep") as sleep:
                    self.assertTrue(core._model_json("Return JSON", {"sample": "text"})["ok"])
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(6.5)

    def test_model_confidence_label_is_normalized_and_profile_saved(self):
        person_id = self.add_profile("confidence-example", "Confidence Example", "hiking")
        instagram = core.sources(self.db, person_id)["instagram"]
        old = core.latest_snapshot(self.db, instagram["id"])
        with self.db:
            self.db.execute(
                "INSERT INTO source_snapshots(id,source_id,observed_at,actor_id,content_hash,content_json) VALUES(?,?,?,?,?,?)",
                (core.uid(), instagram["id"], core.now(), "test/actor", core.uid(), old["content_json"]),
            )
        model_result = {"summary": "Profile interests", "claims": [
            {"category": "hobby", "claim": "Enjoys hiking", "evidence_key": "ig:bio", "confidence": "high"}],
            "unknowns": ["Dating preferences"]}
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": ""}):
            with patch.object(core, "_model_json", return_value=model_result):
                profile_id = core.analyze_person(self.db, person_id)
        self.assertEqual(core.profile_claims(self.db, profile_id)[0]["confidence"], 0.85)
        self.assertEqual(core.person(self.db, person_id)["status"], "ready")

    def test_profile_date_and_ranking_are_persisted(self):
        alice = self.add_profile("alice-example", "Alice Example", "cycling")
        bob = self.add_profile("bob-example", "Bob Example", "cycling")
        cara = self.add_profile("cara-example", "Cara Example", "gardening")
        self.assertEqual(len(core.profile_claims(self.db, core.latest_profile(self.db, alice)["id"])), 2)
        date_id = core.run_date(self.db, alice, bob)
        self.assertEqual(len(core.date_turns(self.db, date_id)), 8)
        run_id = core.rank_for(self.db, alice)
        items = core.ranking_items(self.db, run_id)
        self.assertEqual([item["candidate_id"] for item in items], [bob, cara])
        self.assertEqual(items[0]["date_id"], date_id)
        self.assertGreater(items[0]["score"], items[1]["score"])
        self.assertFalse(core.ranking_stale(self.db, alice))
        instagram = core.sources(self.db, alice)["instagram"]
        current = json.loads(core.latest_snapshot(self.db, instagram["id"])["content_json"])
        current["biography"] = "I enjoy cycling and ceramics"
        with self.db:
            self.db.execute(
                "INSERT INTO source_snapshots(id, source_id, observed_at, actor_id, content_hash, content_json) VALUES(?, ?, ?, ?, ?, ?)",
                (core.uid(), instagram["id"], core.now(), "test/actor", core.uid(), json.dumps(current)),
            )
        core.analyze_person(self.db, alice)
        self.assertTrue(core.ranking_stale(self.db, alice))
        self.assertIsNone(core.latest_date(self.db, alice, bob))

    def test_identity_mismatch_requires_curator_action(self):
        target = self.add_profile("john-example", "John Example", "music")
        instagram = core.sources(self.db, target)["instagram"]
        snapshot = core.latest_snapshot(self.db, instagram["id"])
        data = json.loads(snapshot["content_json"])
        data["name"] = "Entirely Different Person"
        with self.db:
            self.db.execute("UPDATE source_snapshots SET content_json=? WHERE id=?", (json.dumps(data), snapshot["id"]))
        with self.assertRaisesRegex(ValueError, "account names differ"):
            core.analyze_person(self.db, target)
        self.assertEqual(core.person(self.db, target)["status"], "review")
        core.confirm_identity(self.db, target)
        core.analyze_person(self.db, target)
        self.assertEqual(core.person(self.db, target)["status"], "ready")

    def test_autonomous_plan_covers_25_agents_without_pairwise_dates(self):
        people = {self.add_profile(f"person-{i}", f"Person {i}", "cycling" if i % 2 else "gardening")
                  for i in range(25)}
        plan = core.plan_autonomous_pairs(self.db, 1)
        covered = {person_id for a, b, _ in plan for person_id in (a, b)}
        self.assertEqual(covered, people)
        self.assertLessEqual(len(plan), 25)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM agent_dates").fetchone()[0], 0)

    def test_stale_snapshot_cannot_create_agent_after_source_error(self):
        person_id = self.add_profile("stale-example", "Stale Example", "hiking")
        instagram = core.sources(self.db, person_id)["instagram"]
        with self.db:
            self.db.execute("UPDATE profile_sources SET status='error' WHERE id=?", (instagram["id"],))
        with self.assertRaisesRegex(ValueError, "successfully checked"):
            core.analyze_person(self.db, person_id)

    def test_background_date_cohort_saves_plan_dates_and_all_rankings(self):
        for i in range(3):
            self.add_profile(f"cohort-{i}", f"Cohort {i}", "cycling")
        run_id = core.uid()
        with self.db:
            self.db.execute(
                "INSERT INTO pipeline_runs(id,kind,status,phase,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (run_id, "date_cohort", "running", "starting", json.dumps({"dates_per_agent": 1}), core.now(), core.now()),
            )
        run = self.db.execute("SELECT * FROM pipeline_runs WHERE id=?", (run_id,)).fetchone()
        autonomy._date_cohort(self.db, run)
        self.assertEqual(self.db.execute("SELECT status FROM pipeline_runs WHERE id=?", (run_id,)).fetchone()[0], "complete")
        self.assertGreaterEqual(self.db.execute("SELECT COUNT(*) FROM agent_dates WHERE status='complete'").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ranking_runs").fetchone()[0], 3)
        saved_count = self.db.execute("SELECT COUNT(*) FROM agent_dates").fetchone()[0]
        retry_id = core.uid()
        with self.db:
            self.db.execute(
                "INSERT INTO pipeline_runs(id,kind,status,phase,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (retry_id, "date_cohort", "running", "starting", json.dumps({"dates_per_agent": 1}), core.now(), core.now()),
            )
        retry = self.db.execute("SELECT * FROM pipeline_runs WHERE id=?", (retry_id,)).fetchone()
        autonomy._date_cohort(self.db, retry)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM agent_dates").fetchone()[0], saved_count)


if __name__ == "__main__":
    unittest.main()
