# 交付说明：开发任务清单、质量检查要点、部署验证方式

> 本页是**当前**交付状态（2026-10-29 第二轮，接手 CodeBuddy 后）。
> 首轮历史结论见 [07-audit-and-redesign.md](07-audit-and-redesign.md)，
> 本轮变更见 [08-frontend-and-shortcut.md](08-frontend-and-shortcut.md)。

## 一、开发任务清单

### 已完成

| # | 任务 | 产出 | 状态 |
| --- | --- | --- | --- |
| 1 | 复用参考项目架构（config / scripts / app / tests / docs 分层） | 全目录结构 | 完成 |
| 2 | `config/` 唯一配置来源 | `studio-env.ps1`、`models.manifest.json`、`params.json`、`prompts.json`、`ui.json`、`workflows/*.json` | 完成 |
| 3 | 领域层：六阶段资产扫描与验收门 | `app/studio.py` | 完成 |
| 4 | 领域层：ComfyUI 工作流渲染与校验 | `app/workflows.py` | 完成 |
| 5 | 领域层：ComfyUI 客户端（提交/轮询/取图/中断） | `app/comfy.py` | 完成（第二轮） |
| 6 | 只读取证脚本（端口/语法/ASCII/磁盘/进程/GPU） | `app/checks.ps1` | 完成 |
| 7 | 审计引擎 39 项 + 评分 + 报告 | `app/audit_engine.py` → `reports/audit-latest.json`、`docs/05-audit.md` | 完成 |
| 8 | 控制台服务 + 单页界面（**四页签**） | `app/server.py`、`app/static/index.html` | 完成（第二轮重构） |
| 9 | 控制台内出图闭环（渲染→提交→进度→预览→归档） | `POST /api/generate`、`GET /api/job`、`POST /api/illust` | 完成（第二轮） |
| 10 | 产出页（ComfyUI 输出 + project/ 合并索引、灯箱） | `GET /api/gallery`、`GET /api/image` | 完成（第二轮） |
| 11 | 幂等运维脚本（初始化/体检/权重/启停/校验/状态） | `scripts/*.ps1` | 完成 |
| 12 | 桌面快捷方式 + 图标生成 | `scripts/30-create-shortcut.ps1`、`app/static/studio.ico` | 完成（第二轮） |
| 13 | 一键入口 | `start.cmd`、`audit.cmd` | 完成 |
| 14 | 离线静态检查 **98 项**（含 6 项前端一致性 + 领域层单测 41 例） | `tests/lint_test.py`、`tests/unit_test.py` | 完成，全通过 |
| 15 | 工作流校验（离线 + 对照 `/object_info`） | `tests/workflow_test.py` | 完成，3 模板全通过 |
| 16 | 阶段 0 冒烟出图 + 基准记录 | `tests/smoke_test.py` → `reports/baseline.json` | 完成，38.6s @ 1024² |
| 17 | 设定表编辑器（写入 `character.json`） | `/api/bible` + `/api/asset` + 创作页 | 完成（第二轮增量） |
| 18 | 刷新后恢复上次出图（sessionStorage + `/api/job`） | 创作页 | 完成（第二轮增量） |
| 19 | 产出页下载 / 在文件夹中显示 | `?download=1`、`action: reveal` | 完成（第二轮增量） |
| 20 | 模型页：本地大模型加载/卸载 + 显存管理 | `/api/models*`、`app/ollama.py`、模型页 | 完成（第三轮增量） |
| 21 | 审计一键修复 | `app/remediation.py`、`POST /api/fix`、体检页按钮 | 完成（第三轮增量） |
| 22 | 重复 ComfyUI 实例清理 | `scripts/12-dedupe-comfyui.ps1` | 完成（第三轮增量） |
| 23 | 流程/排错/参数/架构/前端文档 | `docs/01`–`08` | 完成 |

### 剩余待办

