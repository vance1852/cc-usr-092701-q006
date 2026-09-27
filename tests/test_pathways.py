from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.api import create_handler
from careflow.clock import FrozenClock
from careflow.db import Database
from careflow.errors import Conflict, Forbidden, NotFound, ValidationError
from careflow.service import Careflow

SECTIONS = ["chief_complaint", "assessment", "plan"]
NODES_V1 = [
    {"kind": "review", "title": "首次评估后复核", "offset_days": 28, "assign_to_owner": True},
    {"kind": "measurement", "title": "体重腰围复测", "offset_days": 56},
]
NODES_V2 = [
    {"kind": "review", "title": "首次评估后复核", "offset_days": 21, "assign_to_owner": True},
    {"kind": "measurement", "title": "体重腰围复测", "offset_days": 42},
]


class PathwayCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "诊所负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        self.clinician = self.app.create_staff(self.clinic, "临床医生甲", "clinician", actor_id=self.owner)["id"]
        self.reviewer = self.app.create_staff(self.clinic, "临床医生乙", "clinician", actor_id=self.owner)["id"]
        self.nurse = self.app.create_staff(self.clinic, "护理人员", "nurse", actor_id=self.owner)["id"]
        self.coordinator = self.app.create_staff(self.clinic, "运营协调员", "coordinator", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.coordinator, "case-101", "林女士")
        self._consent_revision = 0

    def tearDown(self):
        self.temp.cleanup()

    def consent(self, purpose="weight_program"):
        self._consent_revision += 1
        digest = hashlib.sha256(f"{purpose}-r{self._consent_revision}".encode()).hexdigest()
        return self.app.grant_consent(self.clinic, self.clinician, self.patient["id"], purpose,
                                      self._consent_revision, digest)

    def publish_template(self, nodes=NODES_V1, name="体重管理标准路径", program="weight"):
        template = self.app.pathways.create_template(self.clinic, self.clinician, name, program, SECTIONS, nodes)
        version_id = template["draft_version"]["id"]
        self.app.pathways.submit(self.clinic, self.clinician, version_id)
        self.app.pathways.review(self.clinic, self.reviewer, version_id, "approve")
        return template["id"], version_id

    def create_plan(self, template_id, key="plan-key-1", start_date="2026-09-27", consent_id=None):
        consent_id = consent_id or self.consent()["id"]
        return self.app.create_plan_from_template(
            self.clinic, self.clinician, template_id, self.patient["id"], self.clinician,
            {"description": "按路径管理体重"}, {"screening": "reviewed"}, start_date,
            consent_id=consent_id, idempotency_key=key)

    def test_publish_requires_submission_and_separate_reviewer(self):
        with self.assertRaises(Forbidden):
            self.app.pathways.create_template(self.clinic, self.nurse, "护理路径", "weight", SECTIONS, NODES_V1)
        template = self.app.pathways.create_template(self.clinic, self.clinician, "体重管理标准路径", "weight", SECTIONS, NODES_V1)
        version_id = template["draft_version"]["id"]
        with self.assertRaises(Conflict):
            self.app.pathways.review(self.clinic, self.reviewer, version_id, "approve")
        submitted = self.app.pathways.submit(self.clinic, self.clinician, version_id)
        self.assertEqual(submitted["state"], "pending_review")
        with self.assertRaises(Forbidden):
            self.app.pathways.review(self.clinic, self.clinician, version_id, "approve")
        with self.assertRaises(Forbidden):
            self.app.pathways.review(self.clinic, self.coordinator, version_id, "approve")
        approved = self.app.pathways.review(self.clinic, self.reviewer, version_id, "approve", note="同意发布")
        self.assertEqual(approved["state"], "published")
        detail = self.app.pathways.get_template(self.clinic, self.clinician, template["id"])
        self.assertEqual(detail["versions"][0]["state"], "published")
        self.assertEqual(detail["versions"][0]["reviewed_by"], self.reviewer)
        listing = self.app.pathways.list_templates(self.clinic, self.clinician, program="weight")
        self.assertEqual(listing[0]["published_version"]["version"], 1)

    def test_plan_creation_generates_nodes_once_and_replay_returns_original_set(self):
        template_id, version_id = self.publish_template()
        with self.assertRaises(Conflict):
            self.app.create_plan_from_template(
                self.clinic, self.clinician, template_id, self.patient["id"], self.clinician,
                {"description": "缺授权"}, {}, "2026-09-27", idempotency_key="no-consent")
        first_consent = self.consent()["id"]
        first = self.create_plan(template_id, consent_id=first_consent)
        self.assertFalse(first["replayed"])
        self.assertEqual(first["plan"]["template_version_id"], version_id)
        self.assertEqual(first["plan"]["kind"], "weight")
        self.assertEqual(len(first["nodes"]), 2)
        review, measurement = first["nodes"]
        self.assertEqual(review["due_at"], "2026-10-25T01:00:00Z")
        self.assertEqual(review["assigned_to"], self.clinician)
        self.assertEqual(measurement["due_at"], "2026-11-22T01:00:00Z")
        replay = self.create_plan(template_id, consent_id=first_consent)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["plan"]["id"], first["plan"]["id"])
        self.assertEqual([node["id"] for node in replay["nodes"]], [node["id"] for node in first["nodes"]])
        listed = self.app.milestones.list_for_plan(self.clinic, self.clinician, first["plan"]["id"])
        self.assertEqual(len(listed), 2)
        with self.assertRaises(Conflict):
            self.create_plan(template_id, start_date="2026-09-28")
        binding = self.app.pathways.plan_pathway(self.clinic, self.clinician, first["plan"]["id"])
        self.assertEqual(binding["binding"]["version"], 1)
        self.assertEqual(binding["binding"]["version_state"], "published")
        self.assertEqual(binding["migrations"], [])
        history = self.app.milestones.history(self.clinic, self.clinician, review["id"])
        self.assertEqual([event["action"] for event in history], ["created"])

    def test_unpublished_or_rejected_template_cannot_generate_plan(self):
        template = self.app.pathways.create_template(self.clinic, self.clinician, "体重管理标准路径", "weight", SECTIONS, NODES_V1)
        version_id = template["draft_version"]["id"]
        with self.assertRaises(Conflict):
            self.create_plan(template["id"])
        self.app.pathways.submit(self.clinic, self.clinician, version_id)
        with self.assertRaises(Conflict):
            self.create_plan(template["id"])
        rejected = self.app.pathways.review(self.clinic, self.reviewer, version_id, "reject", note="间隔依据不足")
        self.assertEqual(rejected["state"], "rejected")
        with self.assertRaises(Conflict):
            self.create_plan(template["id"])
        draft = self.app.pathways.create_version(self.clinic, self.clinician, template["id"], SECTIONS, NODES_V2)
        self.assertEqual(draft["version"], 2)
        self.app.pathways.submit(self.clinic, self.clinician, draft["id"])
        self.app.pathways.review(self.clinic, self.owner, draft["id"], "approve")
        plan = self.create_plan(template["id"])
        self.assertEqual(plan["plan"]["template_version"], 2)
        self.assertEqual(plan["nodes"][0]["due_at"], "2026-10-18T01:00:00Z")

    def test_new_version_only_shapes_new_plans_until_individual_migration(self):
        template_id, version1 = self.publish_template()
        plan_a = self.create_plan(template_id, key="plan-a")["plan"]
        plan_c = self.create_plan(template_id, key="plan-c")["plan"]
        manual = self.app.milestones.create(self.clinic, self.clinician, plan_a["id"], "followup", "手工营养随访",
                                            "2026-10-02T09:00:00+08:00", "manual-1")
        generated = {node["kind"]: node for node in self.app.milestones.list_for_plan(self.clinic, self.clinician, plan_a["id"])
                     if node["kind"] in {"review", "measurement"}}
        self.app.milestones.transition(self.clinic, self.clinician, generated["review"]["id"], 1, "complete", reason="已完成首检")
        draft2 = self.app.pathways.create_version(self.clinic, self.clinician, template_id, SECTIONS, NODES_V2)
        self.app.pathways.submit(self.clinic, self.clinician, draft2["id"])
        with self.assertRaises(Conflict):
            self.app.pathways.create_version(self.clinic, self.clinician, template_id, SECTIONS, NODES_V2)
        self.app.pathways.review(self.clinic, self.reviewer, draft2["id"], "approve")
        self.assertEqual(self.app.pathways.get_version(self.clinic, self.clinician, version1)["state"], "superseded")
        plan_b = self.create_plan(template_id, key="plan-b")["plan"]
        self.assertEqual(self.app.pathways.plan_pathway(self.clinic, self.clinician, plan_b["id"])["binding"]["version"], 2)
        nodes_a = self.app.milestones.list_for_plan(self.clinic, self.clinician, plan_a["id"])
        self.assertEqual([node["due_at"] for node in nodes_a if node["kind"] == "review"],
                         ["2026-10-25T01:00:00Z"])
        with self.assertRaises(Forbidden):
            self.app.pathways.migrate_plan(self.clinic, self.coordinator, plan_a["id"], template_id, "统一改三周")
        migrated = self.app.pathways.migrate_plan(self.clinic, self.owner, plan_a["id"], template_id, "医疗负责人批准改三周复核")
        self.assertEqual(migrated["from_version_id"], version1)
        self.assertEqual(migrated["generation"], 1)
        self.assertEqual(len(migrated["cancelled_node_ids"]), 1)
        nodes_a = {node["id"]: node for node in self.app.milestones.list_for_plan(self.clinic, self.clinician, plan_a["id"])}
        self.assertEqual(nodes_a[manual["id"]]["state"], "pending")
        self.assertEqual(nodes_a[generated["review"]["id"]]["state"], "completed")
        self.assertEqual(migrated["cancelled_node_ids"], [generated["measurement"]["id"]])
        self.assertEqual(nodes_a[generated["measurement"]["id"]]["state"], "cancelled")
        new_reviews = [node for node in nodes_a.values() if node["state"] == "pending" and node["kind"] == "review"]
        self.assertEqual([node["due_at"] for node in new_reviews], ["2026-10-18T01:00:00Z"])
        binding = self.app.pathways.plan_pathway(self.clinic, self.clinician, plan_a["id"])
        self.assertEqual(binding["binding"]["version"], 2)
        self.assertEqual(len(binding["migrations"]), 1)
        self.assertEqual(binding["migrations"][0]["approved_by"], self.owner)
        with self.assertRaises(Conflict):
            self.app.pathways.migrate_plan(self.clinic, self.owner, plan_a["id"], template_id, "重复迁移")
        nodes_c = self.app.milestones.list_for_plan(self.clinic, self.clinician, plan_c["id"])
        self.assertEqual([node["due_at"] for node in nodes_c], ["2026-10-25T01:00:00Z", "2026-11-22T01:00:00Z"])
        self.assertEqual(self.app.pathways.plan_pathway(self.clinic, self.clinician, plan_c["id"])["binding"]["version"], 1)

    def test_withdraw_keeps_referenced_content_and_blocks_new_plans(self):
        template_id, version_id = self.publish_template()
        plan = self.create_plan(template_id)["plan"]
        withdrawn = self.app.pathways.withdraw(self.clinic, self.owner, version_id, "间隔依据更新，停用该版本")
        self.assertEqual(withdrawn["state"], "withdrawn")
        self.assertEqual(withdrawn["bound_plan_count"], 1)
        with self.assertRaises(Conflict):
            self.create_plan(template_id, key="plan-after-withdraw")
        version = self.app.pathways.get_version(self.clinic, self.clinician, version_id)
        self.assertEqual(version["state"], "withdrawn")
        self.assertEqual(version["required_sections"], SECTIONS)
        self.assertEqual(len(version["nodes"]), 2)
        nodes = self.app.milestones.list_for_plan(self.clinic, self.clinician, plan["id"])
        self.assertEqual(len(nodes), 2)
        binding = self.app.pathways.plan_pathway(self.clinic, self.clinician, plan["id"])
        self.assertEqual(binding["binding"]["version_state"], "withdrawn")
        with self.assertRaises(Conflict):
            self.app.pathways.withdraw(self.clinic, self.owner, version_id, "重复撤回")
        self.assertTrue(self.app.verify_audit(self.clinic, self.owner)["ok"])

    def test_template_definition_validation_and_name_uniqueness(self):
        with self.assertRaises(ValidationError):
            self.app.pathways.create_template(self.clinic, self.clinician, "路径", "weight", ["unknown_section"], NODES_V1)
        with self.assertRaises(ValidationError):
            self.app.pathways.create_template(self.clinic, self.clinician, "路径", "weight", ["plan", "plan"], NODES_V1)
        with self.assertRaises(ValidationError):
            self.app.pathways.create_template(self.clinic, self.clinician, "路径", "weight", SECTIONS, [])
        with self.assertRaises(ValidationError):
            self.app.pathways.create_template(self.clinic, self.clinician, "路径", "weight", SECTIONS,
                                              [{"kind": "review", "title": "复核", "offset_days": -1}])
        with self.assertRaises(ValidationError):
            self.app.pathways.create_template(self.clinic, self.clinician, "路径", "weight", SECTIONS,
                                              [{"kind": "review", "title": "复核", "offset_days": 21, "due_time": "25:00"}])
        template = self.app.pathways.create_template(self.clinic, self.clinician, "路径", "weight", SECTIONS, NODES_V1)
        with self.assertRaises(Conflict):
            self.app.pathways.create_template(self.clinic, self.clinician, "路径", "weight", SECTIONS, NODES_V1)
        edited = self.app.pathways.update_draft(self.clinic, self.clinician, template["draft_version"]["id"],
                                                SECTIONS, NODES_V2)
        self.assertEqual(edited["nodes"][0]["offset_days"], 21)
        self.app.pathways.submit(self.clinic, self.clinician, template["draft_version"]["id"])
        with self.assertRaises(Conflict):
            self.app.pathways.update_draft(self.clinic, self.clinician, template["draft_version"]["id"], SECTIONS, NODES_V1)
        with self.assertRaises(NotFound):
            self.app.pathways.get_template(self.clinic, self.clinician, "pwt_missing")

    def test_migration_rejects_program_mismatch_and_finished_plan(self):
        template_id, _ = self.publish_template()
        plan = self.create_plan(template_id)["plan"]
        aesthetic = self.app.pathways.create_template(self.clinic, self.clinician, "医美路径", "aesthetic", SECTIONS, NODES_V1)
        self.app.pathways.submit(self.clinic, self.clinician, aesthetic["draft_version"]["id"])
        self.app.pathways.review(self.clinic, self.reviewer, aesthetic["draft_version"]["id"], "approve")
        with self.assertRaises(Conflict):
            self.app.pathways.migrate_plan(self.clinic, self.owner, plan["id"], aesthetic["id"], "项目不匹配")
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 1, "cancel", reason="患者中止")
        with self.assertRaises(Conflict):
            self.app.pathways.migrate_plan(self.clinic, self.owner, plan["id"], template_id, "已结束计划")

    def test_http_template_review_and_idempotent_plan_creation(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def call(method, path, token, payload=None, headers=None):
            body = json.dumps(payload).encode() if payload is not None else None
            request = Request(base + path, data=body, method=method,
                              headers={"X-Clinic-ID": self.clinic, "Content-Type": "application/json",
                                       "Authorization": f"Bearer {token}", **(headers or {})})
            try:
                with urlopen(request, timeout=3) as response:
                    return response.status, json.loads(response.read())
            except HTTPError as error:
                return error.code, json.loads(error.read())

        try:
            self.app.set_password(self.clinic, self.owner, self.clinician, "ClinicianPass!2026")
            self.app.set_password(self.clinic, self.owner, self.reviewer, "ReviewerPass!2026")
            _, session_a = call("POST", "/auth/token", None, {"staff_id": self.clinician, "password": "ClinicianPass!2026"})
            _, session_b = call("POST", "/auth/token", None, {"staff_id": self.reviewer, "password": "ReviewerPass!2026"})
            token_a, token_b = session_a["access_token"], session_b["access_token"]
            status, template = call("POST", "/pathway-templates", token_a,
                                    {"name": "体重管理标准路径", "program": "weight",
                                     "required_sections": SECTIONS, "nodes": NODES_V1})
            self.assertEqual(status, 201)
            version_id = template["draft_version"]["id"]
            status, _ = call("POST", f"/pathway-template-versions/{version_id}/submit", token_a, {})
            self.assertEqual(status, 200)
            status, _ = call("POST", f"/pathway-template-versions/{version_id}/review", token_a, {"action": "approve"})
            self.assertEqual(status, 403)
            status, _ = call("POST", f"/pathway-template-versions/{version_id}/review", token_b, {"action": "approve"})
            self.assertEqual(status, 200)
            consent = self.consent()
            payload = {"patient_id": self.patient["id"], "clinical_owner": self.clinician,
                       "goal": {"description": "按路径管理体重"}, "risk": {}, "start_date": "2026-09-27",
                       "consent_id": consent["id"]}
            status, first = call("POST", f"/pathway-templates/{template['id']}/plans", token_a, payload,
                                 {"Idempotency-Key": "http-plan-1"})
            self.assertEqual(status, 201)
            self.assertEqual(len(first["nodes"]), 2)
            status, replay = call("POST", f"/pathway-templates/{template['id']}/plans", token_a, payload,
                                  {"Idempotency-Key": "http-plan-1"})
            self.assertEqual(status, 201)
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["plan"]["id"], first["plan"]["id"])
            status, pathway = call("GET", f"/plans/{first['plan']['id']}/pathway", token_a)
            self.assertEqual(status, 200)
            self.assertEqual(pathway["binding"]["version"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
