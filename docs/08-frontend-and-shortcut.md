# 前端完善与桌面快捷方式

本轮接手后的变更范围：**前端补齐出图闭环、新增产出页、桌面快捷方式**。
视频生成按计划**暂时搁置**（见第 6 节），阶段 5 的定义与工作流模板原样保留。

---

## 1. 从「三个页签」到「四个页签」

旧前端三页签回答「能不能用 / 怎么填提示词 / 哪里坏了」，但**出图必须切到 ComfyUI 网页**，
产出也无处可看。本轮补上的是这条断链：

```
创作页  渲染提示词 ──▶ 提交 /api/generate ──▶ 轮询 /api/job ──▶ 预览缩略图
                                                        │
                                                   保存到项目 ──▶ 阶段 3 验收门
产出页  ComfyUI 输出 + project/ 全量索引 ──▶ 灯箱 / 归档
```

| 页签 | 回答的问题 | 本轮变化 |
| --- | --- | --- |
| 开始 | 能不能用 / 卡在哪 / 下一步做什么 | 状态胶囊进顶栏；阶段 5 标注「暂缓」；待办新增「出第一张插画」 |
| 创作 | 填什么、改什么、出几张大图 | **新增出图面板**：尺寸预设、步数/CFG/种子/张数、进度条、结果网格、同种子再生成、保存到项目 |
| 产出 | 生成了什么、在哪里 | **新页签**：ComfyUI 输出与 project/ 合并索引、按来源筛选、灯箱、归档 |
| 体检 | 哪里不达标、怎么修 | 评分环、搜索框、日志过滤与「只看警告/错误」 |

键盘：`1`–`4` 切页签、`R` 刷新、`Esc` 关灯箱、灯箱内 `←/→` 翻页。
深链：`#home` `#studio` `#gallery` `#health`，主题可钉：`?theme=dark|light`。

---

## 2. 出图闭环

### 2.1 一次出图的完整链路

```
① POST /api/prompt      {key, values}            → 渲染后的正/负向文本（可手改）
② POST /api/generate    {template, values, options, prompt}
③ GET  /api/job?prompt_id=...                    → queued / running / done / error
④ GET  /api/image?source=comfy&filename=...      → 图片字节（后端代理）
⑤ POST /api/illust      {source, meta}           → 写入 project/03_illust/final + manifest
```

关键取舍：

- **提示词以文本框为准。** 渲染只是「填表」，出图提交的是文本框当前内容，
  所见即所出（`comfy.prepare_generation(prompt=...)`）。
- **参数只覆盖表单里给了的字段。** 未填写的键不进入工作流，模板 `__meta__.defaults`
  继续生效，`config/` 仍是唯一默认值来源。
- **越界值在领域层夹紧。** 宽高 256–2048（按 8 对齐）、步数 1–120、CFG 1–20、张数 1–4，
  前端只做输入提示，真正生效的钳制在 `app/comfy.py::normalize_options`。
- **轮询不猜进度。** ComfyUI 的 HTTP 面没有逐步步数，只有队列与历史，
  因此状态机是 `queued → running → done|error`，耗时取 `execution_start/success` 时间戳。

### 2.2 归档与验收门联动

「保存到项目」会把图片字节落到 `project/03_illust/final/`，并把种子、步数、CFG、
采样器、尺寸、模板、prompt_id 追加到 `project/03_illust/manifest.json`。
阶段 3 的两项检查（终稿已产出 / 配方已记录）因此**同一次动作同时满足**，
避免「有图无配方」的半通过状态。

命名冲突自动加 `-2`、`-3` 后缀；只接受 `config` 中声明的图片后缀。
实测归档一次后阶段 3 立即由 `pending` 变为 `pass`（验证后已回滚测试产物）。

---

## 3. 新增后端契约

全部为**追加**，不改动既有端点；响应字段只增不减。