| # | 任务 | 现状 | 验收 |
| --- | --- | --- | --- |
| A | 阶段 1 角色候选图 40–60 张 | 0 张（唯一阻塞点） | 阶段 1 验收门 pass |
| B | 阶段 2 LoRA 训练与测试网格 | 训练集 0 张 | `selected.json` 有 checkpoint |
| C | 可选权重（LLLite 66MB / Turbo / Aesthetic 各 4GB / Wan Q8 15.4GB） | 4 项未下载 | 按需下载，`02-fetch-models.ps1` 退出码 0 |
| D | 视频生成接回 | **暂时搁置**：`config/ui.json` 的 `video.paused = true` | 改回 `false` 后创作页恢复阶段 5 模板 |

---

## 二、质量检查要点

### 1. 配置层（唯一来源）

- `config/` 是唯一事实来源，脚本与 Python 都不得内联硬编码参数。
- Python 无法 dot-source PowerShell，由 `studio.load_studio_env()` 解析 `$StudioEnv` 的字面量赋值；
  因此 hashtable 内**必须保持字面量表达式**（引号字符串或 `Join-Path $ProjectRoot 'x'`），
  覆盖逻辑放在表之后。
- `models.manifest.json` 是权重清单的唯一来源，`02-fetch-models.ps1` 与审计 `MDL-*` 都读它。
- `ui.json` 只放展示层设置（标题、视频阶段状态、刷新节奏、画廊上限），不得混入出图参数。

### 2. 代码层

- HTTP 层（`server.py`）只做参数校验与序列化，判断全在 `studio.py` / `workflows.py` / `comfy.py`，可直接单测。
- 领域函数对缺失输入返回 `None` / 空结构而不是抛异常；`storyboard_report()` 等返回值
  **key 集合恒定**，调用方无需防御性 `get`。
- 所有返回值可被 JSON 序列化（审计结果与控制台共用同一份数据结构）。
- 访问 ComfyUI 的 HTTP 代码**只允许出现在 `app/comfy.py`**；`/api/image` 是唯一的图片出口，
  同时负责路径穿越与后缀白名单校验。

### 3. 脚本层（Windows PowerShell 5.1）

- **ASCII-only**：PS 5.1 对无 BOM 的 `.ps1` 按 ANSI 解析。由 `lint_test.py` 强制校验；
  需要中文的地方（如快捷方式名）用 `[char]0x4E2D` 拼接。
- **`NN-动作.ps1` 编号**，幂等可重复执行：存在即跳过、大小校验后重下、端口已监听直接返回 0。
- **停止类脚本用 `return` 不用 `exit`**，避免终止编排它的调用方。
- 启动类脚本必须**等待就绪并返回非零**：`10-start-comfyui.ps1` 轮询 `/system_stats` 180 次。
- 停止脚本要**按端口反查监听进程再杀**，只杀 wrapper 会留下占显存的 python 子进程。
- 图标：ICO 小尺寸（≤64px）写 32bpp BMP 段，大尺寸才写 PNG 段，否则 GDI 系程序解码成噪点。

### 4. 工作流层

- 节点名**以本机 `/object_info` 为准**，模板不可能一次写对；`20-verify-workflow.ps1` 负责校验。
- 模板带 `__meta__`（标题/阶段/依赖节点包/默认值），提交 `/prompt` 前由 `strip_meta()` 剥离。
- 占位符必须有默认值；缺失即判 FAIL，不靠运行时报错发现。
- 图必须能到达输出节点（`SaveImage` / `VHS_VideoCombine`），否则跑完没有产物。
- 出图只覆盖表单里给出的参数，未给出的键回落到模板默认值——默认值只有一份。

### 5. 资产层（六阶段验收门）

