# 架构与目录职责

## 分层

```
控制台 HTTP (app/server.py)
        │
        ├── 领域层 app/studio.py       六阶段资产、验收门、产出索引与归档
        ├── 领域层 app/workflows.py    ComfyUI 工作流渲染与校验
        ├── 领域层 app/comfy.py        ComfyUI 客户端（提交/轮询/取图/中断/释放显存）
        ├── 领域层 app/ollama.py       Ollama 客户端（列表/加载/卸载/显存统计）
        ├── 领域层 app/remediation.py  审计问题 → 可执行的修复动作（纯映射）
        ├── 审计   app/audit_engine.py 30+ 项检查、评分、报告
        ├── 取证   app/checks.ps1      只读取证，stdout 只吐一个 JSON
        └── 运维   scripts/*.ps1       幂等、按编号执行
```

HTTP 层只做参数校验与序列化，所有判断都在领域层，因此领域层可以直接单测。

## 目录职责

| 目录 | 职责 | 入版本库 |
| --- | --- | --- |
| `docs/` | 设计、流程、排障、参数、审计 | 是 |
| `config/` | **唯一配置来源**：环境变量、权重清单、参数档位、提示词模板、工作流模板 | 是 |
| `scripts/` | 幂等运维脚本，按 `NN-动作.ps1` 编号 | 是 |
| `tests/` | 离线静态检查、工作流校验、1024×1024 出图验收 | 是 |
| `app/` | 控制台服务、领域层、审计引擎、取证脚本、单页界面 | 是 |
| `project/` | 创作产出；元数据入库，二进制不入库 | 部分 |
| `reports/` | 审计 JSON/Markdown、基准耗时 | 否 |
| `logs/` | ComfyUI stdout/stderr、控制台日志 | 否 |

权重不在本项目内：它们放在 ComfyUI 的 `models/` 子目录，由
`config/models.manifest.json` 描述目标位置与最小字节数。

## 关键设计决策

1. **config/ 是唯一配置来源。** 路径、端口、最低 ComfyUI 版本集中在
   `config/studio-env.ps1`；权重清单在 `models.manifest.json`；参数档位在
   `params.json`；提示词在 `prompts.json`；展示层设置（标题、视频阶段是否暂停、
   刷新节奏）在 `ui.json`。脚本不得内联硬编码参数。
   Python 不能 dot-source PowerShell，因此 `studio.load_studio_env()` 解析
   `$StudioEnv` 的字面量赋值（`Join-Path $ProjectRoot 'x'` 会被解析为真实路径）。

2. **只读取证与判断分离。** `app/checks.ps1` 只负责采集（环境变量、端口、脚本语法、
   ASCII 检查、磁盘、进程、GPU），输出一个 JSON；判定逻辑全在 Python 侧，便于测试。

3. **工作流模板带 `__meta__`。** 标题、阶段、依赖节点包、默认值都写在模板里，
   提交 `/prompt` 前由 `workflows.strip_meta()` 剥离。节点名以本机
   `/object_info` 为准 —— 不同 ComfyUI 版本和自定义节点包差异很大，模板不可能
   一次写对，所以 `scripts/20-verify-workflow.ps1` 负责校验而不是靠猜。

4. **验收门是硬约束。** `studio.evaluate_stages()` 依据磁盘实际状态评估六个阶段；
   第一个未通过的强制阶段会阻塞其后所有阶段。

5. **脚本 ASCII-only。** Windows PowerShell 5.1 对无 BOM 的 `.ps1` 按 ANSI 解析，
   非 ASCII 注释会乱码。`tests/lint_test.py` 强制校验这一点。

6. **停止脚本用 `return` 不用 `exit`。** 被编排脚本调用时 `exit` 会终止调用方。

7. **只绑定回环。** ComfyUI 与控制台都只监听 `127.0.0.1`；本机服务无鉴权，
   暴露到局域网等于开放任意代码执行。审计对此有独立检查项（NET-03 / SEC-01）。

8. **启动关键路径只做必要的事。** 顺序固定为：校验 config → 校验 ComfyUI launcher →
   绑定端口 → 打开浏览器 → 后台跑审计。审计是 2–3s 的取证，绝不能阻塞首屏；
   launcher 缺失时就地判 `missing` 并给出可复制命令，**不 spawn、不轮询**。
   后台脚本启动后要探测是否早退，不能用 `Popen` 成功代替启动成功。

9. **状态每次轮询重新推导。** `/api/state` 依据"是否在线 / launcher 是否存在 /
   当前是否有进行中的启动"重新计算，而不是沿用上一次写入的值，否则一次失败的启动
   会让状态永久停在 `starting`。

10. **出网只有一处。** 所有对 ComfyUI 的调用集中在 `app/comfy.py`：提交
    `/prompt`、轮询 `/queue` 与 `/history`、经 `/view` 取图、`/interrupt` 中断。
    前端不直连 `127.0.0.1:8188`（同源策略会拦），后端负责代理并把
    ComfyUI 的 `node_errors` 翻译成一句可读的失败原因。

11. **产出索引只读磁盘。** 产出页的数据来自 `studio.gallery_snapshot()` 对
    ComfyUI `output/` 与 `project/` 的扫描，不另建数据库；归档走
    `studio.archive_illust()`，图片落盘与 manifest 记录在同一函数内完成，
    保证「有图必有配方」。图片读取有路径穿越与后缀白名单校验。

12. **修复是声明式的。** `audit_engine` 只产事实；`remediation.FIXES` 用一张表把
    finding id 映射到 `action` / `copy` / `goto`。`server.py` 只做「标注 finding、
    转给 do_action、执行完重跑审计」，不在 HTTP 层写判断。只有非 pass 的项才会
    带修复；破坏性动作必须带 confirm。

13. **本地模型也是服务。** ComfyUI 与 Ollama 都当作可启停、可释放显存的外部服务：
    `comfy.py` / `ollama.py` 各自封装出网调用，前端只通过 `/api/models*` 与
    `/api/action` 操作，不直接拼地址。