| 端点 | 用途 | 关键字段 |
| --- | --- | --- |
| `GET /api/gallery` | 产出索引 | `items[]`（`source/name/rel/size/mtime/kind/url`）、`counts`、`scanned[]`、`total` |
| `GET /api/models` | 本地大模型与显存 | `ollama{online,host,models[],vram_bytes}`、`gpu`、`comfyui` |
| `POST /api/models/load` | 加载一个模型 | `{"name": "..."}`，空 prompt + `keep_alive` 预热 |
| `POST /api/models/unload` | 卸载一个模型 | `{"name": "..."}`，`keep_alive: 0` |
| `POST /api/models/unload_all` | 卸载全部常驻模型 | 返回卸载个数 |
| `POST /api/fix` | 一键修复 | `{"id": "NET-01"}`，仅 `kind=action` 的项 |
| `GET /api/bible` | 设定表编辑载荷 | `fields`、`labels`、`palette`、`required`、`missing`、`complete` |
| `GET /api/image` | 图片代理 | `source=comfy&filename&subfolder&type` 或 `source=project&rel`；`download=1` 走附件下载 |
| `GET /api/job` | 任务状态 | `state`、`outputs[]`、`position`、`elapsed_s`、`message` |
| `POST /api/generate` | 提交出图 | 入：`template/values/options/prompt`；出：`prompt_id/options/prompt/queue` |
| `POST /api/illust` | 归档成品 | 入：`source{filename,subfolder,type}` + `meta`；出：`entry`、`url` |
| `POST /api/action` | 新增动作 | `open_folder{which}`、`comfyui_interrupt` |
| `GET /studio.ico` | 站点图标 | 与桌面快捷方式同一份 ICO |

`GET /api/state` 追加两个字段：`ui`（来自 `config/ui.json`）与 `queue`（运行/排队数）。
`POST /api/generate` 的 `prompt` 字段可选；不传时按模板与字段渲染。

安全边界：

- `/api/image?source=project` 只解析 `project/` 内路径（`resolve().relative_to()` 校验），
  路径穿越与不支持的扩展名一律 404。
- `/api/image?source=comfy` 拒绝含路径分隔符的 `filename`，一律经 ComfyUI `/view` 取回。
- 仍未引入任何鉴权：控制台只监听 `127.0.0.1`，Host 头非回环直接 403。

---

## 4. 前端实现要点

单文件 `app/static/index.html`，无构建、无第三方依赖、无外部字体/图标请求（离线可用）。

| 主题 | 做法 | 放弃的做法 |
| --- | --- | --- |
| 状态一致性 | 每 5s 轮询 `/api/state`；`document.hidden` 暂停；任务进行中不抢刷新 | 手动刷新按钮作为唯一入口 |
| 重绘代价 | 首页按数据签名比对后再写 DOM，避免轮询打断悬停/焦点 | 每次轮询重建整页 |
| 长任务 | 底部任务条统一承载：三态 + 实时耗时 + 可展开原始输出；出图另有一条进度条 | 一次性 toast |
| 失败可见 | `api()` 统一抛错 → 任务条红态 + 提示条 + toast；`/api/report` 404 视为「未检查」 | 静默失败、白屏 |
| 可访问性 | `role=tablist/aria-selected`、`aria-live` 任务与提示、`:focus-visible`、`prefers-reduced-motion` | 纯 div 点击区 |
| 事件 | 全部 `data-act` 委托；`tests/lint_test.py` 校验「每个动作都有处理器」 | 内联 `onclick` |
| 主题 | `data-theme` + CSS 变量，暗色默认，跟随系统，可手动切换并记忆 | 两套 CSS 文件 |
| 图片 | `loading="lazy"`、失败降级为「图片已失效」占位、灯箱左右翻页 | 一次性全量加载 |

脚本自检：`window.onerror` / `unhandledrejection` 会把错误显示成顶部红色横幅，
不用开控制台也能看出页面脚本挂了。

---

## 5. 视觉规范

