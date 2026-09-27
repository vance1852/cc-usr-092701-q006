"""版本化、双人审批的诊疗路径模板。

临床负责人定义模板内容（适用项目、评估章节、节点与时间间隔），另一名有
权限的医生审批后版本才生效。计划建立时固定引用当时生效的版本并一次性生成
节点；模板后续修改只影响新计划，在途计划须由临床负责人逐个批准才能迁移。
被引用的已发布版本不能撤回删除；草稿作者不能审批自己的草稿。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import audit
from .db import Database, decode_json, encode_json
from .errors import Conflict, Forbidden, NotFound, ValidationError
from .ids import new_id, require_id, require_idempotency_key
from .security import authorize, principal_for
from .validation import (
    calendar_date,
    choice,
    object_value,
    parsed_timestamp,
    request_digest,
    text,
    timestamp,
)

PROGRAMS = {"aesthetic", "weight", "wellbeing"}
ASSESSMENT_SECTIONS = {"measurements", "answers", "history", "screening", "risk", "goals"}
NODE_KINDS = {"review", "measurement", "followup", "preparation", "recovery_check", "other"}
VERSION_STATES = {"draft", "pending_review", "published", "rejected", "superseded", "withdrawn"}
IN_FLIGHT_PLAN_STATES = {"proposed", "active", "paused"}


class PathwayTemplateService:
    def __init__(self, database: Database, clock, record_plan_revision=None):
        self.db = database
        self.clock = clock
        # 由应用服务注入，保证模板建计划与手工建计划留下一致的修订历史。
        self._record_plan_revision = record_plan_revision

    # ------------------------------------------------------------------ 内容

    def _definition(self, programs: list[str], sections: list[str], nodes: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(programs, list) or not programs:
            raise ValidationError("模板至少包含一个适用项目")
        if len(programs) > len(PROGRAMS):
            raise ValidationError("适用项目重复或超出范围")
        normalized_programs = [choice(item, "适用项目", PROGRAMS) for item in programs]
        if len(set(normalized_programs)) != len(normalized_programs):
            raise ValidationError("适用项目不能重复")
        if not isinstance(sections, list) or not sections:
            raise ValidationError("模板至少要求一个评估章节")
        normalized_sections = [choice(item, "评估章节", ASSESSMENT_SECTIONS) for item in sections]
        if len(set(normalized_sections)) != len(normalized_sections):
            raise ValidationError("评估章节不能重复")
        if not isinstance(nodes, list) or not nodes:
            raise ValidationError("模板至少包含一个计划节点")
        if len(nodes) > 60:
            raise ValidationError("计划节点数量不得超过 60")
        normalized_nodes = []
        seen_codes = set()
        for index, raw in enumerate(nodes):
            node = object_value(raw, "计划节点", allowed={"code", "kind", "title", "offset_days", "required"})
            label = f"第{index + 1}个计划节点"
            code = text(node.get("code", ""), f"{label}编号", maximum=60)
            if not all(ch.isalnum() or ch in "_-" for ch in code) or code in seen_codes:
                raise ValidationError(f"{label}编号无效或重复")
            kind = choice(node.get("kind", ""), f"{label}类型", NODE_KINDS)
            title = text(node.get("title", ""), f"{label}名称", maximum=160)
            offset = node.get("offset_days")
            if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 3650:
                raise ValidationError(f"{label}时间间隔必须为 0 至 3650 天的整数")
            required = node.get("required", True)
            if not isinstance(required, bool):
                raise ValidationError(f"{label}必做标记必须为布尔值")
            seen_codes.add(code)
            normalized_nodes.append({"code": code, "kind": kind, "title": title,
                                     "offset_days": offset, "required": required})
        return {"programs": normalized_programs, "assessment_sections": normalized_sections,
                "nodes": normalized_nodes}

    @staticmethod
    def _definition_digest(definition: dict[str, Any]) -> str:
        return request_digest(definition)

    # ---------------------------------------------------------- 模板与草稿

    def create_template(self, clinic_id: str, actor_id: str, code: str, name: str,
                        programs: list[str], sections: list[str], nodes: list[dict[str, Any]],
                        *, change_note: str | None = None) -> dict[str, Any]:
        """建立模板并同时创建首个草稿版本。"""
        code = text(code, "模板编号", maximum=60)
        name = text(name, "模板名称", maximum=160)
        definition = self._definition(programs, sections, nodes)
        template_id, version_id, now = new_id("ptw"), new_id("ptv"), timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "pathway:manage", clinic_id=clinic_id)
            self._require_clinician(principal)
            if connection.execute("SELECT 1 FROM pathway_templates WHERE clinic_id=? AND code=?", (clinic_id, code)).fetchone():
                raise Conflict("模板编号在本诊所内已存在")
            connection.execute(
                "INSERT INTO pathway_templates(id,clinic_id,code,name,programs_json,state,current_version_id,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'active',NULL,?,?,?)",
                (template_id, clinic_id, code, name, encode_json(definition["programs"]), actor_id, now, now))
            connection.execute(
                "INSERT INTO pathway_template_versions(id,template_id,clinic_id,version_no,state,definition_json,content_digest,created_by,created_at,version) "
                "VALUES(?,?,?,1,'draft',?,?,?,?,1)",
                (version_id, template_id, clinic_id, encode_json(definition), self._definition_digest(definition), actor_id, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="pathway_template", aggregate_id=template_id,
                               action="pathway_template.created", occurred_at=now,
                               payload={"code": code, "version_id": version_id, "version_no": 1})
        return self.get_version(clinic_id, actor_id, version_id)

    def edit_draft(self, clinic_id: str, actor_id: str, version_id: str, *,
                   programs: list[str], sections: list[str], nodes: list[dict[str, Any]]) -> dict[str, Any]:
        require_id(version_id, "模板版本编号")
        definition = self._definition(programs, sections, nodes)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "pathway:manage", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM pathway_template_versions WHERE id=? AND clinic_id=?", (version_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("模板版本不存在")
            if row["state"] not in {"draft", "rejected"}:
                raise Conflict("只有草稿或被驳回版本可以修改", details={"state": row["state"]})
            if row["created_by"] != actor_id and principal.role != "owner":
                raise Forbidden("只能修改本人建立的草稿；负责人可另建新版")
            connection.execute(
                "UPDATE pathway_template_versions SET state='draft',definition_json=?,content_digest=?,version=version+1 WHERE id=?",
                (encode_json(definition), self._definition_digest(definition), version_id))
            connection.execute("UPDATE pathway_templates SET updated_at=? WHERE id=?", (now, row["template_id"]))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="pathway_template_version", aggregate_id=version_id,
                               action="pathway_template.draft_edited", occurred_at=now,
                               payload={"version_no": row["version_no"]})
        return self.get_version(clinic_id, actor_id, version_id)

    def new_draft(self, clinic_id: str, actor_id: str, template_id: str) -> dict[str, Any]:
        """以当前已发布版本内容为基础创建下一版草稿；没有在途草稿时才能创建。"""
        require_id(template_id, "模板编号")
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "pathway:manage", clinic_id=clinic_id)
            self._require_clinician(principal)
            template = connection.execute("SELECT * FROM pathway_templates WHERE id=? AND clinic_id=?", (template_id, clinic_id)).fetchone()
            if template is None:
                raise NotFound("模板不存在")
            if template["state"] != "active":
                raise Conflict("已停用模板不能创建新版本")
            in_progress = connection.execute(
                "SELECT id FROM pathway_template_versions WHERE template_id=? AND state IN ('draft','pending_review')",
                (template_id,)).fetchone()
            if in_progress:
                raise Conflict("该模板已有草稿或待审批版本，请先完成或驳回现有版本",
                               details={"version_id": in_progress["id"]})
            base = connection.execute(
                "SELECT * FROM pathway_template_versions WHERE template_id=? ORDER BY version_no DESC LIMIT 1",
                (template_id,)).fetchone()
            if base is None:
                raise Conflict("模板没有可用作基础的版本")
            next_no = base["version_no"] + 1
            version_id = new_id("ptv")
            connection.execute(
                "INSERT INTO pathway_template_versions(id,template_id,clinic_id,version_no,state,definition_json,content_digest,created_by,created_at,version) "
                "VALUES(?,?,?,?,'draft',?,?,?,?,1)",
                (version_id, template_id, clinic_id, next_no, base["definition_json"],
                 base["content_digest"], actor_id, now))
            connection.execute("UPDATE pathway_templates SET updated_at=? WHERE id=?", (now, template_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="pathway_template", aggregate_id=template_id,
                               action="pathway_template.draft_started", occurred_at=now,
                               payload={"version_id": version_id, "version_no": next_no,
                                        "based_on": base["id"]})
        return self.get_version(clinic_id, actor_id, version_id)

    # -------------------------------------------------------------- 审批流

    def submit_for_review(self, clinic_id: str, actor_id: str, version_id: str, *, note: str | None = None) -> dict[str, Any]:
        require_id(version_id, "模板版本编号")
        note = text(note or "提交发布审批", "提交说明", maximum=1000)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "pathway:manage", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM pathway_template_versions WHERE id=? AND clinic_id=?", (version_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("模板版本不存在")
            if row["state"] not in {"draft", "rejected"}:
                raise Conflict("只有草稿或被驳回版本可以提交审批", details={"state": row["state"]})
            connection.execute("UPDATE pathway_template_versions SET state='pending_review',submitted_at=?,version=version+1 WHERE id=?",
                               (now, version_id))
            connection.execute("UPDATE pathway_templates SET updated_at=? WHERE id=?", (now, row["template_id"]))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="pathway_template_version", aggregate_id=version_id,
                               action="pathway_template.submitted", occurred_at=now,
                               payload={"version_no": row["version_no"], "note": note})
        return self.get_version(clinic_id, actor_id, version_id)

    def review(self, clinic_id: str, actor_id: str, version_id: str, decision: str, note: str) -> dict[str, Any]:
        """另一名有相应权限的医生审批；批准即发布，草稿作者不能审批自己的草稿。"""
        require_id(version_id, "模板版本编号")
        decision = choice(decision, "审批结论", {"approved", "rejected"})
        note = text(note, "审批意见", maximum=2000)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "pathway:manage", clinic_id=clinic_id)
            self._require_clinician(principal)
            row = connection.execute("SELECT * FROM pathway_template_versions WHERE id=? AND clinic_id=?", (version_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("模板版本不存在")
            if row["state"] != "pending_review":
                raise Conflict("只有待审批版本可以审批", details={"state": row["state"]})
            if row["created_by"] == actor_id:
                raise Forbidden("审批人不能审核自己提交的草稿")
            approval_id = new_id("pta")
            connection.execute(
                "INSERT INTO pathway_template_approvals(id,version_id,clinic_id,decision,reviewer_id,note,created_at) "
                "VALUES(?,?,?,?,?,?,?)", (approval_id, version_id, clinic_id, decision, actor_id, note, now))
            if decision == "rejected":
                connection.execute("UPDATE pathway_template_versions SET state='rejected',version=version+1 WHERE id=?", (version_id,))
                audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                                   aggregate_type="pathway_template_version", aggregate_id=version_id,
                                   action="pathway_template.rejected", occurred_at=now,
                                   payload={"version_no": row["version_no"], "note": note})
            else:
                previous = connection.execute("SELECT id FROM pathway_template_versions WHERE template_id=? AND state='published'",
                                              (row["template_id"],)).fetchall()
                connection.execute("UPDATE pathway_template_versions SET state='superseded' WHERE template_id=? AND state='published'",
                                   (row["template_id"],))
                connection.execute(
                    "UPDATE pathway_template_versions SET state='published',published_at=?,published_by=?,version=version+1 WHERE id=?",
                    (now, actor_id, version_id))
                connection.execute(
                    "UPDATE pathway_templates SET current_version_id=?,updated_at=?,version=version+1 WHERE id=?",
                    (version_id, now, row["template_id"]))
                audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                                   aggregate_type="pathway_template_version", aggregate_id=version_id,
                                   action="pathway_template.published", occurred_at=now,
                                   payload={"template_id": row["template_id"], "version_no": row["version_no"],
                                            "superseded": [item["id"] for item in previous], "note": note})
        return self.get_version(clinic_id, actor_id, version_id)

    def withdraw_version(self, clinic_id: str, actor_id: str, version_id: str, reason: str) -> dict[str, Any]:
        """撤回已发布版本；被计划引用的版本只能停用、不能删除内容。"""
        require_id(version_id, "模板版本编号")
        reason = text(reason, "撤回原因", maximum=1000)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "pathway:manage", clinic_id=clinic_id)
            self._require_clinician(principal)
            row = connection.execute("SELECT * FROM pathway_template_versions WHERE id=? AND clinic_id=?", (version_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("模板版本不存在")
            if row["state"] != "published":
                raise Conflict("只有已发布版本可以撤回", details={"state": row["state"]})
            referenced = connection.execute("SELECT 1 FROM plans WHERE pathway_template_version_id=? LIMIT 1", (version_id,)).fetchone()
            if referenced:
                raise Conflict("该版本已被诊疗计划引用，不能撤回删除；如需停止用于新计划，请停用模板",
                               details={"reason": "referenced_by_plans"})
            template = connection.execute("SELECT * FROM pathway_templates WHERE id=?", (row["template_id"],)).fetchone()
            connection.execute(
                "UPDATE pathway_template_versions SET state='withdrawn',withdrawn_at=?,withdrawn_by=?,withdrawal_reason=?,version=version+1 WHERE id=?",
                (now, actor_id, reason, version_id))
            if template["current_version_id"] == version_id:
                # 模板保持可用状态，但暂时没有生效版本，新计划会被拒绝，直到新版本发布。
                connection.execute("UPDATE pathway_templates SET current_version_id=NULL,updated_at=?,version=version+1 WHERE id=?",
                                   (now, row["template_id"]))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="pathway_template_version", aggregate_id=version_id,
                               action="pathway_template.withdrawn", occurred_at=now,
                               payload={"version_no": row["version_no"], "reason": reason})
        return self.get_version(clinic_id, actor_id, version_id)

    # --------------------------------------------------------------- 查询

    def list_templates(self, clinic_id: str, actor_id: str, *, program: str | None = None) -> list[dict[str, Any]]:
        if program is not None:
            program = choice(program, "适用项目", PROGRAMS)
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "pathway:manage", clinic_id=clinic_id)
            rows = connection.execute(
                "SELECT * FROM pathway_templates WHERE clinic_id=? ORDER BY code", (clinic_id,)).fetchall()
            results = []
            for row in rows:
                programs = decode_json(row["programs_json"])
                if program and program not in programs:
                    continue
                current = connection.execute(
                    "SELECT version_no,state,published_at FROM pathway_template_versions WHERE id=?",
                    (row["current_version_id"],)).fetchone() if row["current_version_id"] else None
                results.append({"id": row["id"], "code": row["code"], "name": row["name"], "programs": programs,
                                "state": row["state"], "current_version_id": row["current_version_id"],
                                "current_version_no": current["version_no"] if current else None,
                                "current_published_at": current["published_at"] if current else None,
                                "version": row["version"]})
            return results

    def list_versions(self, clinic_id: str, actor_id: str, template_id: str) -> list[dict[str, Any]]:
        require_id(template_id, "模板编号")
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "pathway:manage", clinic_id=clinic_id)
            if connection.execute("SELECT 1 FROM pathway_templates WHERE id=? AND clinic_id=?", (template_id, clinic_id)).fetchone() is None:
                raise NotFound("模板不存在")
            rows = connection.execute(
                "SELECT * FROM pathway_template_versions WHERE template_id=? ORDER BY version_no", (template_id,)).fetchall()
            return [self._version_summary(row) for row in rows]

    def get_version(self, clinic_id: str, actor_id: str, version_id: str) -> dict[str, Any]:
        require_id(version_id, "模板版本编号")
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "pathway:manage", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM pathway_template_versions WHERE id=? AND clinic_id=?", (version_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("模板版本不存在")
            template = connection.execute("SELECT code,name FROM pathway_templates WHERE id=?", (row["template_id"],)).fetchone()
            approvals = connection.execute(
                "SELECT decision,reviewer_id,note,created_at FROM pathway_template_approvals WHERE version_id=? ORDER BY created_at",
                (version_id,)).fetchall()
            result = self._version_summary(row)
            result["template_code"] = template["code"]
            result["template_name"] = template["name"]
            result["definition"] = decode_json(row["definition_json"])
            result["approvals"] = [dict(item) for item in approvals]
            return result

    @staticmethod
    def _version_summary(row) -> dict[str, Any]:
        return {"id": row["id"], "template_id": row["template_id"], "version_no": row["version_no"],
                "state": row["state"], "content_digest": row["content_digest"], "created_by": row["created_by"],
                "created_at": row["created_at"], "submitted_at": row["submitted_at"],
                "published_at": row["published_at"], "published_by": row["published_by"],
                "withdrawn_at": row["withdrawn_at"], "withdrawal_reason": row["withdrawal_reason"],
                "version": row["version"]}

    def published_for_program(self, connection, clinic_id: str, program: str):
        """解析项目当前生效版本：模板当前指针必须仍指向该已发布版本。"""
        rows = connection.execute(
            "SELECT v.*,t.programs_json AS programs_json FROM pathway_template_versions v "
            "JOIN pathway_templates t ON t.id=v.template_id "
            "WHERE t.clinic_id=? AND v.state='published' AND t.current_version_id=v.id "
            "ORDER BY v.published_at DESC,v.id", (clinic_id,)).fetchall()
        for row in rows:
            if program in decode_json(row["programs_json"]):
                return row
        return None

    # ----------------------------------------------------- 计划建立与迁移

    def create_plan_from_template(self, clinic_id: str, actor_id: str, patient_id: str,
                                  program: str, clinical_owner: str, start_date: str, *,
                                  goal: dict[str, Any] | None = None, risk: dict[str, Any] | None = None,
                                  target_date: str | None = None, assessment_id: str | None = None,
                                  consent_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        """按当时生效模板版本建立计划，并一次性生成全部节点。

        重复提交（相同幂等键）返回原计划与原节点集合，不会再加一套节点。
        """
        program = choice(program, "适用项目", PROGRAMS)
        key = require_idempotency_key(idempotency_key or "")
        start = calendar_date(start_date, "开始日期")
        goal_body = object_value(goal or {"description": "按路径模板执行"}, "目标",
                                 allowed={"description", "measure", "target", "review_interval_days"})
        risk_body = object_value(risk or {}, "风险摘要",
                                 allowed={"screening", "contraindications", "review_required", "notes"})
        if "description" not in goal_body:
            goal_body["description"] = "按路径模板执行"
        goal_body["description"] = text(goal_body["description"], "目标说明", maximum=1000)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "clinical:write", clinic_id=clinic_id)
            existing = connection.execute(
                "SELECT response_json FROM idempotency WHERE scope='pathway_plan' AND key=?", (key,)).fetchone()
            if existing:
                cached = decode_json(existing["response_json"])
                expected = {"clinic_id": clinic_id, "patient_id": patient_id, "program": program,
                            "clinical_owner": clinical_owner, "start_date": start,
                            "target_date": target_date, "assessment_id": assessment_id,
                            "consent_id": consent_id}
                if cached["request_hash"] != request_digest(expected):
                    raise Conflict("幂等编号已被不同的建计划请求使用")
                plan_row = connection.execute("SELECT * FROM plans WHERE id=? AND clinic_id=?",
                                              (cached["plan_id"], clinic_id)).fetchone()
                if plan_row is None:
                    raise Conflict("原计划已不存在，幂等结果无法复用")
                return self._plan_result(connection, plan_row, replayed=True)
            version = self.published_for_program(connection, clinic_id, program)
            if version is None:
                raise Conflict("该项目当前没有已发布并生效的路径模板", details={"program": program})
            template = connection.execute("SELECT * FROM pathway_templates WHERE id=?", (version["template_id"],)).fetchone()
            definition = decode_json(version["definition_json"])
            patient = connection.execute("SELECT * FROM patients WHERE id=? AND clinic_id=?", (patient_id, clinic_id)).fetchone()
            owner = connection.execute("SELECT * FROM staff WHERE id=? AND clinic_id=? AND active=1", (clinical_owner, clinic_id)).fetchone()
            if patient is None:
                raise NotFound("患者不存在")
            if patient["state"] != "active":
                raise Conflict("非在诊患者不能建立新计划")
            if owner is None or owner["role"] not in {"clinician", "owner"}:
                raise ValidationError("临床负责人必须是有效的医生或负责人")
            self._check_assessment_sections(connection, patient_id, assessment_id, definition, program)
            consent_row = self._check_consent(connection, patient_id, program, consent_id, now)
            target = calendar_date(target_date, "目标日期") if target_date else None
            if target and target < start:
                raise ValidationError("目标日期不能早于开始日期")
            plan_id = new_id("pln")
            connection.execute(
                "INSERT INTO plans(id,patient_id,clinic_id,kind,state,created_by,clinical_owner,assessment_id,consent_id,"
                "goal_json,risk_json,start_date,target_date,pathway_template_version_id,created_at,updated_at) "
                "VALUES(?,?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, patient_id, clinic_id, program, actor_id, clinical_owner, assessment_id,
                 consent_row["id"] if consent_row else None, encode_json(goal_body), encode_json(risk_body),
                 start, target, version["id"], now, now))
            milestones = self._instantiate_nodes(connection, definition, plan_id, clinic_id, patient_id, start, actor_id, now)
            if self._record_plan_revision is not None:
                self._record_plan_revision(connection, plan_id, 1, actor_id,
                                           f"按路径模板 {template['code']} v{version['version_no']} 建立", now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                               aggregate_type="plan", aggregate_id=plan_id, action="plan.created_from_pathway",
                               occurred_at=now, payload={"kind": program, "pathway_template_id": template["id"],
                                                         "pathway_version_id": version["id"],
                                                         "pathway_version_no": version["version_no"],
                                                         "milestone_count": len(milestones)})
            plan_row = connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            result = self._plan_result(connection, plan_row, replayed=False)
            cached = {"request_hash": request_digest(
                {"clinic_id": clinic_id, "patient_id": patient_id, "program": program,
                 "clinical_owner": clinical_owner, "start_date": start, "target_date": target,
                 "assessment_id": assessment_id, "consent_id": consent_id}),
                "plan_id": plan_id}
            connection.execute(
                "INSERT INTO idempotency(scope,key,request_hash,response_json,created_at) VALUES('pathway_plan',?,?,?,?)",
                (key, cached["request_hash"], encode_json(cached), now))
            return result

    @staticmethod
    def _check_consent(connection, patient_id: str, program: str, consent_id: str | None, now: str):
        required_purpose = {"aesthetic": "aesthetic_procedure", "weight": "weight_program"}.get(program)
        if not required_purpose:
            if consent_id:
                consent = connection.execute("SELECT * FROM consents WHERE id=? AND patient_id=?", (consent_id, patient_id)).fetchone()
                if consent is None:
                    raise NotFound("授权不存在")
                return consent
            return None
        consent = connection.execute(
            "SELECT * FROM consents WHERE id=? AND patient_id=? AND purpose=? AND state='granted'",
            (consent_id, patient_id, required_purpose)).fetchone() if consent_id else None
        if consent is None:
            raise Conflict("计划需要当前有效的对应授权")
        if consent["expires_at"] and parsed_timestamp(consent["expires_at"]) <= parsed_timestamp(now):
            raise Conflict("计划需要当前有效的对应授权")
        return consent

    @staticmethod
    def _check_assessment_sections(connection, patient_id: str, assessment_id: str | None,
                                   definition: dict[str, Any], program: str) -> None:
        required_sections = set(definition["assessment_sections"])
        if assessment_id is None:
            if required_sections:
                raise Conflict("该路径模板要求引用已完成所需章节的评估")
            return
        assessment = connection.execute(
            "SELECT * FROM assessments WHERE id=? AND patient_id=?", (assessment_id, patient_id)).fetchone()
        if assessment is None or assessment["status"] != "signed":
            raise Conflict("计划引用的评估不存在或尚未签署")
        present = {"measurements", "answers"}
        payload_measurements = decode_json(assessment["measurements_json"])
        payload_answers = decode_json(assessment["answers_json"])
        if not payload_measurements:
            present.discard("measurements")
        if not payload_answers:
            present.discard("answers")
        missing = required_sections - present
        # history/screening/risk/goals 等章节以问卷键名约定标记，由签署评估承载。
        for section in tuple(missing):
            if section in ASSESSMENT_SECTIONS - {"measurements", "answers"}:
                if any(key.startswith(f"{section}.") or key == section for key in payload_answers):
                    missing.discard(section)
        if missing:
            raise Conflict("评估未覆盖模板要求的章节", details={"missing_sections": sorted(missing)})

    def _instantiate_nodes(self, connection, definition: dict[str, Any], plan_id: str, clinic_id: str,
                           patient_id: str, start_date: str, actor_id: str, now: str) -> list[dict[str, Any]]:
        start = date.fromisoformat(start_date)
        created = []
        for node in definition["nodes"]:
            due_date = (start + timedelta(days=node["offset_days"])).isoformat()
            milestone_id = new_id("msl")
            idem = f"pathway:{plan_id}:{node['code']}"
            connection.execute(
                "INSERT INTO plan_milestones(id,plan_id,kind,title,due_at,state,assigned_to,idempotency_key,"
                "created_by,created_at,updated_at,template_node_code) "
                "VALUES(?,?,?,?,?,'pending',NULL,?,?,?,?,?)",
                (milestone_id, plan_id, node["kind"], node["title"], due_date, idem, actor_id, now, now, node["code"]))
            connection.execute(
                "INSERT INTO milestone_events(id,milestone_id,sequence,action,actor_id,reason,prior_due_at,next_due_at,occurred_at) "
                "VALUES(?,?,1,'created',?,?,?,?,?)",
                (new_id("mse"), milestone_id, actor_id, f"按路径模板生成：{node['title']}", None, due_date, now))
            item = {"id": milestone_id, "plan_id": plan_id, "kind": node["kind"], "title": node["title"],
                    "due_at": due_date, "state": "pending", "assigned_to": None,
                    "template_node_code": node["code"], "required": node["required"], "version": 1}
            created.append(item)
        audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                           aggregate_type="plan", aggregate_id=plan_id, action="pathway.milestones_generated",
                           occurred_at=now, payload={"count": len(created),
                                                     "node_codes": [n["code"] for n in definition["nodes"]]})
        return created

    def _plan_result(self, connection, plan_row, *, replayed: bool) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT * FROM plan_milestones WHERE plan_id=? ORDER BY due_at,template_node_code,id", (plan_row["id"],)).fetchall()
        milestones = [{"id": row["id"], "kind": row["kind"], "title": row["title"], "due_at": row["due_at"],
                       "state": row["state"], "assigned_to": row["assigned_to"],
                       "template_node_code": row["template_node_code"], "version": row["version"]} for row in rows]
        version = connection.execute(
            "SELECT id,version_no,state,content_digest,published_at FROM pathway_template_versions WHERE id=?",
            (plan_row["pathway_template_version_id"],)).fetchone()
        return {"id": plan_row["id"], "patient_id": plan_row["patient_id"], "kind": plan_row["kind"],
                "state": plan_row["state"], "start_date": plan_row["start_date"], "target_date": plan_row["target_date"],
                "version": plan_row["version"], "replayed": replayed,
                "pathway_template_version_id": plan_row["pathway_template_version_id"],
                "pathway_version": {"id": version["id"], "version_no": version["version_no"],
                                    "state": version["state"], "content_digest": version["content_digest"],
                                    "published_at": version["published_at"]},
                "milestones": milestones}

    # --------------------------------------------------------------- 迁移

    def request_migration(self, clinic_id: str, actor_id: str, plan_id: str, target_version_id: str,
                          reason: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        """申请将在途计划迁移到同一模板的更新已发布版本；须逐个计划申请。"""
        require_id(plan_id, "诊疗计划编号")
        require_id(target_version_id, "目标模板版本编号")
        reason = text(reason, "迁移原因", maximum=1000)
        key = require_idempotency_key(idempotency_key or "")
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "clinical:write", clinic_id=clinic_id)
            existing_req = connection.execute("SELECT * FROM idempotency WHERE scope='pathway_migration' AND key=?", (key,)).fetchone()
            if existing_req:
                cached = decode_json(existing_req["response_json"])
                if cached["request_hash"] != request_digest({"plan_id": plan_id, "target_version_id": target_version_id}):
                    raise Conflict("幂等编号已被不同的迁移申请使用")
                migration = connection.execute("SELECT * FROM plan_template_migrations WHERE id=? AND clinic_id=?",
                                               (cached["migration_id"], clinic_id)).fetchone()
                if migration is None:
                    raise Conflict("原迁移申请已不存在，幂等结果无法复用")
                return self._migration_result(migration, replayed=True)
            plan = connection.execute("SELECT * FROM plans WHERE id=? AND clinic_id=?", (plan_id, clinic_id)).fetchone()
            if plan is None:
                raise NotFound("诊疗计划不存在")
            if plan["state"] not in IN_FLIGHT_PLAN_STATES:
                raise Conflict("只有在途计划（提议、生效、暂停）可以申请迁移", details={"state": plan["state"]})
            if not plan["pathway_template_version_id"]:
                raise Conflict("该计划未引用路径模板，不能迁移")
            source = connection.execute("SELECT * FROM pathway_template_versions WHERE id=?",
                                        (plan["pathway_template_version_id"],)).fetchone()
            target = connection.execute("SELECT * FROM pathway_template_versions WHERE id=? AND clinic_id=?",
                                        (target_version_id, clinic_id)).fetchone()
            if target is None:
                raise NotFound("目标模板版本不存在")
            if target["state"] != "published":
                raise Conflict("只能迁移到已发布版本", details={"state": target["state"]})
            if source["template_id"] != target["template_id"]:
                raise Conflict("迁移只能在同一模板的不同版本之间进行")
            if source["id"] == target["id"]:
                raise Conflict("计划已经引用该版本")
            pending = connection.execute(
                "SELECT id FROM plan_template_migrations WHERE plan_id=? AND state='pending'", (plan_id,)).fetchone()
            if pending:
                raise Conflict("该计划已有待审批的迁移申请", details={"migration_id": pending["id"]})
            migration_id = new_id("ptm")
            connection.execute(
                "INSERT INTO plan_template_migrations(id,clinic_id,plan_id,from_version_id,to_version_id,state,"
                "requested_by,requested_at,reason) VALUES(?,?,?,?,?,'pending',?,?,?)",
                (migration_id, clinic_id, plan_id, source["id"], target["id"], actor_id, now, reason))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=plan["patient_id"],
                               aggregate_type="plan", aggregate_id=plan_id, action="pathway.migration_requested",
                               occurred_at=now, payload={"migration_id": migration_id,
                                                         "from_version_id": source["id"], "to_version_id": target["id"],
                                                         "reason": reason})
            row = connection.execute("SELECT * FROM plan_template_migrations WHERE id=?", (migration_id,)).fetchone()
            cached = {"request_hash": request_digest({"plan_id": plan_id, "target_version_id": target_version_id}),
                      "migration_id": migration_id}
            connection.execute(
                "INSERT INTO idempotency(scope,key,request_hash,response_json,created_at) VALUES('pathway_migration',?,?,?,?)",
                (key, cached["request_hash"], encode_json(cached), now))
            return self._migration_result(row, replayed=False)

    def decide_migration(self, clinic_id: str, actor_id: str, migration_id: str, decision: str,
                         note: str, *, expected_version: int | None = None) -> dict[str, Any]:
        """临床负责人逐个批准/驳回迁移；批准后按新版本重算节点。"""
        require_id(migration_id, "迁移申请编号")
        decision = choice(decision, "迁移结论", {"approved", "rejected"})
        note = text(note, "审批意见", maximum=2000)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "clinical:write", clinic_id=clinic_id)
            if principal.role not in {"clinician", "owner"}:
                raise Forbidden("只有临床负责人可以批准路径迁移")
            row = connection.execute("SELECT * FROM plan_template_migrations WHERE id=? AND clinic_id=?",
                                     (migration_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("迁移申请不存在")
            if row["state"] != "pending":
                raise Conflict("迁移申请已有结论", details={"state": row["state"]})
            plan = connection.execute("SELECT * FROM plans WHERE id=?", (row["plan_id"],)).fetchone()
            if plan is None:
                raise NotFound("诊疗计划不存在")
            if plan["state"] not in IN_FLIGHT_PLAN_STATES:
                raise Conflict("计划已结束，不能再迁移", details={"state": plan["state"]})
            if decision == "rejected":
                connection.execute(
                    "UPDATE plan_template_migrations SET state='rejected',decided_by=?,decided_at=?,decision_note=? WHERE id=?",
                    (actor_id, now, note, migration_id))
                audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=plan["patient_id"],
                                   aggregate_type="plan", aggregate_id=plan["id"], action="pathway.migration_rejected",
                                   occurred_at=now, payload={"migration_id": migration_id, "note": note})
            else:
                target = connection.execute("SELECT * FROM pathway_template_versions WHERE id=?", (row["to_version_id"],)).fetchone()
                if target is None or target["state"] != "published":
                    raise Conflict("目标版本已不是已发布状态")
                self._apply_migration(connection, plan, target, actor_id, note, now)
                connection.execute(
                    "UPDATE plan_template_migrations SET state='approved',decided_by=?,decided_at=?,decision_note=? WHERE id=?",
                    (actor_id, now, note, migration_id))
            refreshed = connection.execute("SELECT * FROM plan_template_migrations WHERE id=?", (migration_id,)).fetchone()
            result = self._migration_result(refreshed, replayed=False)
            if decision == "approved":
                plan_row = connection.execute("SELECT * FROM plans WHERE id=?", (plan["id"],)).fetchone()
                result["plan"] = self._plan_result(connection, plan_row, replayed=False)
            return result

    def _apply_migration(self, connection, plan, target, actor_id: str, note: str, now: str) -> None:
        definition = decode_json(target["definition_json"])
        rows = connection.execute("SELECT * FROM plan_milestones WHERE plan_id=? ORDER BY id", (plan["id"],)).fetchall()
        # 只与仍待完成的节点做对配；已完成、取消、豁免的节点保留为历史，不阻止同代码节点重新生成。
        existing_by_code = {row["template_node_code"]: row for row in rows
                            if row["template_node_code"] and row["state"] == "pending"}
        matched, added, cancelled = [], [], []
        for node in definition["nodes"]:
            due_date = (date.fromisoformat(plan["start_date"]) + timedelta(days=node["offset_days"])).isoformat()
            current = existing_by_code.pop(node["code"], None)
            if current is None:
                milestone_id = new_id("msl")
                idem = f"pathway:{plan['id']}:{node['code']}@v{target['version_no']}"
                connection.execute(
                    "INSERT INTO plan_milestones(id,plan_id,kind,title,due_at,state,assigned_to,idempotency_key,"
                    "created_by,created_at,updated_at,template_node_code) "
                    "VALUES(?,?,?,?,?,'pending',NULL,?,?,?,?,?)",
                    (milestone_id, plan["id"], node["kind"], node["title"], due_date, idem, actor_id, now, now, node["code"]))
                connection.execute(
                    "INSERT INTO milestone_events(id,milestone_id,sequence,action,actor_id,reason,prior_due_at,next_due_at,occurred_at) "
                    "VALUES(?,?,1,'created',?,?,?,?,?)",
                    (new_id("mse"), milestone_id, actor_id, f"迁移至模板 v{target['version_no']} 新增节点", None, due_date, now))
                added.append(node["code"])
            else:
                if current["state"] == "pending":
                    if current["due_at"] != due_date or current["title"] != node["title"] or current["kind"] != node["kind"]:
                        connection.execute(
                            "UPDATE plan_milestones SET due_at=?,title=?,kind=?,updated_at=?,version=version+1 WHERE id=?",
                            (due_date, node["title"], node["kind"], now, current["id"]))
                        if current["due_at"] != due_date:
                            seq = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM milestone_events WHERE milestone_id=?",
                                                     (current["id"],)).fetchone()[0]
                            connection.execute(
                                "INSERT INTO milestone_events(id,milestone_id,sequence,action,actor_id,reason,prior_due_at,next_due_at,occurred_at) "
                                "VALUES(?,?,?,'reschedule',?,?,?,?,?)",
                                (new_id("mse"), current["id"], seq, actor_id,
                                 f"批准迁移至模板 v{target['version_no']}", current["due_at"], due_date, now))
                matched.append(node["code"])
        for old_code, old_row in existing_by_code.items():
            if old_row["state"] == "pending":
                connection.execute(
                    "UPDATE plan_milestones SET state='cancelled',updated_at=?,version=version+1 WHERE id=?",
                    (now, old_row["id"]))
                seq = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM milestone_events WHERE milestone_id=?",
                                         (old_row["id"],)).fetchone()[0]
                connection.execute(
                    "INSERT INTO milestone_events(id,milestone_id,sequence,action,actor_id,reason,prior_due_at,next_due_at,occurred_at) "
                    "VALUES(?,?,?,'cancel',?,?,?,?,?)",
                    (new_id("mse"), old_row["id"], seq, actor_id,
                     f"模板 v{target['version_no']} 已移除该节点", old_row["due_at"], old_row["due_at"], now))
                cancelled.append(old_code)
        new_version = plan["version"] + 1
        connection.execute(
            "UPDATE plans SET pathway_template_version_id=?,updated_at=?,version=? WHERE id=?",
            (target["id"], now, new_version, plan["id"]))
        plan_revision = {
            "kind": plan["kind"], "state": plan["state"], "clinical_owner": plan["clinical_owner"],
            "assessment_id": plan["assessment_id"], "consent_id": plan["consent_id"], "goal_json": plan["goal_json"],
            "risk_json": plan["risk_json"], "start_date": plan["start_date"], "target_date": plan["target_date"],
            "pathway_template_version_id": target["id"], "version": new_version,
        }
        connection.execute(
            "INSERT INTO plan_revisions(plan_id,revision,snapshot_json,changed_by,change_reason,created_at) VALUES(?,?,?,?,?,?)",
            (plan["id"], new_version, encode_json(plan_revision), actor_id,
             f"批准迁移至模板 v{target['version_no']}：{note}", now))
        audit.append_event(connection, clinic_id=plan["clinic_id"], actor_id=actor_id, patient_id=plan["patient_id"],
                           aggregate_type="plan", aggregate_id=plan["id"], action="pathway.migration_approved",
                           occurred_at=now, payload={"from_version_id": plan["pathway_template_version_id"],
                                                     "to_version_id": target["id"], "matched": matched,
                                                     "added": added, "cancelled": cancelled, "note": note})

    def list_migrations(self, clinic_id: str, actor_id: str, *, plan_id: str | None = None,
                        state: str | None = None) -> list[dict[str, Any]]:
        if state is not None:
            state = choice(state, "迁移状态", {"pending", "approved", "rejected"})
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinical:read", clinic_id=clinic_id)
            query = "SELECT * FROM plan_template_migrations WHERE clinic_id=?"
            params: list[Any] = [clinic_id]
            if plan_id:
                query += " AND plan_id=?"
                params.append(plan_id)
            if state:
                query += " AND state=?"
                params.append(state)
            query += " ORDER BY requested_at DESC,id"
            rows = connection.execute(query, params).fetchall()
            return [self._migration_result(row, replayed=False) for row in rows]

    @staticmethod
    def _migration_result(row, *, replayed: bool) -> dict[str, Any]:
        return {"id": row["id"], "plan_id": row["plan_id"], "from_version_id": row["from_version_id"],
                "to_version_id": row["to_version_id"], "state": row["state"], "requested_by": row["requested_by"],
                "requested_at": row["requested_at"], "reason": row["reason"], "decided_by": row["decided_by"],
                "decided_at": row["decided_at"], "decision_note": row["decision_note"], "replayed": replayed}

    # --------------------------------------------------------------- 其他

    @staticmethod
    def _require_clinician(principal) -> None:
        if principal.role not in {"clinician", "owner"}:
            raise Forbidden("只有医生或临床负责人可以管理诊疗路径模板")
