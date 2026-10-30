# 审计结论、修复方案与新前端设计

审计日期：2026-10-29 · 范围：本项目（ComfyUI 本地创作控制台）全部代码与运行环境

---

## 一、审计方法

| 手段 | 覆盖 |
| --- | --- |
| 分阶段计时（`Measure-Command` + 直接调用审计类） | 启动链路耗时归因 |
| 运行时状态轮询（`/api/state` 每 3s 采样 5 次） | 启动状态机是否收敛 |
| 符号引用检索（`Select-String` 全仓） | 死代码与未读配置 |
| 边缘渲染（Edge headless `--dump-dom` / `--screenshot`） | 前端真实渲染与 JS 运行时错误 |
| `tests/lint_test.py`（78 项）+ `audit_engine`（38 项） | 回归基线 |

---

## 二、审计结论

### 结论 1（阻断级）：启动"长时间未成功"是启动流程缺陷，不是 ComfyUI 慢

用户的直接感受是"双击 `start.cmd` 等很久没结果"。归因链如下：

1. `start.cmd` 固定传 `--with-comfyui`，`preflight()` 无条件调用 `action_comfyui_start()`。
2. 该函数先把状态置为 `starting`，再**后台**拉起 `10-start-comfyui.ps1`。
3. 脚本内 `Get-ComfyUiLauncher` 返回 `$null`，`Write-Error` 后立即退出 —— 进程秒退。
4. 但 Python 侧后台分支**无条件** `return True`（`run_script(..., background=True)` 只做 `Popen` 就报成功），于是对外宣告"启动中"。
5. 随后 waiter 线程轮询 `/system_stats` **240 次**。期间 UI 一直显示"正在启动 ComfyUI…"。
6. 4 分钟后才翻成 error，且提示"请查看 `logs\comfyui.err.log`"——而该文件根本没被创建（`Start-Process` 重定向从未执行）。

运行时证据：

```
t+ 3s  comfy.state=starting  msg=正在启动 ComfyUI…
t+ 6s  comfy.state=starting  msg=正在启动 ComfyUI…
t+ 9s  comfy.state=starting  msg=正在启动 ComfyUI…
t+12s  comfy.state=starting  msg=正在启动 ComfyUI…
t+15s  comfy.state=starting  msg=正在启动 ComfyUI…
--- logs\comfyui.err.log ---
（未生成）
```

第二层归因（耗时）：启动时**同步**执行完整审计。首次冷启动实测 17s（PowerShell 取证进程被 Defender 扫描），热态 2.3s。也就是说"控制台可用"被绑在"审计跑完"上，而审计与首屏毫无关系。

第三层归因（放大）：ComfyUI 与 3 个权重缺失导致 6 项 critical fail，评分恒为 0，首屏即满屏红色，把"环境未装"误读成"程序坏了"。

> 结论：ComfyUI 本身没有"启动慢"。慢的是控制台把注定失败的启动当作进行中，并在关键路径上串了一段与首屏无关的审计。

### 结论 2（结构性）：状态机不收敛

`build_state()` 只在 `online` 时改写状态，离线时沿用上一次的 `starting`。任何一次失败的启动都会让 `/api/state` 永久停在 `starting`，直到进程重启 —— 前端无从区分"正在装"、"装失败"、"没装"。

### 结论 3：冗余与死代码

| 编号 | 问题 | 证据 | 处置 |
| --- | --- | --- | --- |
| D1 | `studio.STAGES` 定义即死 | 仅 `studio.py:67` 一处引用 | 删除 |
| D2 | `load_workflow` / `list_workflows` / `template_keys` / `render_from_bible` 无调用方 | 各自只有定义行 | 删除（`workflows.py` 已提供等价能力） |
| D3 | `STUDIO_MODELS_DIR` / `STUDIO_LOG_DIR` / `STUDIO_REPORTS_DIR` 只写不读 | 仅出现在 `studio-env.ps1` | 删除，并清理用户级环境变量 |
| D4 | 空的 `models/` 目录 | 权重实际在 `ComfyUI/models` | 删除，`00-init-project.ps1` 不再创建 |
| D5 | 前端丢弃 `/api/prompts` 返回的 `params` | 拉了 112 行参数表却从不渲染 | 新前端渲染参数摘要 |
| D6 | 前端硬编码 `40–60 / 20–40 / 4–6` 阈值 | `index.html` 资产卡片 | 改为渲染后端 `check.label`（阈值只在 `studio.py`） |
| D7 | `docs/05-audit.md` 与 `reports/audit-latest.md` 内容相同 | 设计使然 | 保留，文档中说明 |