- 强调色单一（`#5b8cff` → `#8b5cf6` 渐变），语义色仅三档：绿 / 琥珀 / 红；「暂缓」用蓝灰。
- 状态**双重编码**：颜色 + 文字/图标（`✔ ! ✘`、`通过/警告/失败`），不靠颜色单独传达。
- 等宽字体只用于命令、日志、提示词、文件名；正文系统 UI 字体。
- 卡片去边框噪声：1px 半透明描边 + 大圆角 + 单层阴影；面板背景半透明叠在渐变底上。

---

## 6. 视频生成：暂时搁置

`config/ui.json` 是这轮唯一新增的展示层配置：

```json
"video": { "paused": true, "stage": 5, "note": "按当前排期暂时搁置视频生成：…" }
```

生效方式（无删除、无隐藏真相）：

| 位置 | 表现 |
| --- | --- |
| 开始页 | 阶段 5 卡片标「暂缓」，下方一条说明条；顶部标签「视频生成：暂缓」 |
| 创作页 | 阶段 5 模板仍在列表里但 `disabled`，标题加「（暂缓）」 |
| 阶段门 | `optional: true` 保持原样，不参与阻塞判断（本来就是可选阶段） |
| 后端 | `wan22-i2v.json`、`params.wan_i2v`、阶段 5 检查项一律不动 |

恢复视频生成只需要把 `paused` 改成 `false`（并可改 `stage`），前端即时生效。

---

