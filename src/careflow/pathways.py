"""诊疗路径模板：起草、复核发布、版本绑定与在途计划迁移。

模板版本一经提交即不可改写；发布须由起草人之外、具备复核权限的医生确认。
计划在建计划时固定引用当时生效的版本并一次性生成全部节点；此后的模板
修改只影响新计划，在途计划须由临床负责人逐案批准迁移。撤回版本仅改变
状态，已被引用的内容始终保留，系统不提供任何删除模板内容的入口。
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import audit
from .db import Database, decode_json, encode_json
from .errors import Conflict, Forbidden, NotFound, ValidationError
from .ids import new_id
from .milestones import MILESTONE_KINDS
from .security import authorize, principal_for
from .validation import choice, integer, text, timestamp

PROGRAMS = {"aesthetic", "weight", "wellbeing"}
# 所需评估章节使用就诊病历的章节词汇表，保证模板定义可在签署环节核对。
ASSESSMENT_SECTIONS = {"chief_complaint", "history", "examination", "assessment", "plan", "instructions", "followup"}
_DUE_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_NODES = 50


def normalize_sections(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= len(ASSESSMENT_SECTIONS):
        raise ValidationError("所需评估章节数量须在允许范围内")
    sections = [choice(item, "评估章节", ASSESSMENT_SECTIONS) for item in value]
    if len(set(sections)) != len(sections):
        raise ValidationError("所需评估章节不能重复")
    return sections


def normalize_nodes(value: Any) -> list[dict[str, Any]]:
    """校验并规范化节点定义；返回按间隔排序后的稳定顺序。"""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_NODES:
        raise ValidationError(f"路径节点数量须为 1 至 {MAX_NODES}")
    normalized = []
    for item in value:
        if not isinstance(item, dict):
            raise ValidationError("路径节点必须为对象")
        extra = set(item) - {"kind", "title", "offset_days", "due_time", "assign_to_owner"}
        if extra:
            raise ValidationError("路径节点包含未知字段", details={"fields": sorted(extra)})
        kind = choice(item.get("kind"), "节点类型", MILESTONE_KINDS)
        title = text(item.get("title"), "节点名称", maximum=160)
        offset = integer(item.get("offset_days"), "节点间隔天数", minimum=0, maximum=3650)
        due_time = item.get("due_time", "09:00")
        if not isinstance(due_time, str) or not _DUE_TIME.fullmatch(due_time):
            raise ValidationError("节点时间须为 HH:MM 格式")
        assign = item.get("assign_to_owner", False)
        if not isinstance(assign, bool):
            raise ValidationError("节点负责人标记须为布尔值")
        normalized.append({"kind": kind, "title": title, "offset_days": offset,
                           "due_time": due_time, "assign_to_owner": assign})
    normalized.sort(key=lambda node: (node["offset_days"], node["due_time"], node["title"]))
    return normalized


def published_version(connection, template_id: str):
    return connection.execute(
        "SELECT * FROM pathway_template_versions WHERE template_id=? AND state='published'", (template_id,)
    ).fetchone()


def insert_generated_milestones(connection, *, plan_id: str, start_date: str, timezone_name: str,
                                nodes: list[dict[str, Any]], clinical_owner: str, actor_id: str,
                                now: str, generation: int) -> list[dict[str, Any]]:
    """按模板节点定义一次性生成计划节点；节点时间按计划开始日期与诊所时区换算。"""
    zone = ZoneInfo(timezone_name)
    base = date.fromisoformat(start_date)
    results = []
    for sequence, spec in enumerate(nodes):
        due_date = base + timedelta(days=spec["offset_days"])
        hour, minute = (int(part) for part in spec["due_time"].split(":"))
        due = timestamp(datetime.combine(due_date, time(hour, minute), tzinfo=zone))
        milestone_id = new_id("msl")
        assigned = clinical_owner if spec["assign_to_owner"] else None
        connection.execute(
            "INSERT INTO plan_milestones(id,plan_id,kind,title,due_at,state,assigned_to,idempotency_key,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'pending',?,?,?,?,?)",
            (milestone_id, plan_id, spec["kind"], spec["title"], due, assigned,
             f"pathway:{plan_id}:{generation}:{sequence}", actor_id, now, now))
        connection.execute(
            "INSERT INTO milestone_events(id,milestone_id,sequence,action,actor_id,reason,prior_due_at,next_due_at,occurred_at) "
            "VALUES(?,?,1,'created',?,?,NULL,?,?)",
            (new_id("mse"), milestone_id, actor_id, "按诊疗路径模板生成", due, now))
        results.append({"id": milestone_id, "plan_id": plan_id, "kind": spec["kind"], "title": spec["title"],
                        "due_at": due, "state": "pending", "assigned_to": assigned, "version": 1})
    return results


class PathwayService:
    def __init__(self, database: Database, clock):
        self.db, self.clock = database, clock

    def _now(self) -> str:
        return timestamp(self.clock.now())

    def _template(self, connection, clinic_id: str, template_id: str):
        row = connection.execute("SELECT * FROM pathway_templates WHERE id=? AND clinic_id=?",
                                 (template_id, clinic_id)).fetchone()
        if row is None:
            raise NotFound("诊疗路径模板不存在")
        return row

    def _version(self, connection, clinic_id: str, version_id: str):
        row = connection.execute(
            "SELECT v.* FROM pathway_template_versions v JOIN pathway_templates t ON t.id=v.template_id "
            "WHERE v.id=? AND t.clinic_id=?", (version_id, clinic_id)).fetchone()
        if row is None:
            raise NotFound("路径模板版本不存在")
        return row

    @staticmethod
    def _version_view(row, *, bound_plans: int = 0) -> dict[str, Any]:
        return {"id": row["id"], "template_id": row["template_id"], "version": row["version"],
                "state": row["state"], "required_sections": decode_json(row["required_sections_json"]),
                "nodes": decode_json(row["nodes_json"]), "created_by": row["created_by"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "submitted_by": row["submitted_by"], "submitted_at": row["submitted_at"],
                "reviewed_by": row["reviewed_by"], "reviewed_at": row["reviewed_at"],
                "review_note": row["review_note"], "published_at": row["published_at"],
                "withdrawn_by": row["withdrawn_by"], "withdrawn_at": row["withdrawn_at"],
                "withdraw_reason": row["withdraw_reason"], "bound_plan_count": bound_plans}

    def create_template(self, clinic_id: str, actor_id: str, name: str, program: str,
                        required_sections: Any, nodes: Any) -> dict[str, Any]:
        """建立模板并同时起草第 1 版；模板与首版草稿是一个原子操作。"""
        name = text(name, "模板名称", maximum=120)
        program = choice(program, "适用项目", PROGRAMS)
        sections = normalize_sections(required_sections)
        specs = normalize_nodes(nodes)
        template_id, version_id = new_id("pwt"), new_id("pwv")
        now = self._now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "pathway:write", clinic_id=clinic_id)
            if connection.execute("SELECT 1 FROM pathway_templates WHERE clinic_id=? AND name=?",
                                  (clinic_id, name)).fetchone():
                raise Conflict("诊所内已存在同名路径模板")
            connection.execute("INSERT INTO pathway_templates(id,clinic_id,name,program,created_by,created_at) VALUES(?,?,?,?,?,?)",
                               (template_id, clinic_id, name, program, actor_id, now))
            connection.execute(
                "INSERT INTO pathway_template_versions(id,template_id,version,state,required_sections_json,nodes_json,created_by,created_at,updated_at) "
                "VALUES(?,?,1,'draft',?,?,?,?,?)",
                (version_id, template_id, encode_json(sections), encode_json(specs), actor_id, now, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="pathway_template", aggregate_id=template_id,
                               action="pathway.template_created", occurred_at=now,
                               payload={"name": name, "program": program, "version": 1,
                                        "required_sections": sections, "nodes": specs})
        return {"id": template_id, "clinic_id": clinic_id, "name": name, "program": program,
                "created_by": actor_id, "created_at": now,
                "draft_version": {"id": version_id, "version": 1, "state": "draft",
                                  "required_sections": sections, "nodes": specs}}

    def create_version(self, clinic_id: str, actor_id: str, template_id: str,
                       required_sections: Any, nodes: Any) -> dict[str, Any]:
        """在已发布或被驳回的版本基础上起草下一版；同一模板同一时间只允许一个草稿或待审版本。"""
        sections = normalize_sections(required_sections)
        specs = normalize_nodes(nodes)
        version_id = new_id("pwv")
        now = self._now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "pathway:write", clinic_id=clinic_id)
            self._template(connection, clinic_id, template_id)
            open_version = connection.execute(
                "SELECT version,state FROM pathway_template_versions WHERE template_id=? AND state IN ('draft','pending_review')",
                (template_id,)).fetchone()
            if open_version:
                raise Conflict("模板已有待处理的草稿或待审版本，请先完成或驳回后再起草",
                               details={"version": open_version["version"], "state": open_version["state"]})
            number = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM pathway_template_versions WHERE template_id=?",
                                        (template_id,)).fetchone()[0]
            connection.execute(
                "INSERT INTO pathway_template_versions(id,template_id,version,state,required_sections_json,nodes_json,created_by,created_at,updated_at) "
                "VALUES(?,?,?,'draft',?,?,?,?,?)",
                (version_id, template_id, number, encode_json(sections), encode_json(specs), actor_id, now, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="pathway_template_version", aggregate_id=version_id,
                               action="pathway.version_drafted", occurred_at=now,
                               payload={"template_id": template_id, "version": number,
                                        "required_sections": sections, "nodes": specs})
            row = connection.execute("SELECT * FROM pathway_template_versions WHERE id=?", (version_id,)).fetchone()
        return self._version_view(row)

    def update_draft(self, clinic_id: str, actor_id: str, version_id: str,
                     required_sections: Any, nodes: Any) -> dict[str, Any]:
        sections = normalize_sections(required_sections)
        specs = normalize_nodes(nodes)
        now = self._now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "pathway:write", clinic_id=clinic_id)
            row = self._version(connection, clinic_id, version_id)
            if row["state"] != "draft":
                raise Conflict("只有草稿状态的版本可以编辑")
            connection.execute("UPDATE pathway_template_versions SET required_sections_json=?,nodes_json=?,updated_at=? WHERE id=?",
                               (encode_json(sections), encode_json(specs), now, version_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="pathway_template_version", aggregate_id=version_id,
                               action="pathway.version_updated", occurred_at=now,
                               payload={"template_id": row["template_id"], "version": row["version"],
                                        "required_sections": sections, "nodes": specs})
            updated = connection.execute("SELECT * FROM pathway_template_versions WHERE id=?", (version_id,)).fetchone()
        return self._version_view(updated)

    def submit(self, clinic_id: str, actor_id: str, version_id: str) -> dict[str, Any]:
        now = self._now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "pathway:write", clinic_id=clinic_id)
            row = self._version(connection, clinic_id, version_id)
            if row["state"] != "draft":
                raise Conflict("只有草稿版本可以提交复核")
            connection.execute("UPDATE pathway_template_versions SET state='pending_review',submitted_by=?,submitted_at=?,updated_at=? WHERE id=?",
                               (actor_id, now, now, version_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="pathway_template_version", aggregate_id=version_id,
                               action="pathway.version_submitted", occurred_at=now,
                               payload={"template_id": row["template_id"], "version": row["version"]})
        return {"id": version_id, "template_id": row["template_id"], "version": row["version"],
                "state": "pending_review", "submitted_by": actor_id, "submitted_at": now}

    def review(self, clinic_id: str, actor_id: str, version_id: str, action: str, note: str = "") -> dict[str, Any]:
        """复核待审版本。批准即发布并取代当前生效版本；起草人不能复核自己的版本。"""
        action = choice(action, "复核结论", {"approve", "reject"})
        if action == "reject":
            note = text(note, "复核意见", maximum=1000)
        else:
            note = text(note, "复核意见", minimum=0, maximum=1000) if note else ""
        now = self._now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "pathway:review", clinic_id=clinic_id)
            row = self._version(connection, clinic_id, version_id)
            if row["state"] != "pending_review":
                raise Conflict("只有待复核状态的版本可以复核")
            if row["created_by"] == actor_id:
                raise Forbidden("复核人不能批准或驳回自己起草的模板版本")
            superseded = None
            if action == "approve":
                current = published_version(connection, row["template_id"])
                if current:
                    superseded = current["id"]
                    connection.execute("UPDATE pathway_template_versions SET state='superseded',updated_at=? WHERE id=?",
                                       (now, current["id"]))
                connection.execute(
                    "UPDATE pathway_template_versions SET state='published',reviewed_by=?,reviewed_at=?,review_note=?,published_at=?,updated_at=? WHERE id=?",
                    (actor_id, now, note, now, now, version_id))
            else:
                connection.execute(
                    "UPDATE pathway_template_versions SET state='rejected',reviewed_by=?,reviewed_at=?,review_note=?,updated_at=? WHERE id=?",
                    (actor_id, now, note, now, version_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="pathway_template_version", aggregate_id=version_id,
                               action=f"pathway.version_{'approved' if action == 'approve' else 'rejected'}",
                               occurred_at=now,
                               payload={"template_id": row["template_id"], "version": row["version"],
                                        "note": note, "superseded_version_id": superseded})
        result = {"id": version_id, "template_id": row["template_id"], "version": row["version"],
                  "state": "published" if action == "approve" else "rejected",
                  "reviewed_by": actor_id, "reviewed_at": now}
        if action == "approve":
            result["published_at"] = now
        if superseded:
            result["superseded_version_id"] = superseded
        return result

    def withdraw(self, clinic_id: str, actor_id: str, version_id: str, reason: str) -> dict[str, Any]:
        """撤回已发布或已被取代的版本。撤回不删除内容，已绑定计划继续引用原版本。"""
        reason = text(reason, "撤回原因", maximum=1000)
        now = self._now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "pathway:review", clinic_id=clinic_id)
            row = self._version(connection, clinic_id, version_id)
            if row["state"] not in {"published", "superseded"}:
                raise Conflict("只有已发布或已被取代的版本可以撤回")
            bound = connection.execute("SELECT COUNT(*) FROM plan_pathway_bindings WHERE template_version_id=?",
                                       (version_id,)).fetchone()[0]
            connection.execute("UPDATE pathway_template_versions SET state='withdrawn',withdrawn_by=?,withdrawn_at=?,withdraw_reason=?,updated_at=? WHERE id=?",
                               (actor_id, now, reason, now, version_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="pathway_template_version", aggregate_id=version_id,
                               action="pathway.version_withdrawn", occurred_at=now,
                               payload={"template_id": row["template_id"], "version": row["version"],
                                        "reason": reason, "previous_state": row["state"],
                                        "bound_plan_count": bound})
        return {"id": version_id, "template_id": row["template_id"], "version": row["version"],
                "state": "withdrawn", "withdrawn_by": actor_id, "withdrawn_at": now,
                "bound_plan_count": bound}

    def list_templates(self, clinic_id: str, actor_id: str, *, program: str | None = None) -> list[dict[str, Any]]:
        if program is not None:
            program = choice(program, "适用项目", PROGRAMS)
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinical:read", clinic_id=clinic_id)
            if program:
                rows = connection.execute("SELECT * FROM pathway_templates WHERE clinic_id=? AND program=? ORDER BY name,id",
                                          (clinic_id, program)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM pathway_templates WHERE clinic_id=? ORDER BY name,id",
                                          (clinic_id,)).fetchall()
            items = []
            for row in rows:
                published = published_version(connection, row["id"])
                open_version = connection.execute(
                    "SELECT id,version,state FROM pathway_template_versions WHERE template_id=? AND state IN ('draft','pending_review') "
                    "ORDER BY version DESC LIMIT 1", (row["id"],)).fetchone()
                items.append({"id": row["id"], "name": row["name"], "program": row["program"],
                              "created_by": row["created_by"], "created_at": row["created_at"],
                              "published_version": ({"id": published["id"], "version": published["version"],
                                                     "published_at": published["published_at"]} if published else None),
                              "open_version": dict(open_version) if open_version else None})
            return items

    def get_template(self, clinic_id: str, actor_id: str, template_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinical:read", clinic_id=clinic_id)
            row = self._template(connection, clinic_id, template_id)
            versions = connection.execute(
                "SELECT v.*,(SELECT COUNT(*) FROM plan_pathway_bindings b WHERE b.template_version_id=v.id) AS bound_plans "
                "FROM pathway_template_versions v WHERE v.template_id=? ORDER BY v.version", (template_id,)).fetchall()
            return {"id": row["id"], "clinic_id": clinic_id, "name": row["name"], "program": row["program"],
                    "created_by": row["created_by"], "created_at": row["created_at"],
                    "versions": [self._version_view(version, bound_plans=version["bound_plans"]) for version in versions]}

    def get_version(self, clinic_id: str, actor_id: str, version_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinical:read", clinic_id=clinic_id)
            row = self._version(connection, clinic_id, version_id)
            bound = connection.execute("SELECT COUNT(*) FROM plan_pathway_bindings WHERE template_version_id=?",
                                       (version_id,)).fetchone()[0]
            return self._version_view(row, bound_plans=bound)

    def migrate_plan(self, clinic_id: str, actor_id: str, plan_id: str, template_id: str, reason: str) -> dict[str, Any]:
        """临床负责人逐案批准：把在途计划迁移到模板当前生效版本。

        迁移会取消该计划尚未处置的模板生成节点（手工节点不受影响），并按
        计划原开始日期以新版本定义重新生成整套节点；已完成或已豁免的节点
        作为临床记录保留。
        """
        reason = text(reason, "迁移原因", maximum=600)
        now = self._now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "pathway:review", clinic_id=clinic_id)
            plan = connection.execute("SELECT * FROM plans WHERE id=? AND clinic_id=?", (plan_id, clinic_id)).fetchone()
            if plan is None:
                raise NotFound("诊疗计划不存在")
            if plan["state"] not in {"draft", "proposed", "active", "paused"}:
                raise Conflict("已完成或已取消的计划不能迁移路径版本")
            template = self._template(connection, clinic_id, template_id)
            if template["program"] != plan["kind"]:
                raise Conflict("路径模板适用项目与计划类型不一致")
            version = published_version(connection, template_id)
            if version is None:
                raise Conflict("模板当前没有已发布版本，不能迁移")
            binding = connection.execute("SELECT * FROM plan_pathway_bindings WHERE plan_id=?", (plan_id,)).fetchone()
            if binding and binding["template_version_id"] == version["id"]:
                raise Conflict("计划已绑定当前生效的模板版本")
            generation = binding["generation"] + 1 if binding else 0
            clinic = connection.execute("SELECT timezone FROM clinics WHERE id=?", (clinic_id,)).fetchone()
            pending = connection.execute(
                "SELECT * FROM plan_milestones WHERE plan_id=? AND state IN ('pending','deferred') AND idempotency_key LIKE ?",
                (plan_id, f"pathway:{plan_id}:%")).fetchall()
            for milestone in pending:
                connection.execute("UPDATE plan_milestones SET state='cancelled',updated_at=?,version=version+1 WHERE id=?",
                                   (now, milestone["id"]))
                sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM milestone_events WHERE milestone_id=?",
                                              (milestone["id"],)).fetchone()[0]
                connection.execute(
                    "INSERT INTO milestone_events(id,milestone_id,sequence,action,actor_id,reason,prior_due_at,next_due_at,occurred_at) "
                    "VALUES(?,?,?,'cancel',?,?,?,?,?)",
                    (new_id("mse"), milestone["id"], sequence, actor_id, f"路径模板迁移：{reason}",
                     milestone["due_at"], None, now))
            nodes = insert_generated_milestones(
                connection, plan_id=plan_id, start_date=plan["start_date"], timezone_name=clinic["timezone"],
                nodes=decode_json(version["nodes_json"]), clinical_owner=plan["clinical_owner"],
                actor_id=actor_id, now=now, generation=generation)
            if binding:
                connection.execute("UPDATE plan_pathway_bindings SET template_version_id=?,generation=?,bound_at=? WHERE plan_id=?",
                                   (version["id"], generation, now, plan_id))
            else:
                connection.execute("INSERT INTO plan_pathway_bindings(plan_id,clinic_id,template_version_id,generation,bound_at) "
                                   "VALUES(?,?,?,?,?)", (plan_id, clinic_id, version["id"], generation, now))
            migration_id = new_id("pwm")
            connection.execute(
                "INSERT INTO pathway_migrations(id,clinic_id,plan_id,from_version_id,to_version_id,reason,approved_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (migration_id, clinic_id, plan_id, binding["template_version_id"] if binding else None,
                 version["id"], reason, actor_id, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=plan["patient_id"],
                               aggregate_type="plan", aggregate_id=plan_id,
                               action="pathway.plan_migrated", occurred_at=now,
                               payload={"template_id": template_id,
                                        "from_version_id": binding["template_version_id"] if binding else None,
                                        "to_version_id": version["id"], "to_version": version["version"],
                                        "reason": reason, "generation": generation,
                                        "cancelled_node_ids": [milestone["id"] for milestone in pending],
                                        "new_node_ids": [node["id"] for node in nodes]})
        return {"plan_id": plan_id, "migration_id": migration_id,
                "from_version_id": binding["template_version_id"] if binding else None,
                "to_version_id": version["id"], "to_version": version["version"],
                "generation": generation, "approved_by": actor_id, "reason": reason,
                "cancelled_node_ids": [milestone["id"] for milestone in pending],
                "nodes": nodes, "migrated_at": now}

    def plan_pathway(self, clinic_id: str, actor_id: str, plan_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinical:read", clinic_id=clinic_id)
            plan = connection.execute("SELECT id,patient_id FROM plans WHERE id=? AND clinic_id=?",
                                      (plan_id, clinic_id)).fetchone()
            if plan is None:
                raise NotFound("诊疗计划不存在")
            binding = connection.execute(
                "SELECT b.*,v.version AS version_number,v.state AS version_state,t.id AS template_id,t.name AS template_name,t.program "
                "FROM plan_pathway_bindings b JOIN pathway_template_versions v ON v.id=b.template_version_id "
                "JOIN pathway_templates t ON t.id=v.template_id WHERE b.plan_id=?", (plan_id,)).fetchone()
            migrations = connection.execute(
                "SELECT m.*,fv.version AS from_version,tv.version AS to_version FROM pathway_migrations m "
                "LEFT JOIN pathway_template_versions fv ON fv.id=m.from_version_id "
                "JOIN pathway_template_versions tv ON tv.id=m.to_version_id "
                "WHERE m.plan_id=? ORDER BY m.created_at,m.id", (plan_id,)).fetchall()
            return {"plan_id": plan_id,
                    "binding": ({"template_id": binding["template_id"], "template_name": binding["template_name"],
                                 "program": binding["program"], "template_version_id": binding["template_version_id"],
                                 "version": binding["version_number"], "version_state": binding["version_state"],
                                 "generation": binding["generation"], "bound_at": binding["bound_at"]}
                                if binding else None),
                    "migrations": [{"id": row["id"], "from_version_id": row["from_version_id"],
                                    "from_version": row["from_version"], "to_version_id": row["to_version_id"],
                                    "to_version": row["to_version"], "reason": row["reason"],
                                    "approved_by": row["approved_by"], "created_at": row["created_at"]}
                                   for row in migrations]}