- 验收门由 `evaluate_stages()` 依据 `project/` 实际文件判定，不靠人工勾选。
- 第一个未通过的强制阶段**阻塞其后所有阶段**；阶段 5 是可选阶段，当前标记暂缓。
- 训练集标签首词必须是触发词；固定特征不写进标签；`dialogue` 允许为空（无台词格）。
- 二进制（图片/视频/权重）不入库，元数据（设定表/分镜表/标签/manifest）入库。
- 归档成品时**图片落盘与 manifest 记录在同一个函数里完成**（`archive_illust`），不允许只落其一。

### 6. 前端层

- 单文件、无构建、无第三方依赖、无外部字体/图标请求；断网可用。
- 无内联事件：全部 `data-act` 委托；`lint_test.py` 校验「每个动作都有处理器」。
- 所有 `/api` 端点必须能在 `server.py` 中找到路由；`lint_test.py` 校验。
- 页签与视图一一对应（4 个）；`hidden` 属性必须真正隐藏（`[hidden]{display:none !important}`）。
- 长任务统一走底部任务条；失败必须可见（任务条 + 提示条 + toast），不允许静默失败。

### 7. 安全层

- ComfyUI 与控制台**只绑定 `127.0.0.1`**，本机服务无鉴权，禁止 `--listen 0.0.0.0`。
  审计有独立检查项 `NET-03` / `SEC-01`。
- 控制台对每个请求校验 `Host`，非回环返回 403。
- `SEC-03` 扫描 config 下 JSON 是否含密钥字面量。
- Anima 为 CircleStone Labs 非商业许可，商用前核对（`SEC-05`）。

### 自动化检查汇总

| 检查 | 命令 | 项数 | 当前 |
| --- | --- | --- | --- |
| 离线静态检查 | `python tests\lint_test.py` | 98 | 全通过 |
| 领域层单测 | `python -m unittest discover -s tests -p "unit_test.py"` | 41 例 | 全通过（离线，不碰真实目录） |
| 工作流校验 | `python tests\workflow_test.py` | 3 模板 | 全通过（在线，对照 `/object_info`） |
| 部署审计 | `python app\audit_engine.py` | 39 | 32 通过 / 7 警告 / 0 失败（71 分 C） |
| 出图验收 | `python tests\smoke_test.py` | 1 | 38.6s @ 1024²，已写入基线 |

---

## 三、部署验证方式

### 前置条件

Windows 11 / Python ≥ 3.9 / NVIDIA 驱动可用 / 无第三方 Python 依赖（全部标准库）。

### 可重复执行的部署步骤

```powershell
cd <项目根>

# 步骤 1：建目录 + 写入用户级环境变量（幂等，可反复执行）
.\scripts\00-init-project.ps1
# 预期：14 个 [ok]，ComfyUI 缺失时给出 [warn] + 修复命令

# 步骤 2：环境体检
.\scripts\01-check-env.ps1
# 判定：退出码 0 = 就绪；1 = ComfyUI 或 Python 缺失

# 步骤 3：权重到位校验
.\scripts\02-fetch-models.ps1
# 判定：退出码 0 = 3 个必需权重齐全且不小于 min_bytes

# 步骤 4：启动 ComfyUI
.\scripts\10-start-comfyui.ps1
# 判定：[ok] ComfyUI ready on http://127.0.0.1:8188；180s 内未就绪返回 1

# 步骤 5：工作流节点名校验
.\scripts\20-verify-workflow.ps1
# 判定：退出码 0，且无 [problem] 输出

# 步骤 6：阶段 0 验收（1024×1024 出图）
python .\tests\smoke_test.py
# 判定：退出码 0，reports/baseline.json 含 elapsed_s 与 vram_peak_mb

# 步骤 7：离线静态检查
python .\tests\lint_test.py
# 判定：[PASS] 98 项全部通过（内含领域层单测）

# 步骤 8：审计
.\audit.cmd
# 判定：失败 0 项；评分受阶段资产影响（当前 71/C）

# 步骤 9：控制台
.\start.cmd
# 判定：浏览器打开 http://127.0.0.1:8766，顶部胶囊显示「ComfyUI 在线」

# 步骤 10：桌面快捷方式
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\30-create-shortcut.ps1
# 判定：桌面出现「漫画工作台」图标，双击打开控制台
```