## 7. 桌面快捷方式

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\30-create-shortcut.ps1
```

- 生成 `app/static/studio.ico`（16/24/32/48/64/128/256 七帧）与 `studio-256.png` 预览；
- 在当前用户桌面创建 `漫画工作台.lnk` → `start.cmd`，工作目录为项目根，图标指向该 ICO；
- 幂等：已存在则跳过，`-Force` 重绘，`-IconOnly` 只出图标。

实现细节（都是踩过的坑）：

1. **图标与网页同源。** ICO 的几何轮廓就是页面内联 SVG 的同一段三次贝塞尔，
   标签页 favicon、桌面图标、预览图三者一致。
2. **小尺寸必须用 BMP 段。** 只写 PNG 帧时 Explorer 能显示，但 GDI/GDI+ 系程序
   （部分第三方文件管理器、旧式对话框）会解码成彩色噪点；因此 ≤64px 写 32bpp DIB
   （含 1bpp AND 掩码，全零由 alpha 决定透明），128/256 才写 PNG。
3. **源码 ASCII-only。** `30-create-shortcut.ps1` 里没有中文字面量：快捷方式名称由
   `[char]0x6F2B,0x753B,0x5DE5,0x4F5C,0x53F0` 拼出（漫画工作台）。Windows
   PowerShell 5.1 把无 BOM 的 `.ps1` 当 ANSI 解析，中文字面量会乱码。

实测属性：

| 项 | 值 |
| --- | --- |
| 快捷方式 | `C:\Users\Admin\Desktop\漫画工作台.lnk` |
| 目标 | `…\20261029191544\start.cmd` |
| 工作目录 | `…\20261029191544` |
| 图标 | `…\app\static\studio.ico,0`（62,295 字节，7 帧） |

---

## 8. 验证记录

| 项目 | 命令 / 手段 | 结果 |
| --- | --- | --- |
| 静态检查 | `python tests\lint_test.py` | **95 项全部通过**（含 6 项前端一致性检查 + 32 例领域层单测） |
| 领域层单测 | `python -m unittest discover -s tests -p "unit_test.py"` | 32 例通过（离线、补丁化目录） |
| 工作流校验 | `python tests\workflow_test.py` | 3 个模板通过 |
| 出图端到端 | `/api/prompt` → `/api/generate` → `/api/job` → `/api/image` | 512×512 / 8 步，**19.6s 出图**，图片字节 117,599 |
| 归档闭环 | `POST /api/illust` | 写入 `03_illust/final` 并新增 manifest 条目，阶段 3 转 `pass`（验证后已删除测试产物） |
| 页面渲染 | Edge headless `--screenshot` × 四页签（暗色）+ 亮色 | 无未捕获脚本错误，页签互斥显示 |
| 路径穿越 | `/api/image?source=project&rel=../../config/prompts.json` | 404 |
| 快捷方式 | 脚本执行 + WScript.Shell 回读 | 目标/工作目录/图标正确 |

未覆盖：亮色主题下灯箱与长列表的对比度只做了肉眼检查；灯箱键盘翻页未做自动化。

---

## 9. 已知取舍与下一步

1. **未做多任务并行。** 前端一次只跟踪一个 `prompt_id`；ComfyUI 队列里若有别处提交的任务，
   只体现为顶栏「队列 N」。多任务需要后端任务表，留到有需求时再做。
2. **未做批次矩阵。** 「同种子再生成」只改种子；批量网格（种子 × 强度）需要
   LoRA 阶段的工作流，属于阶段 2 的活。
3. **产出页无删除/移动。** 归到 `project/` 的东西按「创作资产」对待，删除交给资源管理器。
4. **出图默认走 `anima-txt2img.json`。** 其它工作流（LoRA 测试、视频）没有通用的
   「提示词 → 单图」形态，因此 `generation` 只接文生图；扩到图生图时需要先有
   `__meta__.defaults.positive` 这类约定。

---

## 10. 增量二：设定表、会话恢复、产出操作

同一天的第二批改动，仍然是前端主导，后端只增不改。

### 10.1 角色设定表编辑器（创作页）

之前 `POST /api/asset` 一直存在但前端从未调用：设定表只能手改 JSON。
现在创作页左侧有可折叠的「角色设定表」，7 个字段 + 三色调色板，保存后
`project/00_bible/character.json` 更新、阶段 1 的「设定表字段齐全」随之刷新。

- `GET /api/bible` 返回字段、中文标签、必填项、缺失项、完整度；
- 保存复用 `POST /api/asset {kind:"bible"}`，`studio.save_bible()` 改为**合并语义**：
  只写文档声明的字段，文件里手写的其它键（如备注）原样保留，`created_at` 不丢；
- 写入改为临时文件 + `os.replace` 原子替换，进程中途退出不会留下截断的 JSON。

### 10.2 刷新不丢结果

`sessionStorage["studio.lastJob"]` 记录本次提交的 `prompt_id`、种子、参数、提示词与模板。
刷新后 `boot()` 会：回填表单 → 查 `/api/job` → 仍在跑就继续轮询，已完成就直接渲染缩略图，
并显示「已恢复上次结果 · 种子 N · 渲染 Xs」。任务条与「清除结果」按钮同步出现。
选 sessionStorage 而不是 localStorage：关掉标签页即清，符合「临时任务」的语义。

### 10.3 产出页：下载与定位

- 缩略图悬浮层新增「下载」「文件夹」；灯箱标题栏同样有这两个动作。
- 下载走 `/api/image?...&download=1`，服务端补 `Content-Disposition: attachment`，
  文件名白名单化（`[A-Za-z0-9._-]`），不会出现头注入。
- 「文件夹」调用 `POST /api/action {action:"reveal"}`，后端用
  `resolve_project_media` / `resolve_comfy_media` 校验路径后
  `explorer /select,<path>`，前端拿不到任意路径执行权。

### 10.4 顶栏队列胶囊

`/api/state` 的 `queue` 有运行时，顶栏出现可点击的「渲染中 1 · 排队 2」胶囊，
点它跳到创作页出图面板。ComfyUI 空闲时胶囊不占位置。

### 10.5 后端硬化（27B 审查中核对成立的几条）

| 问题 | 修法 |
| --- | --- |
| `/api/audit` 的 `locked()` 检查与执行之间是 TOCTOU，两个并发请求会排队重复审计 | `run_audit` 改非阻塞 `acquire`，占用中抛 `AuditBusy` → 409 |
| `int(Content-Length)` 遇到非法头抛 `ValueError`，连接直接断 | 捕获后返回 400 |
| 设定表/分镜/训练选择/基准/manifest 全是 `write_text` 直写 | 统一 `_write_bytes_atomic` + `_write_json_atomic` |
| `comfy_root()` 未配置时 `Path("")` 是相对路径，画廊可能扫描进程工作目录 | `source_dir`/`gallery_snapshot` 要求绝对路径 |
| 画廊扫描用 `rglob`，符号链接目录可能成环 | 改用 `os.walk(followlinks=False)`，`count_media` 同步 |
| `view_bytes` 只依赖 HTTP 层的文件名检查 | 函数内二次校验，且非法文件名返回 400 而非 502 |
| 轮询首个探测是 `setTimeout`，`stopPolling` 清不到它 | 句柄存 `S.gen.delay`，`stopPolling` 同时清理 timeout 与 interval |

补充（同日，由「双击快捷方式多出一个控制台」牵出）：

| 问题 | 修法 |
| --- | --- |
| `start.cmd` 在已有控制台时又起一个实例，端口顺延，两个控制台同时轮询 ComfyUI | 启动前探测 `8766..+8` 的 `/api/health`，命中即打印「复用已有实例」并只打开浏览器；需要另起用 `--force-port` |
| `checks.ps1` 用 `comfy` 匹配进程，把 `--with-comfyui` 开关误判成 ComfyUI 实例（SVC-04 假警告） | 改为只认 `main.py` 或 `ComfyUI\` 路径 |

`tests/unit_test.py`（32 例）覆盖了这些函数的边界：参数夹紧与 8 对齐、随机种子、
提示词覆盖、路径穿越、`media_url` 编码、画廊合并、归档命名冲突、manifest 追加、
设定表合并、`job_status` 的四种状态映射。全部离线，用 `patched()` 把模块路径指向
临时目录，不会碰真实 `project/`。

### 10.6 本地小模型怎么参与

调用的是本机 Ollama（`qwen3.5-9b-256k`、`qwen3.8-27b-128k`），任务通过
Python 直连 `/api/chat`（PowerShell 发中文会变成问号，不可用）。

| 阶段 | 结论 |
| --- | --- |
| 9B 首轮（3 任务，25s） | 多数是错觉：称 `stopPolling` 未调用、`galleryLoading` 无并发保护、筛选不重绘——代码里都有。仅 2 条可采纳 |
| 27B 复跑（3 任务，53s，要求每条引用代码片段） | 有 5 条经核对成立（即 10.5 前 5 行），其余误报（如「下载文件名头注入」——其实已白名单化） |
| 采用原则 | 只采纳能回读代码验证的条目；模型输出不直接落盘，一律先核对再改 |

### 10.7 本批验证

| 项目 | 结果 |
| --- | --- |
| 静态检查 | 95 项全部通过 |
| 单元测试 | 32 例通过 |
| UI 端到端（CDP） | 设定表 7 字段加载 → 保存后 `character.json` 更新且 `created_at` 保留 → 出图 3.3s → **刷新后自动恢复** → 产出页 2 张 → 灯箱两个动作齐全；页面零脚本错误 |
| 下载 | `GET /api/image?...&download=1` → 200 + `attachment; filename="txt2img_00001_.png"` |
| 非法文件名 | `filename=../../config/prompts.json` → 400（此前 502） |

---

## 11. 增量三：文本框尺寸与出图逻辑

### 11.1 文本框的尺寸契约

之前所有输入都是"一行高"的 `<input>`：长标签被横向截断，提示词框固定 6 行、
超出后只能在小框里滚动。现在按用途分三类：

| 类型 | 默认高度 | 拉伸 | 溢出 | 记忆 |
| --- | --- | --- | --- | --- |
| 正向提示词 `#pos` | 190px | 拖右下角 | 超过半屏后滚动 | localStorage `studio.promptSizes` |
| 负向提示词 `#neg` | 96px | 拖右下角 | 同上 | 同上 |
| 模板/设定表字段 | 34px 起 | 自动增高（不手拖） | 超过 150px 后滚动 | 内容决定，无需记忆 |

