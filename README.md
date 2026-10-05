# 多机构执业冲突

本项目维护多机构执业冲突的领域约定、角色边界与样例数据，并提供 Python 后端服务：登记执业关系生效区间、服务计划、移动缓冲与实际记录，提交时检测时空冲突并创建待核案件，处置全程留痕，历史记录不物理删除。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/practice_conflict/`：后端服务（FastAPI + SQLite）。
  - `models.py`：角色、案件状态机、冲突类型、时间区间工具。
  - `store.py`：SQLite 仓储，只追加/状态翻转，无物理删除。
  - `service.py`：冲突检测、案件处置、人员时间线、机构明细隔离。
  - `api.py`：HTTP 接口与请求头身份解析。
- `tools/check_contract.py`：契约命令行摘要检查。
- `tools/run_server.py`：启动后端服务。
- `tests/`：契约回归测试与后端端到端测试。

## 领域约束实现

- **执业关系时态**：执业关系带生效区间（`effective_from`/`effective_to`），服务计划与实际记录必须落在有效区间内，否则拒绝或生成 `outside_registration` 案件。
- **时空冲突检测**：提交实际记录时，与同一执业人员在他机构的有效记录两两比对——时段重叠记 `overlap`；间隔小于机构间移动缓冲记 `buffer_violation`。命中即创建待核案件（状态 `待核验`），证据关联双方记录。
- **待核案件流程**：`待核验 → 处置中 → 已决定 → 已归档`；`已决定`/`已归档` 可经 `reopen` 复开回 `处置中`。处置事件包括纠正身份（`identity_correction`，生成新版本记录并重新检测）、机构撤报（`institution_withdrawal`，记录翻转为 `withdrawn`）、合理重叠说明（`overlap_justification`）。所有事件追加留痕并保留来源（`source`），记录版本通过 `supersedes` 链保留，任何数据不物理删除。
- **机构明细隔离**：机构合规员仅能查看/操作本机构数据；时间线与案件证据中他机构记录脱敏为时间窗占位，事件负载仅监管人员与复核专家可见。

## 身份与角色

请求头声明调用者（演示用）：`X-Actor-Id`、`X-Actor-Role`（`regulator` 监管人员 / `reviewer` 复核专家 / `compliance` 机构合规员 / `practitioner` 执业人员）、`X-Institution-Id`（compliance 必填）。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/institutions`、`/practitioners`、`/movement-buffers` | 基础登记（监管） |
| POST | `/registrations`、`/registrations/{id}/revoke` | 执业关系生效区间与撤销 |
| POST | `/plans`、`/plans/{id}/cancel` | 服务计划与取消 |
| POST | `/records` | 提交实际记录，同步冲突检测并建案 |
| GET | `/records/{id}`、`/records/{id}/versions` | 记录详情与历史版本链 |
| GET | `/cases`、`/cases/{id}` | 案件列表/详情（按角色过滤与脱敏） |
| POST | `/cases/{id}/events` | 处置事件（纠正身份/撤报/合理重叠说明/复开/归档） |
| GET | `/practitioners/{id}/timeline` | 人员时间线：执业关系、计划、记录、冲突证据 |

## 验证

测试命令：`python3 -m pytest tests/ -q`（或 `python3 -m unittest discover -s tests -v` 运行契约回归）

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

启动服务：`python3 tools/run_server.py`（环境变量 `PC_DB_PATH` 指定 SQLite 路径，`PORT` 指定端口）
