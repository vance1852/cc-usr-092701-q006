# 服务接口

所有时间使用带时区的 ISO 8601 格式。服务持久化 UTC 时间，按诊所配置的时区解释运营日期。JSON 请求大小上限为 1 MB；无效请求返回稳定的错误码和 HTTP 状态，不向调用方透出数据库异常。

## 登录与诊所隔离

`POST /auth/token` 接受 `staff_id` 和 `password`，返回有效期不超过一天的 Bearer 凭据。除登录与健康检查外，请求必须同时提供 `Authorization: Bearer …` 和 `X-Clinic-ID`。认证失败不区分账号不存在、停用或密码错误；诊所边界之外的数据返回不存在，避免泄露另一诊所的记录。

`POST /auth/logout` 撤销当前凭据。修改员工密码会撤销该员工的全部活动凭据。初始负责人通过命令行创建；没有可直接注册负责人的 HTTP 路由。

## 患者、评估与诊疗计划

- `POST /patients` 建立诊所内患者档案；外部编号在诊所范围内唯一。
- `GET /patients/{patient_id}` 返回最小档案，不返回联系方式密文。
- `POST /patients/{patient_id}/merge` 以两个版本号和书面原因将重复档案标记为合并，并指向保留档案。
- `POST /patients/{patient_id}/assessments` 新建评估草稿；`POST /assessments/{assessment_id}/sign` 由临床岗位签署。
- `POST /patients/{patient_id}/consents` 创建更高版本的授权；`POST /consents/{consent_id}/withdraw` 撤回授权。
- `POST /patients/{patient_id}/plans` 建立计划，医美和体重管理计划必须引用当前对应授权。
- `POST /plans/{plan_id}/{propose|activate|pause|resume|complete|cancel}` 以 `expected_version` 执行带版本保护的状态转换。
- `GET /patients/{patient_id}/weight-series` 返回按观察时间排序的测量值，不生成诊断或治疗建议。

评估签署后不可覆盖。就诊病历由章节组成，签署需要主诉、评估和计划三部分；签署后的补充内容成为新版本，原始文字仍保留。

## 诊疗路径模板

诊疗路径模板供医疗负责人统一定义同类项目的评估与随访安排，只有医生或诊所负责人（`clinician`/`owner`）可以管理，全程进入审计哈希链。模板按版本发布，内容包含：

- `programs`：适用项目，取值 `aesthetic`、`weight`、`wellbeing`；
- `assessment_sections`：建计划前评估必须覆盖的章节（`measurements`、`answers`、`history`、`screening`、`risk`、`goals`）；
- `nodes`：一次性生成的计划节点，每项含唯一 `code`、节点 `kind`（与计划节点类型一致）、`title` 和相对开始日的 `offset_days` 间隔。

接口与规则：

- `POST /pathway-templates` 建立模板和首个草稿；`PATCH /pathway-versions/{version_id}` 修改草稿或被驳回版本。
- `POST /pathway-versions/{version_id}/submit` 提交审批；`POST /pathway-versions/{version_id}/review` 由**另一名**有权限的医生批准或驳回，审批结论和意见不可改。草稿作者不能审批自己的草稿，护理、协调和审计岗位无权审批。
- 批准即发布，旧已发布版本变为 `superseded`；`GET /pathway-templates?program=weight` 与 `GET /pathway-templates/{template_id}/versions` 供审核发布前核对。
- `POST /pathway-versions/{version_id}/withdraw` 撤回未被引用的已发布版本（内容保留为 `withdrawn`，不删除）；已被任何计划引用的版本拒绝撤回，历史引用始终可解析。
- `POST /patients/{patient_id}/pathway-plans` 按项目**当时生效**的模板版本建立计划，计划固定记录 `pathway_template_version_id` 并在同一事务内一次性生成全部节点；请求须带 `Idempotency-Key`，重复提交返回原计划与原节点集合（`replayed: true`），不会再加一套。
- 模板后续发布只影响之后新建的计划；在途计划（提议、生效、暂停）不变。需要跟进新版时，`POST /plans/{plan_id}/migrations` 逐计划申请，`GET /pathway-migrations?state=pending` 查看待批，`POST /pathway-migrations/{migration_id}/decide` 由临床负责人逐个 `approved`/`rejected`。批准后同代码待办节点按新间隔改期、新节点补入、被移除的待办节点取消并全部记入节点事件与计划修订历史；已完成节点保留为历史。迁移同样支持幂等键，且只能在同一模板的不同已发布版本之间进行。


## 预约、随访与计划节点

创建预约须提供 `Idempotency-Key`，有责任人的预约不能与未结束时段重叠。临时占位到期后由 `POST /appointments/{id}/book` 拒绝确认，过期占位可通过服务方法按限额释放。预约状态按占位、确认、到诊、服务、完成推进；开始服务时产生就诊记录。

随访和计划节点支持领取租约、版本校验、幂等创建、延期和完整处置历史。旧领取者不能以过期令牌提交结果；重新领取不会删除前次领取事件。

## 诊所耗材

- `POST /products` 登记耗材；`POST /products/{product_id}/lots` 按批号入库。
- `POST /stock/reserve` 依据失效日期按先到期先出分批预留，需要 `Idempotency-Key`。
- `POST /stock/{reservation_id}/consume` 记录患者使用；`release` 释放尚未使用的数量。
- `POST /stock/{lot_id}/quarantine`、`recall` 或 `release-quarantine` 记录批次处置及受影响预留。
- `GET /stock/lots` 查看可用数量；`GET /stock/{lot_id}/history` 查看批次流水。

入库、占用、释放与患者使用均进入不可变流水。存在不足时整笔预留回滚；被隔离、召回或在诊所本地日期已过期的批次不能继续使用。

## 不良事件与数据使用

护理人员可报告事件或患者安全关注项；临床岗位复核并记录处置，诊所负责人可作废就诊记录。`GET /audit/verify` 校验诊所哈希链，`GET /audit/diagnostics` 汇报需人工核对的一致性问题，不自动修改业务状态。

`POST /patients/{patient_id}/export` 只在存在有效数据导出授权时返回明确选择的章节。导出字段采用白名单，联系方式密文、凭据和内部合并字段不会导出；相同幂等请求得到相同内容摘要。`GET /reports/daily`、`appointments`、`incidents` 和 `overdue-milestones` 仅返回运营汇总或经岗位授权的工作队列。

## 主要状态

- 路径模板版本：草稿 → 待审批 → 已发布；可被驳回后修改重提；发布新版后旧版成为已取代；未被引用的已发布版本可撤回，已引用版本只能保留。状态流转和审批意见均入审计链。
- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。按模板建立的计划固定引用模板版本，迁移经逐个批准后改记新版本。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。