实现要点：

- **原生拖拽优先**：`resize:vertical` 交给浏览器，右下角再用 `.ta-wrap::after` 画一个小折角，
  避免自定义滚动条把原生拖拽角盖住；双击文本框恢复默认高度（并清除记忆）。
- **尺寸记忆**：`ResizeObserver` 观察两个提示词框，把高度写入 localStorage；
  程序性改高（自动适配、恢复上次尺寸）用 `_programmatic` 标记跳过，不会把"自动高度"记成"用户拖拽"。
- **自动增高只增不减**：渲染出的提示词比框高就长高，最多半屏；变短时不自动缩，
  否则每次渲染页面都在跳。要缩就双击。
- **字段框用 textarea 而不是 input**：`rows=1` + `data-grow`，输入时按 `scrollHeight`
  长到内容高度（上限 150px），超过后滚动；标签再长也不会被横向截断，也不会把整列撑爆。
- **统一的滚动条样式**：`::-webkit-scrollbar` + `scrollbar-width:thin`，
  暗色下可见、悬停加深，避免"能不能滚"靠猜。

### 11.2 出图逻辑

| 行为 | 之前 | 现在 |
| --- | --- | --- |
| 改字段后提示词 | 必须手点「渲染提示词」 | 420ms 防抖后自动渲染 |
| 手改提示词后再改字段 | 无保护，点渲染会覆盖 | 标记 dirty，停止自动同步，出现「跟随模板」按钮 |
| 正向为空点生成 | 直接报错 | 先自动渲染一次，仍为空才提示 |
| 占位符没填 | 无提示 | warn toast 列出前三个，不阻断出图 |
| 耗时 | 无 | 按 `reports/baseline.json` 线性推算（面积 × 步数 × 张数），显示在按钮行 |
| 显存风险 | 无 | >3MP 或「多张 + >1.2MP」给出黄色预警行，不阻断 |
| 出图完成 | 结果可能在折叠区外 | 不在视口内时自动滚动到结果区 |

