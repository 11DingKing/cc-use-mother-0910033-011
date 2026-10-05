# 多机构执业冲突

本项目维护多机构执业冲突的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖机构合规员、执业人员、监管人员、复核专家，并明确执业关系时态、时空冲突检测、待核案件流程、机构明细隔离等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/practice_conflict/`：后端服务（模型、冲突检测、应用服务、HTTP API）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：开发用 API 服务启动入口。
- `tests/`：契约完整性回归测试与后端功能测试。

## 后端设计

纯标准库实现，与契约不变量一一对应：

- **执业关系时态**：`PracticeRelation` 登记生效区间（可长期有效），计划/记录提交时校验覆盖，缺口生成 `out_of_relation` 案件；撤销只标记不删除。
- **时空冲突检测**：提交计划或实际记录时，与同一执业人员的既有有效承诺逐一比对——时间重叠立案 `overlap`，跨站点间隔小于移动缓冲（`MovementBuffer`，未登记站点对用默认缓冲）立案 `insufficient_travel`；同机构重叠标记 `same_institution` 便于识别重复报送。同机构计划被实际记录履约（`fulfilled`）后不参与检测，避免误报。
- **待核案件流程**：`待核验 → 处置中 → 已决定 → 已归档`。机构撤报（`withdrawn`）、纠正身份（原记录 `superseded`、新记录 `corrected_from` 串联）、合理重叠说明均留痕；已决定/已归档案件可复开，既往说明与决定全部保留。历史记录永不物理删除，全部变更写入审计日志。
- **机构明细隔离**：监管人员/复核专家见全部；机构合规员仅见本机构明细，跨机构案件证据中他方机构标识与明细屏蔽为「（他机构）」；执业人员仅见本人数据。

### API 概览

认证：请求头 `X-Actor-Token`（或 `Authorization: Bearer`），令牌到角色的映射在启动时注入。

| 方法与路径 | 说明 |
| --- | --- |
| `POST /relations`、`POST /relations/{id}/revoke`、`GET /relations` | 执业关系登记/撤销/查询 |
| `POST /buffers`、`GET /buffers` | 移动缓冲登记（仅监管）/查询 |
| `POST /plans`、`POST /plans/{id}/cancel`、`GET /plans` | 服务计划登记/取消/查询 |
| `POST /records`、`GET /records`、`GET /records/{id}` | 实际记录提交（提交时检测并返回立案结果）/查询 |
| `POST /records/{id}/withdraw` | 机构撤报（软删除） |
| `POST /records/{id}/correct-identity` | 纠正身份（原记录保留，新身份重新检测） |
| `GET /cases`、`GET /cases/{id}` | 案件查询（按角色裁剪证据） |
| `POST /cases/{id}/explanations` | 合理重叠说明 |
| `POST /cases/{id}/decide` | 决定（确认违规/合理重叠/身份纠正/机构撤报/不构成冲突） |
| `POST /cases/{id}/archive`、`POST /cases/{id}/reopen` | 归档/复开（复开保留来源） |
| `GET /persons/{id}/timeline` | 人员时间线：关系、计划、记录与冲突证据 |
| `GET /audit` | 审计日志（仅监管） |

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

启动服务：`python3 tools/run_server.py --port 8000 --state var/state.json`（内置演示令牌见文件头部）