### 结论 4：旧前端（346 行内联）代码质量问题

| 编号 | 问题 | 影响 |
| --- | --- | --- |
| F1 | `S.prompts.templates.templates` 双重解引用，把 `/api/prompts` 原始结构直接塞进视图模型 | 后端字段一改即白屏 |
| F2 | `renderOverview()` 内 `S.bible=...` 赋值 | 渲染函数产生副作用，刷新顺序变化即失效 |
| F3 | 20+ 处内联 `onclick` | 无法事件委托、无法键盘操作、无法测试 |
| F4 | `loadPrompts()` 无错误分支 | 请求失败抛 `TypeError`，模板下拉为空且无提示 |
| F5 | 操作结果只写到概览页 `#act-out` | 在其他页点按钮看不到任何反馈 |
| F6 | 长任务只有 60s 固定 toast | 无进行中状态、无耗时、无法查看输出 |
| F7 | 无自动刷新 | 状态变化需手动点"刷新" |
| F8 | 无复制按钮、无分级过滤 | 提示词要手抄，38 行审计结果无法聚焦 |
| F9 | 无加载/空/错误三态，无 `aria-live` | 读屏用户感知不到状态变化 |
| F10 | `function $(id)` 兼任选择器 | 与常见 `$` 语义冲突，易误用 |

---

## 三、修复方案

### 启动路径（结论 1 + 2）

| 改动 | 位置 | 效果 |
| --- | --- | --- |
| 后台脚本不再"假成功"：`Popen` 后探测 10–20s，早退则返回失败与其输出 | `server.py: run_script` | 脚本秒退能立刻暴露真实原因 |
| 启动前校验 launcher（`main.py` / `run_nvidia_gpu.bat` / `python_embeded`） | `server.py: comfyui_launcher()` | 缺装时 0 轮询、0 后台进程 |
| 缺失时置 `state=missing` 并附可直接执行的 `hint` | `server.py: action_comfyui_start / preflight` | 用户拿到的是命令而不是等待 |
| 状态每次轮询重新推导，不再残留 | `server.py: build_state()` | 状态机收敛，`starting` 不会粘连 |
| 轮询 240s → 180s，并区分"未就绪"的排查方向 | `server.py: wait_comfy` | 失败更快、话术可执行 |
| **先绑定端口，审计移到后台线程** | `server.py: main()` | 首屏不再等审计 |

实测（同一台机器，ComfyUI 缺失场景）：

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| 首屏可交互 | 17.0s（冷启动，含同步审计） | **1.78s** |
| ComfyUI 状态 | `starting` 持续 240s 后转 error | **`missing`（立即）+ 安装命令** |
| 失败日志 | 指向不存在的 `comfyui.err.log` | 内联 `hint`，含 `setx` 命令 |
| 审计 | 阻塞首屏 | 后台完成，页面自动刷新 |

### 死代码清理（结论 3）

删除 D1–D4，`audit_engine` 的 `SEC-02` 检查项同步去掉 `models/`；`00-init-project.ps1` 不再创建该目录；已清空对应的用户级环境变量。

### 前端重写（结论 4）

见下节。

---

## 四、新前端设计与实现要点

### 设计目标 → 取舍

| 目标 | 做法 | 放弃的做法 |
| --- | --- | --- |
| 降低门槛 | 首屏只回答三个问题：能不能用 / 卡在哪 / 下一步做什么 | 并列展示全部 38 项检查 |
| 简化流程 | 6 个页签 → **3 个**：开始 / 创作 / 体检 | 概览·模型·工具·审计·日志五件套 |
| 突出核心 | 一个主行动按钮（随状态变化），其余为次要样式 | 一排等权重按钮 |
| 优化交互 | 全局任务条 + 自动刷新 + 一键复制 + 分级过滤 | 每页各自的刷新按钮 |
| 视觉 | 单一强调色、左侧色条表达状态、去边框噪声 | 满屏卡片与高饱和色块 |

### 信息架构