### 验证矩阵

| 层面 | 怎么验 | 通过标准 | 失败时看什么 |
| --- | --- | --- | --- |
| 脚本 | 连续执行两次 | 第二次输出与第一次一致，退出码相同 | 说明脚本不幂等 |
| 服务 | `Invoke-RestMethod http://127.0.0.1:8188/system_stats` | 返回 200 | `logs\comfyui.err.log` |
| 控制台 | `Invoke-RestMethod http://127.0.0.1:8766/api/health` | `{"ok":true,...}` | `reports\audit.log` |
| 六阶段 | 开始页「六阶段进度」 | 阶段 0 全绿后才做阶段 1 | 点开阶段看每项 detail |
| 提示词 | 创作页 → 用设定表填充 → 渲染提示词 | 正负向与设定表字段一致 | 渲染结果下方的「未填占位符」 |
| 自动同步 | 创作页 → 改任一模板字段 | 0.5s 内提示词自动更新；手改提示词后不再覆盖 | 点「跟随模板」重新同步 |
| 文本框 | 拖提示词框右下角 → 刷新页面 | 高度保持（`localStorage.studio.promptSizes`）；双击恢复默认 | 字段框超过 150px 后出现滚动条 |
| 耗时预估 | 改宽/高/步数/张数 | 按钮行显示「预计 ~Ns」；>3MP 出现显存预警 | `reports/baseline.json` 是否过期 |
| 设定表 | 创作页 → 角色设定表 → 改字段 → 保存设定表 | `character.json` 更新且保留 `created_at` | 面板内「缺 N 项」与阶段 1 检查项 |
| 恢复 | 出图后刷新页面 | 结果区自动恢复（种子/尺寸/缩略图） | `sessionStorage["studio.lastJob"]`、`/api/job` |
| 出图 | 创作页 → 开始生成（512×512 / 6 步试跑） | 结果区出现缩略图并带种子 | 面板内红条 + 任务条详情 |
| 归档 | 结果缩略图 → 保存到项目 | `project/03_illust/final` 有文件，`manifest.json` 有配方 | 阶段 3 两项检查应同时转 pass |
| 下载/定位 | 产出页 → 缩略图「下载」/「文件夹」 | 浏览器下载原图；资源管理器选中该文件 | `/api/image?...&download=1`、`action: reveal` |
| 模型 | 模型页 → 加载 9B → 卸载 | 行内出现「已加载 5.x GB」，卸载后回到「未加载」 | `GET /api/models` 的 `models[].loaded` |
| 一键修复 | 体检页 → 问题行 → 修复按钮 | 动作类会执行并自动重新审计；复制类只写剪贴板；跳转类切页签 | `POST /api/fix`、`remediation.FIXES` |
| 产出页 | 产出页 → 切来源筛选 | 缩略图可打开灯箱，翻页正常 | `/api/gallery` 返回项 |
| 工作流 | 体检页 → 校验工作流 | 3 个模板均「通过」 | `problems` 列出未注册节点 |
| 显存释放 | `scripts\stop.ps1` | 打印 `[gpu] VRAM used` 且端口 8188 释放 | 检查残留 python 进程 |
| 图标 | 资源管理器查看 lnk | 16/32/48/256 尺寸均清晰无噪点 | 用 `-Force -IconOnly` 重绘 ICO |

### 回归触发条件

以下任一变更后，重跑步骤 6 → 7 → 8：

- 驱动 / ComfyUI 版本 / 节点包版本变化
- 权重量化或文件替换
- `config/workflows/*.json` 改动
- `config/params.json`、`prompts.json`、`ui.json` 改动
- `app/static/index.html`、`app/comfy.py` 改动（前端一致性检查会在这里挡一道）

基准耗时只在 `reports/baseline.json` 里，改动前后对比才有意义。