估算故意用本机基准而不是常数：`38.6s @ 1024² × 40 步` 是这台机器量出来的，
换显卡后重跑 `smoke_test.py` 就会自动跟着变。

### 11.3 本批验证（CDP 驱动真实浏览器）

> 宽屏"又窄又长"的根因：`main` 同时写了 `max-width:1240px` 和
> `padding:20px max(18px, calc((100% - 1240px)/2)) 120px`，而全局是 `box-sizing:border-box`。
> 1920px 屏上左右 padding 各 340px，内容区只剩 560px；2560px 屏会被压到接近 0。
> 现在两处都改成页面变量（`--page-max` / `--page-pad`），内边距不再吃掉最大宽度。

| 检查 | 结果 |
| --- | --- |
| 打开创作页 | 提示词自动渲染；正向框 188px；估算显示「预计 ~39s（基准 38.6s @ 1024² × 40 步）」 |
| 改字段 | 约 0.4s 后提示词自动包含新标签 |
| 手改提示词 | 出现「已手动编辑」提示，改字段不再覆盖；点「跟随模板」恢复 |
| 拖高提示词框 | `localStorage.studio.promptSizes = {"pos":340}`；刷新后高度仍为 338px |
| 字段框长文本 | 框长到 148px 后出现滚动条（`overflow-y:auto`） |
| 尺寸风险 | 2048² → 估算 154s + 「超过 3MP…建议 ≤1536²」；512²/6 步 → 估算 1s、无预警 |
| 出图 | 512²/6 步 3.2s 完成，结果自动滚入视口 |
| 宽屏布局 | 1366 / 1920 / 2560 三档截图 | 内容区 1240px 居中，正向提示词框约 700px 宽；不再被 padding 挤成 560px |
| 页面版本 | 启动打开的地址带 `?v=<index.html mtime>` | 页面改动即换 URL，已开的旧标签页不会冒充"修复无效" |
| 页面脚本错误 | 0 |

---

## 12. 增量四：本地模型管理与审计一键修复

### 12.1 模型页（第五个页签）

本机有三个 Ollama 模型（9B×2、27B×1）。之前只能靠命令行 `ollama stop` 释放显存，
出图前要手动腾地方。现在集中在一页：