```
开始 ── 状态横幅（色条 + 一句人话 + 主按钮 + 可复制的修复命令）
     ├─ 4 个指标：ComfyUI / 显存 / 权重 / 阶段
     ├─ 下一步：按优先级生成 0–6 条待办，每条带针对性按钮
     └─ 六阶段进度：可点击展开逐项检查

创作 ── 左：模板选择（按阶段分组）+ 字段（中文字段名，可用设定表一键填充）
     └─ 右：正向/负向提示词 + 复制 + 参数摘要（步数/CFG/采样器）

体检 ── 健康度（分数条 + 仅看问题/全部/已通过 + 逐条修复建议）
     ├─ 工作流校验（离线结构 / 在线节点名）
     └─ 运行日志（按需加载，自动滚到底）
```

### 交互要点

- **下一步是算出来的**，不是写死的：缺 ComfyUI → 给安装命令；缺权重 → 列清单；无基准 → 出图验收；阶段卡住 → 跳到该阶段第一个未通过项。
- **全局任务条**：所有异步操作（启动/停止/出图验收/审计/校验）复用同一个底部条，三态 + 实时耗时 + 可展开查看原始输出，任何页签都可见。
- **自动刷新**：5s 轮询，`document.hidden` 时暂停，任务进行中不抢刷新；并用渲染签名比对避免无意义 DOM 重绘（防止焦点/悬停丢失）。
- **深链**：`#studio` / `#health` 可直接打开，支持浏览器前进后退。
- **不静默失败**：`api()` 统一抛错 → 任务条红态 + 消息；`/api/report` 404 视为"未检查"而非错误。

### 视觉要点

- 深色单一强调色（`#5b8cff`），状态只用三色（绿/琥珀/红），不做装饰性上色。
- 状态用**左侧 4px 色条 + 文字**双重编码，不依赖颜色单独传达信息。
- 审计行用 `✔ / ! / ✘` 三档图标区分通过/警告/失败 —— 修复了旧版"警告也画成红叉"的语义错误。
- 等宽字体只用于命令、日志、提示词；正文保持系统 UI 字体。

### 可访问性与兼容性

- 全部交互是 `<button>` + `data-act` 委托，可 Tab 聚焦、可回车触发；`:focus-visible` 有明确轮廓。
- 页签用 `role="tablist"` / `aria-selected` / `aria-controls`；任务条 `role="status" aria-live="polite"`。
- `prefers-reduced-motion` 下关闭脉冲动画。
- 无第三方依赖、无构建步骤，单文件 `app/static/index.html`。

### 后端兼容性

新前端**只调用原有的 9 个端点，未新增、未改动任何契约**：

| 端点 | 用途 |
| --- | --- |
| `GET /api/state` | 状态、指标、阶段、权重、审计摘要（新增字段 `comfyui.launcher` / `comfyui.hint` / `auditing`，均为**追加**） |
| `GET /api/prompts` | 模板与参数（本次才真正被渲染） |
| `GET /api/workflows` | 工作流校验 |
| `GET /api/report` | 审计报告 |
| `GET /api/logs?tail=N` | 日志 |
| `POST /api/prompt` | 渲染提示词 |
| `POST /api/action` | 服务与测试操作 |
| `POST /api/audit` | 运行审计 |
| `GET /report.md` | 导出报告 |

后端唯一行为变更是**启动路径与状态机修复**（结论 1/2），属于缺陷修复；响应字段只增不改，旧前端若回滚仍可工作。

---

## 五、验证记录

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 静态检查 | `python tests/lint_test.py` | 78 项全部通过 |
| 部署审计 | `python app/audit_engine.py --no-write` | 38 项：通过 22 / 警告 9 / 失败 7（ComfyUI 与权重缺失，符合预期） |
| 工作流校验 | `python tests/workflow_test.py` | 3 个模板通过（离线） |
| 首屏可用 | 计时至 `/api/health` 200 | 1.78s |
| 状态收敛 | 每 3s 采样 `/api/state` | `missing` 稳定，不残留 `starting` |
| 页面渲染 | Edge headless `--dump-dom` | 无未捕获 JS 错误；横幅/指标/待办/阶段均正确 |
| 三页截图 | Edge headless `--screenshot` | `#home` `#studio` `#health` 均正常 |

未覆盖：ComfyUI 在线路径（`online` 分支、出图验收、`/object_info` 节点校验）需在装好 ComfyUI 与权重后回归，步骤见 `docs/06-delivery-checklist.md`。
