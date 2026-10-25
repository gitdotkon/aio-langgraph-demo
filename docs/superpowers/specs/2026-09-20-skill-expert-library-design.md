# 专家库（Agent Skill 管理）设计文档

- 日期：2026-09-20
- 修订：2026-10-04 对照已装 SDK（deepagents、agent_sandbox）源码实测，修正 §4/§5.1/§5.2/§9 的接口事实与建目录策略，补充沙箱原生 Skills API 的取舍。
- 范围：在 aio-langgraph-demo 的 Web UI 左侧新增「工具栏 - 专家库」，管理外置 Agent Skill 库，并让启用的技能在每次 Agent 运行时生效。

## 1. 背景与目标

DeepAgents（已装 `deepagents>=0.4.0`）原生支持 Agent Skills：`create_deep_agent(skills=[源目录])`
会自动挂载 `SkillsMiddleware`，从 **Agent 的 backend**（本项目 backend = AIO 沙箱）读取技能目录，
以"渐进式披露"方式把 `name + description + 路径`注入系统提示，Agent 需要时再 `read_file` 全文。

每个技能是一个目录，含 `SKILL.md`（YAML frontmatter：`name`(须与目录同名，小写字母数字连字符)、
`description`，可选 `license/compatibility/allowed-tools/metadata`）及可选 helper 文件。

目标：给用户一个可视化的"专家库"来管理这些技能（增删改查 + 启用开关），并让其作用于 Agent 运行。

## 2. 关键决策（已确认）

1. **存储**：本地 `./skills/` 为真源（可版本管理）；每次 run 前把**启用的**技能同步到沙箱 `/home/gem/skills`。
   适配 `docker --rm` 的临时沙箱。
2. **管理范围**：列表/查看详情 + 新建/编辑/删除 + 每技能启用/禁用开关；只有启用的技能会作为本次 run 的 source。
   通过"只同步启用技能进沙箱"天然实现，无需给 SkillsMiddleware 传白名单。

## 3. 架构与数据流

```
本地 ./skills/<name>/SKILL.md  ──(run 前 sync 启用集)──▶  沙箱 /home/gem/skills/<name>/…
        ▲                                                        │
        │ CRUD/enable (REST, 操作本地盘)                          │ SkillsMiddleware via AIOSandboxBackend
   Web UI 专家库抽屉 ──▶ web_app.py /api/skills* ──▶ agent_core.create_agent(skills=[沙箱目录])
```

- 启用状态变更即时写入本地 `.state.json`；不影响正在运行的任务。
- 每次新建 run 时重新同步并按当时启用集生效；改完技能需新起一个 run 才生效（技能在 run 开始加载一次）。

## 4. 数据模型

- 技能目录：`./skills/<name>/SKILL.md`（+ 可选任意 helper 文件/子目录）。
- 启用状态：`./skills/.state.json`，形如 `{"<name>": true|false}`；**文件缺失或键缺失默认视为启用**。
  - `.state.json` 位于 skills 根、非目录且无 SKILL.md，不会被当技能；同步与列举均跳过隐藏（`.` 开头）项。
- 名称校验（与 DeepAgents 规范一致）：`^[a-z0-9]+(-[a-z0-9]+)*$`，长度 ≤ 64，且等于其目录名。
  实测 SkillsMiddleware 规范另约束 `description` ≤ 1024 字符，新建/编辑时一并校验。

## 5. 后端改动

### 5.1 agent_core.py
- 新增常量 `SKILLS_SANDBOX_DIR = "/home/gem/skills"`。
- `create_agent(backend)` 改为 `create_deep_agent(..., backend=backend, skills=[SKILLS_SANDBOX_DIR])`。
  实测（deepagents 源码）：`skills is not None` 时内部即
  `SkillsMiddleware(backend=backend, sources=skills)`，sources 为 **backend 内的目录路径**，与本文一致。
- 不改变 SYSTEM_PROMPT 现有内容（工作目录仍为 `/home/gem`）。
- **backend 无需新增方法**：SkillsMiddleware 列举/读取技能只调用 `backend.ls()` 与 `backend.download_files()`
  （及 async 版）。实测 `BaseSandbox.ls` 走 `execute()` 跑 shell `ls` 命令再解析输出（非 `ls_info`），
  `AIOSandboxBackend` 的 `execute`/`download_files` 均已实现，故中间件可直接工作。

### 5.2 skills_store.py（新增模块，本地盘读写 + 同步）
- `SKILLS_ROOT = Path(__file__).parent / "skills"`
- `list_skills() -> list[dict]`：扫描子目录，解析每个 `SKILL.md` frontmatter，返回
  `{name, description, path, enabled, has_helpers}`；按 name 排序。
- `read_skill(name) -> {name, description, content}`：返回 SKILL.md 原文。
- `create_skill(name, description, content)`：校验 name；建目录写 SKILL.md（若 content 未含 frontmatter，
  则用 name/description 组装）；默认启用。已存在则报错。
