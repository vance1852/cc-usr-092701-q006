from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.clock import FrozenClock
from careflow.db import Database, decode_json
from careflow.errors import Conflict, Forbidden, NotFound, ValidationError
from careflow.service import Careflow


def template_nodes(review_days=21):
    return [
        {"code": "first_review", "kind": "review", "title": "首次复核", "offset_days": review_days, "required": True},
        {"code": "weight_check", "kind": "measurement", "title": "体重测量", "offset_days": review_days, "required": True},
        {"code": "closing", "kind": "followup", "title": "结业随访", "offset_days": 84, "required": False},
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
        self.doctor_a = self.app.create_staff(self.clinic, "医生甲", "clinician", actor_id=self.owner)["id"]
        self.doctor_b = self.app.create_staff(self.clinic, "医生乙", "clinician", actor_id=self.owner)["id"]
        self.nurse = self.app.create_staff(self.clinic, "护理人员", "nurse", actor_id=self.owner)["id"]
        self.coordinator = self.app.create_staff(self.clinic, "运营协调员", "coordinator", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.coordinator, "pw-001", "林女士")["id"]

    def tearDown(self):
        self.temp.cleanup()

    def consent(self, patient=None):
        import hashlib
        patient = patient or self.patient
        self._consent_seq = getattr(self, "_consent_seq", 0) + 1
        revision = self._consent_seq
        digest = hashlib.sha256(f"weight_program-r{revision}".encode()).hexdigest()
        return self.app.grant_consent(self.clinic, self.doctor_a, patient, "weight_program", revision, digest)["id"]

    def signed_assessment(self, *, answers=None):
        assessment = self.app.create_assessment(
            self.clinic, self.doctor_a, self.patient, "weight",
            {"weight_kg": "80.0", "waist_cm": 90},
            answers if answers is not None else {"history.chief_complaint": "体重管理", "screening.q": "无禁忌"})
        self.app.sign_assessment(self.clinic, self.doctor_a, assessment["id"], expected_version=1)
        return assessment["id"]

    def publish_template(self, code="weight-standard", review_days=21, *, author=None, reviewer=None,
                         sections=("measurements", "answers")):
        author = author or self.doctor_a
        reviewer = reviewer or self.doctor_b
        draft = self.app.pathways.create_template(
            self.clinic, author, code, "体重管理标准路径", ["weight"], list(sections),
            template_nodes(review_days))
        self.assertEqual(draft["state"], "draft")
        self.app.pathways.submit_for_review(self.clinic, author, draft["id"], note="请审批")
        published = self.app.pathways.review(self.clinic, reviewer, draft["id"], "approved", "同意发布")
        self.assertEqual(published["state"], "published")
        return published

    # ------------------------------------------------------------ 审批规则

    def test_template_requires_separate_clinician_approval(self):
        draft = self.app.pathways.create_template(
            self.clinic, self.doctor_a, "w1", "体重路径", ["weight"], ["measurements", "answers"], template_nodes())
        # 护士既不能建模板，也不能审批。
        with self.assertRaises(Forbidden):
            self.app.pathways.review(self.clinic, self.nurse, draft["id"], "approved", "同意")
        # 草稿作者不能审批自己的草稿。
        self.app.pathways.submit_for_review(self.clinic, self.doctor_a, draft["id"])
        with self.assertRaises(Forbidden):
            self.app.pathways.review(self.clinic, self.doctor_a, draft["id"], "approved", "自审")
        # 待审批之外的状态不能审批。
        rejected = self.app.pathways.review(self.clinic, self.doctor_b, draft["id"], "rejected", "需补充章节")
        self.assertEqual(rejected["state"], "rejected")
        with self.assertRaises(Conflict):
            self.app.pathways.review(self.clinic, self.doctor_b, draft["id"], "approved", "重复审批")
        # 驳回后可修改再提交，由另一名医生发布。
        self.app.pathways.edit_draft(self.clinic, self.doctor_a, draft["id"],
                                     programs=["weight"], sections=["measurements", "answers", "screening"],
                                     nodes=template_nodes())
        self.app.pathways.submit_for_review(self.clinic, self.doctor_a, draft["id"])
        published = self.app.pathways.review(self.clinic, self.doctor_b, draft["id"], "approved", "章节已补齐")
        self.assertEqual(published["state"], "published")
        self.assertEqual(published["version_no"], 1)

    def test_template_definition_is_validated(self):
        with self.assertRaises(ValidationError):
            self.app.pathways.create_template(self.clinic, self.doctor_a, "w2", "路径", [], ["measurements"], template_nodes())
        with self.assertRaises(ValidationError):
            self.app.pathways.create_template(self.clinic, self.doctor_a, "w2", "路径", ["weight"], [], template_nodes())
        with self.assertRaises(ValidationError):
            self.app.pathways.create_template(self.clinic, self.doctor_a, "w2", "路径", ["weight"],
                                              ["measurements"], [])
        bad_nodes = template_nodes() + [{"code": "first_review", "kind": "review",
                                         "title": "重复编号", "offset_days": 10}]
        with self.assertRaises(ValidationError):
            self.app.pathways.create_template(self.clinic, self.doctor_a, "w2", "路径", ["weight"],
                                              ["measurements"], bad_nodes)
        bad_kind = [{"code": "x", "kind": "surgery", "title": "手术", "offset_days": 7}]
        with self.assertRaises(ValidationError):
            self.app.pathways.create_template(self.clinic, self.doctor_a, "w2", "路径", ["weight"],
                                              ["measurements"], bad_kind)

    # ------------------------------------------------------------ 计划固化

    def test_plan_pins_version_and_generates_nodes_once(self):
        v1 = self.publish_template(review_days=28)
        consent_id, assessment_id = self.consent(), self.signed_assessment()
        first = self.app.pathways.create_plan_from_template(
            self.clinic, self.doctor_a, self.patient, "weight", self.doctor_a, "2026-09-27",
            assessment_id=assessment_id, consent_id=consent_id, idempotency_key="plan-key-1")
        self.assertEqual(first["pathway_template_version_id"], v1["id"])
        self.assertEqual([m["due_at"] for m in first["milestones"] if m["template_node_code"] == "first_review"],
                         ["2026-10-25"])  # 开始日 +28 天
        self.assertEqual(len(first["milestones"]), 3)
        # 重复提交返回原节点集合，不再加一套。
        replay = self.app.pathways.create_plan_from_template(
            self.clinic, self.doctor_a, self.patient, "weight", self.doctor_a, "2026-09-27",
            assessment_id=assessment_id, consent_id=consent_id, idempotency_key="plan-key-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual([m["id"] for m in replay["milestones"]], [m["id"] for m in first["milestones"]])
        # 同键不同内容必须拒绝。
        with self.assertRaises(Conflict):
            self.app.pathways.create_plan_from_template(
                self.clinic, self.doctor_a, self.patient, "weight", self.doctor_a, "2026-09-28",
                assessment_id=assessment_id, consent_id=consent_id, idempotency_key="plan-key-1")

    def test_template_change_only_affects_new_plans(self):
        v1 = self.publish_template(review_days=28)
        old_plan = self.app.pathways.create_plan_from_template(
            self.clinic, self.doctor_a, self.patient, "weight", self.doctor_a, "2026-09-27",
            assessment_id=self.signed_assessment(), consent_id=self.consent(), idempotency_key="plan-old")
        # 医疗负责人把四周改三周，走新版本与双人审批。
        draft = self.app.pathways.new_draft(self.clinic, self.doctor_a, v1["template_id"])
        self.assertEqual(draft["version_no"], 2)
        self.app.pathways.edit_draft(self.clinic, self.doctor_a, draft["id"],
                                     programs=["weight"], sections=["measurements", "answers"],
                                     nodes=template_nodes(21))
        self.app.pathways.submit_for_review(self.clinic, self.doctor_a, draft["id"], note="复核间隔改三周")
        v2 = self.app.pathways.review(self.clinic, self.doctor_b, draft["id"], "approved", "同意")
        v1_refreshed = self.app.pathways.get_version(self.clinic, self.doctor_b, v1["id"])
        self.assertEqual(v1_refreshed["state"], "superseded")
        # 旧计划仍固定引用 v1、节点仍是 +28 天。
        old_milestones = self.app.milestones.list_for_plan(self.clinic, self.doctor_a, old_plan["id"])
        self.assertEqual([m["due_at"] for m in old_milestones if m["template_node_code"] == "first_review"],
                         ["2026-10-25"])
        # 新计划引用 v2、节点变为 +21 天。
        new_plan = self.app.pathways.create_plan_from_template(
            self.clinic, self.doctor_a, self.patient, "weight", self.doctor_a, "2026-09-27",
            assessment_id=self.signed_assessment(), consent_id=self.consent(), idempotency_key="plan-new")
        self.assertEqual(new_plan["pathway_template_version_id"], v2["id"])
        self.assertEqual([m["due_at"] for m in new_plan["milestones"] if m["template_node_code"] == "first_review"],
                         ["2026-10-18"])

    def test_plan_without_published_template_or_required_assessment_is_rejected(self):
        with self.assertRaises(Conflict):
            self.app.pathways.create_plan_from_template(
                self.clinic, self.doctor_a, self.patient, "weight", self.doctor_a, "2026-09-27",
                idempotency_key="plan-none")
        self.publish_template(sections=("measurements", "answers", "screening"))
        with self.assertRaises(Conflict):
            self.app.pathways.create_plan_from_template(
                self.clinic, self.doctor_a, self.patient, "weight", self.doctor_a, "2026-09-27",
                assessment_id=self.signed_assessment(answers={"history.chief_complaint": "x"}),
                consent_id=self.consent(), idempotency_key="plan-missing-section")

    # ------------------------------------------------------------ 迁移审批

    def test_in_flight_plan_migrates_only_after_individual_approval(self):
        v1 = self.publish_template(review_days=28)
        plan = self.app.pathways.create_plan_from_template(
            self.clinic, self.doctor_a, self.patient, "weight", self.doctor_a, "2026-09-27",
            assessment_id=self.signed_assessment(), consent_id=self.consent(), idempotency_key="plan-mig")
        self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], 1, "propose")
        self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], 2, "activate")
        draft = self.app.pathways.new_draft(self.clinic, self.doctor_a, v1["template_id"])
        self.app.pathways.edit_draft(self.clinic, self.doctor_a, draft["id"],
                                     programs=["weight"], sections=["measurements", "answers"],
                                     nodes=template_nodes(21))
        self.app.pathways.submit_for_review(self.clinic, self.doctor_a, draft["id"])
        v2 = self.app.pathways.review(self.clinic, self.doctor_b, draft["id"], "approved", "同意")
        request = self.app.pathways.request_migration(
            self.clinic, self.doctor_a, plan["id"], v2["id"], "统一三周复核", idempotency_key="mig-1")
        self.assertEqual(request["state"], "pending")
        # 重复申请返回原单。
        replay = self.app.pathways.request_migration(
            self.clinic, self.doctor_a, plan["id"], v2["id"], "统一三周复核", idempotency_key="mig-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["id"], request["id"])
        # 批准前计划仍引用 v1。
        self.assertEqual(self.app.pathways.list_migrations(self.clinic, self.doctor_b, state="pending")[0]["id"],
                         request["id"])
        decision = self.app.pathways.decide_migration(self.clinic, self.doctor_b, request["id"], "approved",
                                                       "同意该患者按新间隔随访")
        self.assertEqual(decision["state"], "approved")
        migrated = decision["plan"]
        self.assertEqual(migrated["pathway_template_version_id"], v2["id"])
        self.assertEqual([m["due_at"] for m in migrated["milestones"] if m["template_node_code"] == "first_review"],
                         ["2026-10-18"])
        # 计划修订历史保留迁移记录。
        revisions = self.app.plan_history(self.clinic, self.doctor_a, plan["id"])
        self.assertEqual(revisions[-1]["snapshot"]["pathway_template_version_id"], v2["id"])
        # 已结论的申请不能重复审批。
        with self.assertRaises(Conflict):
            self.app.pathways.decide_migration(self.clinic, self.doctor_b, request["id"], "approved", "重复")

    def test_migration_rejection_leaves_plan_unchanged_and_is_per_plan(self):
        v1 = self.publish_template(review_days=28)
        plan_a = self.app.pathways.create_plan_from_template(
            self.clinic, self.doctor_a, self.patient, "weight", self.doctor_a, "2026-09-27",
            assessment_id=self.signed_assessment(), consent_id=self.consent(), idempotency_key="plan-a")
        patient_b = self.app.create_patient(self.clinic, self.coordinator, "pw-002", "王女士")["id"]
        import hashlib
        consent_b = self.app.grant_consent(
            self.clinic, self.doctor_a, patient_b, "weight_program", 1,
            hashlib.sha256(b"weight_program-r1-b").hexdigest())["id"]
        assessment_b = self.app.create_assessment(self.clinic, self.doctor_a, patient_b, "weight",
                                                  {"weight_kg": "70"}, {"history.x": "y"})["id"]
        self.app.sign_assessment(self.clinic, self.doctor_a, assessment_b, expected_version=1)
        plan_b = self.app.pathways.create_plan_from_template(
            self.clinic, self.doctor_a, patient_b, "weight", self.doctor_a, "2026-09-27",
            assessment_id=assessment_b, consent_id=consent_b, idempotency_key="plan-b")
        for plan in (plan_a, plan_b):
            self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], 1, "propose")
            self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], 2, "activate")
        draft = self.app.pathways.new_draft(self.clinic, self.doctor_a, v1["template_id"])
        self.app.pathways.edit_draft(self.clinic, self.doctor_a, draft["id"],
                                     programs=["weight"], sections=["measurements", "answers"],
                                     nodes=template_nodes(21))
        self.app.pathways.submit_for_review(self.clinic, self.doctor_a, draft["id"])
        v2 = self.app.pathways.review(self.clinic, self.doctor_b, draft["id"], "approved", "同意")
        req_a = self.app.pathways.request_migration(self.clinic, self.doctor_a, plan_a["id"], v2["id"], "申请A",
                                                    idempotency_key="mig-a")
        req_b = self.app.pathways.request_migration(self.clinic, self.doctor_a, plan_b["id"], v2["id"], "申请B",
                                                    idempotency_key="mig-b")
        result = self.app.pathways.decide_migration(self.clinic, self.doctor_b, req_a["id"], "rejected", "该患者维持原间隔")
        self.assertEqual(result["state"], "rejected")
        # A 计划保持 v1 与 +28 天；B 计划批准后迁移，证明逐个审批互不影响。
        milestones_a = self.app.milestones.list_for_plan(self.clinic, self.doctor_a, plan_a["id"])
        self.assertEqual([m["due_at"] for m in milestones_a if m["template_node_code"] == "first_review"],
                         ["2026-10-25"])
        self.app.pathways.decide_migration(self.clinic, self.doctor_b, req_b["id"], "approved", "同意")
        milestones_b = self.app.milestones.list_for_plan(self.clinic, self.doctor_a, plan_b["id"])
        self.assertEqual([m["due_at"] for m in milestones_b if m["template_node_code"] == "first_review"],
                         ["2026-10-18"])

    def test_migration_requires_same_template_and_in_flight_plan(self):
        v1 = self.publish_template(code="weight-standard")
        other = self.publish_template(code="weight-alt")
        plan = self.app.pathways.create_plan_from_template(
            self.clinic, self.doctor_a, self.patient, "weight", self.doctor_a, "2026-09-27",
            assessment_id=self.signed_assessment(), consent_id=self.consent(), idempotency_key="plan-x")
        # 草稿状态不是在途计划。
        with self.assertRaises(Conflict):
            self.app.pathways.request_migration(self.clinic, self.doctor_a, plan["id"], other["id"], "跨模板",
                                                idempotency_key="mig-x1")
        self.app.transition_plan(self.clinic, self.doctor_a, plan["id"], 1, "propose")
        with self.assertRaises(Conflict):
            self.app.pathways.request_migration(self.clinic, self.doctor_a, plan["id"], other["id"], "跨模板",
                                                idempotency_key="mig-x2")
        # 不能迁到当前已引用版本。
        with self.assertRaises(Conflict):
            self.app.pathways.request_migration(self.clinic, self.doctor_a, plan["id"], v1["id"], "同版本",
                                                idempotency_key="mig-x3")

    # ------------------------------------------------------------ 撤回保护

    def test_referenced_version_cannot_be_withdrawn_but_content_is_retained(self):
        v1 = self.publish_template()
        plan = self.app.pathways.create_plan_from_template(
            self.clinic, self.doctor_a, self.patient, "weight", self.doctor_a, "2026-09-27",
            assessment_id=self.signed_assessment(), consent_id=self.consent(), idempotency_key="plan-keep")
        with self.assertRaises(Conflict):
            self.app.pathways.withdraw_version(self.clinic, self.doctor_b, v1["id"], "尝试撤回已引用版本")
        # 发布 v2 后，v1 被取代；v2 一旦被新计划引用同样不能撤回。
        draft = self.app.pathways.new_draft(self.clinic, self.doctor_a, v1["template_id"])
        self.app.pathways.submit_for_review(self.clinic, self.doctor_a, draft["id"])
        v2 = self.app.pathways.review(self.clinic, self.doctor_b, draft["id"], "approved", "同意")
        self.app.pathways.create_plan_from_template(
            self.clinic, self.doctor_a, self.patient, "weight", self.doctor_a, "2026-09-27",
            assessment_id=self.signed_assessment(), consent_id=self.consent(), idempotency_key="plan-keep-2")
        with self.assertRaises(Conflict):
            self.app.pathways.withdraw_version(self.clinic, self.doctor_b, v2["id"], "仍被引用")
        # 旧版本内容仍完整可查，计划引用不悬空。
        retained = self.app.pathways.get_version(self.clinic, self.doctor_a, v1["id"])
        self.assertEqual(retained["definition"]["nodes"][0]["offset_days"], 21)
        with self.db.transaction(write=False) as connection:
            row = connection.execute("SELECT pathway_template_version_id FROM plans WHERE id=?", (plan["id"],)).fetchone()
            self.assertEqual(row[0], v1["id"])
            content = connection.execute("SELECT definition_json FROM pathway_template_versions WHERE id=?",
                                         (v1["id"],)).fetchone()
            self.assertIn("first_review", content[0])

    def test_unreferced_published_version_can_be_withdrawn(self):
        v1 = self.publish_template(code="weight-temp")
        result = self.app.pathways.withdraw_version(self.clinic, self.doctor_b, v1["id"], "项目暂缓")
        self.assertEqual(result["state"], "withdrawn")
        # 撤回后内容仍保留，但新计划不能再使用。
        self.assertEqual(result["definition"]["programs"], ["weight"])
        with self.assertRaises(Conflict):
            self.app.pathways.create_plan_from_template(
                self.clinic, self.doctor_a, self.patient, "weight", self.doctor_a, "2026-09-27",
                idempotency_key="plan-after-withdraw")

    # ------------------------------------------------------------ 数据校验

    def test_listing_filters_and_audit_chain_stay_intact(self):
        weight = self.publish_template(code="weight-only")
        aesthetic = self.app.pathways.create_template(
            self.clinic, self.doctor_a, "aes-1", "医美路径", ["aesthetic"], ["measurements"],
            [{"code": "review", "kind": "review", "title": "复诊", "offset_days": 14}])
        items = self.app.pathways.list_templates(self.clinic, self.doctor_a, program="weight")
        self.assertEqual([item["code"] for item in items], ["weight-only"])
        versions = self.app.pathways.list_versions(self.clinic, self.doctor_a, weight["template_id"])
        self.assertEqual([(v["version_no"], v["state"]) for v in versions], [(1, "published")])
        self.assertEqual(aesthetic["template_id"] in {item["id"] for item in items}, False)
        self.assertTrue(self.app.verify_audit(self.clinic, self.owner)["ok"])

    def test_schema_migration_adds_template_columns_to_existing_database(self):
        # 既有库在升级后仍可读取，新列允许为空。
        with self.db.transaction(write=False) as connection:
            plan_columns = {row[1] for row in connection.execute("PRAGMA table_info(plans)").fetchall()}
            milestone_columns = {row[1] for row in connection.execute("PRAGMA table_info(plan_milestones)").fetchall()}
        self.assertIn("pathway_template_version_id", plan_columns)
        self.assertIn("template_node_code", milestone_columns)


if __name__ == "__main__":
    unittest.main()