| 区域 | 内容 |
| --- | --- |
| 显存与推理服务 | 显存占用条、Ollama 在线状态、常驻模型数与占用、ComfyUI 在线状态；「释放全部显存」「启动/停止 ComfyUI」 |
| 本地大模型 | 每个模型的参数量、量化、磁盘大小、是否驻留、驻留到期时间；单行「加载 / 卸载」；右上「全部卸载」 |

加载/卸载的语义（对着 Ollama 实测确认）：

```
加载 = POST /api/generate {"model": name, "prompt": "", "keep_alive": "15m"}  → done_reason=load
卸载 = POST /api/generate {"model": name, "prompt": "", "keep_alive": 0}      → done_reason=unload
```

- 空 prompt 不产生 token，只触发权重加载，9B 约 4 秒；
- 默认驻留 15 分钟（`config` 里不会塞这个值，它是 `app/ollama.py` 的默认常量，改一处即可）；
- **显存不够时 Ollama 会自己挤掉已驻留的模型**：实测先驻留 27B（13.6GB）再加载 9B，
  27B 被换出。这是 Ollama 的调度，不是控制台的行为，页面已写明；
- 「释放全部显存」= ComfyUI `POST /free {unload_models:true}` + `ollama stop` 全部模型，
  不停止任何服务；`config/studio-env.ps1` 新增 `STUDIO_OLLAMA_HOST` 可改地址。

ComfyUI 侧的模型不单独暴露：它按工作流按需加载，能做的只有「释放」（`/free`）与「启停」。

### 12.2 审计一键修复

审计引擎只报告事实；能不能自动修、怎么修，交给新的 `app/remediation.py`（纯映射，可单测）：

| kind | 行为 | 例子 |
| --- | --- | --- |
| `action` | 调 `POST /api/fix` 执行动作，成功后自动重跑审计 | NET-01/SVC-01 启动 ComfyUI、SVC-04 清理重复实例、PLT-01 释放显存、ENV-05 初始化目录、WKF-* 重新校验、COD-* 重跑检查 |
| `copy` | 前端把文本放进剪贴板 | ENV-04 安装命令、MDL-01..04 下载命令/缺失清单 |
| `goto` | 跳到能解决问题的页签 | AST-01/03/04 → 创作页 |

约束：

- **只有非 `pass` 的项才有修复按钮**；没有映射的项显示建议但不出按钮，不假装能修。
- 破坏性/耗时动作带确认（清理重复实例、释放显存、全部卸载）。
- 动作成功后自动重跑一次审计，用新的分数和状态闭环，而不是只弹个「成功」。
- 修复逻辑不在 HTTP 层：`server.py` 只负责把 findings 标注好、把 action 转给 `do_action`。

新增 `scripts/12-dedupe-comfyui.ps1`：保留 8188 监听进程及其启动器父进程，
停掉其它跑 `main.py` 的 python；`-WhatIf` 只打印不执行，幂等。

### 12.3 本批验证

| 检查 | 结果 |
| --- | --- |
| 模型清单 | `GET /api/models` 返回 3 个模型，`ollama.host=127.0.0.1:11434` |
| 加载 | `POST /api/models/load` 9B → 4s 后 `loaded=true`，`vram≈5.5GB` |
| 卸载 | `POST /api/models/unload` → `ps` 中消失；`/api/models` 显示 0 个常驻 |
| 释放全部显存 | `action: free_vram` → ComfyUI 模型卸载 + Ollama 全部卸载 |
| 去重脚本 | `-WhatIf` 输出 `kept pid 29776, stopped 0 duplicate process(es)` |
| 修复标注 | 39 项中 4 项带 fix（MDL-04 copy、AST-01/03/04 goto），其余无按钮 |
| 修复接口 | `MDL-04` → 400「在页面内完成（copy）」；未知 id → 404；`pass` 项 → 400 |
| UI | 模型页 3 行、加载后行内显示「已加载 5.9 GB / 驻留至 23:19」、卸载回「未加载」；体检页点「去创作页」切页签、点「复制缺失清单」提示「修复信息已复制」 |
| 页面脚本错误 | 0 |
