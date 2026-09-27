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

路径模板把"适用项目、所需评估章节、计划节点类型和时间间隔"固化为可审核的定义，避免各诊所手工设置不一致。模板按版本管理，版本状态为：草稿 → 待复核 → 已发布；驳回为独立终态，发布新版本时当前生效版本变为已取代，已发布或已取代的版本可撤回。版本离开草稿后内容不可改写，系统不提供删除模板内容的入口，撤回只改变状态，已被计划引用的内容始终保留。

- `POST /pathway-templates` 以名称、适用项目（aesthetic/weight/wellbeing）、所需评估章节（使用就诊病历章节词汇）和节点定义（类型、名称、间隔天数、可选到点时间与是否指派给临床负责人）建立模板并起草第 1 版；诊所内名称唯一。
- `POST /pathway-templates/{template_id}/versions` 起草下一版；同一模板同一时间只允许一个草稿或待审版本。`POST /pathway-template-versions/{version_id}/edit` 只能修改草稿。
- `POST /pathway-template-versions/{version_id}/submit` 提交复核；`POST /pathway-template-versions/{version_id}/review` 由具备复核权限的另一名医生批准或驳回（驳回须填写意见）。复核人不得复核自己起草的版本；批准即发布并原子取代当前生效版本，任一时刻每个模板至多一个生效版本。
- `POST /pathway-template-versions/{version_id}/withdraw` 撤回已发布或已取代的版本，须填写原因；撤回后该版本不能再用于新计划，已绑定计划与其节点不受影响。
- `GET /pathway-templates`、`GET /pathway-templates/{template_id}`、`GET /pathway-template-versions/{version_id}` 查看模板、版本内容、复核记录与当前引用计划的计数。
- `POST /pathway-templates/{template_id}/plans` 按当时生效的版本建立计划，须提供 `Idempotency-Key`。计划在单个事务中固定引用该版本并一次性生成全部节点，节点时间按计划开始日期加间隔天数、以诊所时区（默认 09:00）换算。相同幂等键与相同内容的重复提交返回首次的计划与原节点集合，不会追加第二套；幂等键被不同内容使用时返回冲突。
- `POST /plans/{plan_id}/pathway-migration` 由临床负责人逐案批准，把在途计划迁移到模板当前生效版本：尚未处置的模板生成节点被取消并保留处置历史，手工节点与已完成节点不受影响，随后按新版本定义以计划原开始日期重新生成整套节点。`GET /plans/{plan_id}/pathway` 返回计划当前绑定与迁移记录。

模板修改只影响之后建立的计划；除逐案批准的迁移外，在途计划始终沿用建计划时绑定的版本内容。


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

- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。
- 路径模板版本：草稿 → 待复核 → 已发布；驳回为终态，新版发布时旧版变为已取代，已发布或已取代可撤回。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。