- `update_skill(name, content)`：覆盖写 SKILL.md。
- `delete_skill(name)`：删除整个技能目录。
- `set_enabled(name, enabled)`：更新 `.state.json`。
- `enabled_skills() -> list[str]`：返回启用技能名列表。
- `_safe_dir(name)`：拒绝 `..`、路径分隔符、非法名（抛 `ValueError`）。
- `sync_skills_to_sandbox(client, dest_root=SKILLS_SANDBOX_DIR)`：
  - 先确保 `dest_root` 存在。实测 SDK file 客户端**没有** `make_dir`（方法仅
    read_file/write_file/replace_in_file/search_in_file/find_files/grep_files/glob_files/
    upload_file/download_file/list_path/str_replace_editor/watch_*），故建目录用一次性 shell 会话
    `mkdir -p <dest>`（同 `/api/open` 的 create_session→exec_command→cleanup_session 模式）；
    实现时亦可先实测 `write_file` 是否自动建父目录，若能则省去该步。
  - 对每个启用技能，递归读取本地目录所有文件，`base64` 编码后经
    `client.file.write_file(file=<dest>/<name>/<rel>, content=<b64>, encoding="base64")` 上传（支持二进制 helper）。
    `encoding` 参数实测存在（`FileContentEncoding`：utf-8 / base64），与 `upload_files` 现用写法一致。
  - 幂等：已存在则覆盖。单个文件失败记录并继续（不阻断整个 run）。
- **备选同步通道（v1 不采用）**：agent_sandbox SDK 自带原生 Skills API
  （`client.skills`：`register_skills(file=…, path=…, name=…)` → `POST v1/skills/register` multipart 单文件；
  `list_metadata` 返回 `SkillMetadata{name, path(沙箱内绝对目录), frontmatter}`；另有 `get_content`/
  `delete_skill`/`clear_skills`）。不采用理由：register 为单文件上传，携带 helper 子文件需依赖未经验证的
  归档格式；技能落盘路径由服务端决定，SkillsMiddleware 的 sources 需从 `list_metadata` 反推，可控性差；
  而逐文件 `write_file` 同步递归、二进制安全、落点即约定的 `/home/gem/skills`。留作观察项。
- frontmatter 解析复用简单正则（`^---\n(.*?)\n---`+`yaml.safe_load`），与 DeepAgents 保持一致，不强依赖其私有函数。

### 5.3 web_app.py 新增端点（全部 `asyncio.to_thread` 包装本地 IO）
| 端点 | 说明 |
|------|------|
| `GET /api/skills` | 列表 |
| `GET /api/skills/{name}` | 读详情（SKILL.md 正文） |
| `POST /api/skills` | 新建：body `{name, description, content}` |
| `PUT /api/skills/{name}` | 保存：body `{content}` |
| `DELETE /api/skills/{name}` | 删除 |
| `PATCH /api/skills/{name}/enabled` | body `{enabled: bool}` 切换 |

- 错误：非法名 400；不存在 404；重名新建 409；IO 失败 500。
- 在 `_execute_run` 里 `open_backend` 成功后、`create_agent` 之前调用
  `await asyncio.to_thread(sync_skills_to_sandbox, client)`；同步异常不致命，降级为"无技能/部分技能"并记一条
  `role:"error"` 事件提示。

## 6. 前端改动（static/index.html）

- **左栏工具栏**：`.c-chat` 顶部新增 `.c-toolbar`，含按钮「🧩 专家库」（`aria-pressed`）。
- **抽屉面板** `.c-skills`（右侧滑入或遮罩模态，沿用主题变量）：
  - 头部：标题 + 关闭；"新建技能"按钮。
  - 列表项：名称、描述（截断）、启用开关（`<input type=checkbox>`）、查看/编辑、删除。
  - 编辑态：`<textarea>` 编辑 SKILL.md，保存/取消；新建态含 name/description 输入。
  - 删除/冲突给内联提示。
- 约定：BEM `c-` 前缀、`:root --c-*` 主题、单文件零依赖、内联 SVG/emoji 图标、`:focus-visible` 样式、
  `overscroll-behavior:contain`；新增滚动容器遵循既有高度链 `minmax(0,1fr)`+`min-height:0`。
- 交互：打开时 `GET /api/skills`；开关→`PATCH`；保存→`POST/PUT`；删除→`DELETE`。全部失败时 `addError` 风格提示。

## 7. 错误处理与安全

- 名称严格白名单校验 + 路径穿越防护；仅允许写 `./skills/<合法名>/` 下。
- 同步仅处理启用集，避免把禁用/草稿技能暴露给模型。
- 大 SKILL.md 由 DeepAgents 侧既有上限保护；本项目读取无特殊限制但 UI 编辑建议软限制提示。

## 8. 测试计划

- 后端（pytest + FastAPI TestClient）：
  - CRUD：新建/读取/更新/删除往返；重名 409；非法名 400；不存在 404。
  - `.state.json`：默认启用、切换后持久、缺文件容错。
  - `sync_skills_to_sandbox`：注入假 client，断言仅上传启用集、递归含 helper、base64 正确、单文件失败不阻断。
- 前端：手动验证（无外网依赖），核对样式与可达性。
- 端到端：启用某技能→发消息→日志追踪里系统提示/工具可读该 SKILL.md；禁用后新 run 不再出现。

## 9. 假设

- 沙箱工作目录可写 `/home/gem`。建目录能力已实测：SDK 无 `file.make_dir`，`write_file` 是否自动建父目录
  待实现时一次性实测；兜底为 shell `mkdir -p`（见 §5.2）。
- 运行与热重载：管理改动对"下一个新 run"生效，不追溯正在进行的 run。
- 沙箱原生 Skills API（`client.skills`）存在但 v1 不用，理由见 §5.2；若后续服务端明确归档格式与落盘规则，可重新评估。
- v1 不做：helper 文件的 UI 编辑、多 source 优先级分层、技能市场导入。
