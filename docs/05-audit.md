# 本地创作环境审计报告

- 生成时间：2026-10-31T00:28:44+08:00
- 评分：**85 / 100（B）**
- 检查项：39 项，通过 37，警告 2，失败 0
- 深度校验：否
- 耗时：2640 ms
- 项目根：`C:\Users\Admin\CodeBuddy\20261029191544`
- ComfyUI：`http://127.0.0.1:8188`

## 关键指标

| 指标 | 值 | 说明 |
| --- | --- | --- |
| Python | 3.12.10 |  |
| ComfyUI 目录 | C:\Users\Admin\CodeBuddy\20261029191544\ComfyUI | 用户级 STUDIO_COMFYUI_DIR 可覆盖默认值 |
| ComfyUI 版本 | 0.43.0 |  |
| 可用节点数 | 1334 |  |
| 必需权重 | 3/3 | anima + text encoder + vae |
| Anima + 5 风格 LoRA 链（カントク / てぃんくる / 魔太郎 / 鬼针草 / 白玉） | ok | 无问题 |
| Anima Base v1 + 双风格 LoRA（鬼针草 × 白玉）混合插画 | ok | 无问题 |
| Anima Base v1 + character LoRA - test grid cell | ok | 无问题 |
| 白色泳装专用 · Anima Base v1 + 鬼针草 × 白玉 | ok | 无问题 |
| Anima Base v1 - text to image | ok | 无问题 |
| Illustrious XL（SDXL 系）+ LoRA 槽位 | ok | 无问题 |
| Krea 2 Turbo + Claudechan 角色 LoRA（古典书 × Anthropic 花 × 黑发饰） | ok | 无问题 |
| Wan 2.2 I2V A14B - image to video (5s clip) | ok | 无问题 |
| 提示词模板 | 24 | config/prompts.json 是唯一来源 |
| 参数档位 | 7 | config/params.json |
| 候选/训练/checkpoint | 40/24/0 | 40-60 候选 -> 20-40 训练 -> 选定 checkpoint |
| GPU | NVIDIA GeForce RTX 5070 Ti 16303 MB | 已用 6333 MB，利用率 1% |
| 磁盘可用 | 1655.7 GB |  |

## 检查结果

| ID | 类别 | 检查项 | 严重度 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| ENV-01 | 环境 | Python 版本满足 3.9+ | critical | **PASS** | Python 3.12.10 |
| ENV-02 | 环境 | config/ 唯一配置来源完整 | high | **PASS** | 全部就位 |
| ENV-03 | 环境 | 控制台端口配置合法 | low | **PASS** | STUDIO_CONSOLE_PORT=8766 |
| ENV-04 | 环境 | ComfyUI 安装已定位 | critical | **PASS** | C:\Users\Admin\CodeBuddy\20261029191544\ComfyUI\main.py |
| ENV-05 | 环境 | 创作目录 00-05 就位 | medium | **PASS** | 全部就位 |
| NET-01 | 网络 | ComfyUI 端口 8188 已监听 | high | **PASS** | pid=11644 (python) |
| NET-02 | 网络 | 控制台端口 8766 由本项目控制台持有 | info | **PASS** | pid=31000 (python) 正在运行 |
| NET-03 | 网络 | 服务仅绑定回环地址 | critical | **PASS** | 全部 127.0.0.1 |
| SVC-01 | 服务 | ComfyUI /system_stats 可访问 | critical | **PASS** | 0.43.0 |
| SVC-02 | 服务 | ComfyUI 版本 >= 0.3.31 | high | **PASS** | 0.43.0 |
| SVC-03 | 服务 | 已获取节点清单 /object_info | medium | **PASS** | 1334 个节点类型 |
| SVC-04 | 服务 | 无重复 ComfyUI 实例 | low | **PASS** | 1 个独立实例（2 个进程，含启动器与子进程） |
| MDL-01 | 模型 | anima-base-v1.0.safetensors 在位 | critical | **PASS** | 4.18 GB |
| MDL-02 | 模型 | qwen_3_06b_base.safetensors 在位 | critical | **PASS** | 1.19 GB |
| MDL-03 | 模型 | qwen_image_vae.safetensors 在位 | critical | **PASS** | 0.25 GB |
| MDL-04 | 模型 | 可选权重（阶段 3-5） | info | **PASS** | 全部就位；暂缓阶段跳过 1 个（wan2.2-i2v-a14b-q8.gguf） |
| WKF-01 | 工作流 | 模板 JSON 结构合法 | high | **PASS** | 8/8 个模板通过 |
| WKF-02 | 工作流 | 节点类型已在本机注册 | high | **PASS** | 全部注册 |
| WKF-03 | 工作流 | 节点输入名与 /object_info 一致 | medium | **PASS** | 一致 |
| WKF-04 | 工作流 | 提示词模板占位符声明一致 | medium | **PASS** | 全部一致 |
| AST-00 | 资产 | 阶段 0 环境准备（2/2） | high | **PASS** | 三个必需权重在位=OK; 基准耗时已记录=OK |
| AST-01 | 资产 | 阶段 1 角色设计（1/2） | high | **WARN** | 设定表字段齐全=缺 trigger; 候选图 40-60 张=OK |
| AST-02 | 资产 | 阶段 2 训练角色 LoRA（2/4） | high | **WARN** | 训练集 20-40 张=OK; 每张图都有标签文件=OK; 标签以触发词开头=不匹配 24; 已选定 checkpoint=写入 selected.json |
| AST-03 | 资产 | 阶段 3 单张插画（2/2） | medium | **PASS** | 终稿已产出=OK; 种子/提示词/LoRA 强度已记录=OK |
| AST-04 | 资产 | 阶段 4 漫画页（3/3） | medium | **PASS** | 分镜表 4-6 格=OK; 分镜字段完整=OK; 整页已导出=OK |
| AST-05 | 资产 | 阶段 5 动画短片（暂缓） | info | **PASS** | 按排期暂缓（config/ui.json），不参与阻塞判断 |
| COD-01 | 代码质量 | PowerShell 脚本语法通过 PSParser | high | **PASS** | 15 个文件 |
| COD-02 | 代码质量 | PowerShell 脚本 ASCII-only | medium | **PASS** | 全部 ASCII |
| COD-03 | 代码质量 | Python 文件可编译 | high | **PASS** | 18 个文件 |
| COD-04 | 代码质量 | config/ 下 JSON 均可解析 | high | **PASS** | 15 个文件 |
| COD-05 | 代码质量 | 运维脚本按 NN-动作.ps1 编号 | low | **PASS** | 10/13 个带编号 |
| SEC-01 | 安全 | ComfyUI 地址仅限回环 | critical | **PASS** | http://127.0.0.1:8188 |
| SEC-02 | 安全 | .gitignore 覆盖运行时产物 | medium | **PASS** | 已覆盖 |
| SEC-03 | 安全 | 配置中不含密钥 | high | **PASS** | 未发现 |
| SEC-04 | 安全 | 控制台校验 Host 为回环 | high | **PASS** | 已实现 _host_ok |
| SEC-05 | 安全 | 许可条款已标注 | low | **PASS** | Anima 为 CircleStone Labs 非商业许可 |
| PLT-01 | 平台 | GPU 显存满足 16GB 级 | high | **PASS** | NVIDIA GeForce RTX 5070 Ti 6333/16303 MB |
| PLT-02 | 平台 | 磁盘余量充足（>= 50GB） | medium | **PASS** | 1655.7 GB 可用（盘 C） |
| PLT-03 | 平台 | Windows 平台 | info | **PASS** | Windows 11 |

## 本轮已修复

1. config/ 作为唯一配置来源：路径、端口、最低 ComfyUI 版本集中到 config/studio-env.ps1
2. scripts/ 全部 ASCII-only，避免 Windows PowerShell 5.1 按 ANSI 解析导致乱码
3. 停止类脚本使用 return 而非 exit，避免被编排脚本调用时提前终止调用方
4. 工作流模板带 __meta__（标题/阶段/依赖节点包/默认值），提交 /prompt 前剥离
5. 工作流节点名以本机 /object_info 为准，脚本 20-verify-workflow.ps1 负责校验
6. 六阶段验收门由 app/studio.py 统一评估，验收不过不允许进入下一阶段
7. train 集标签以触发词开头，固定特征标签删除，交给 LoRA 从像素学习

## 使用方式

```powershell
python app\server.py            # 图形控制台
python app\audit_engine.py      # 命令行审计
python tests\lint_test.py       # 离线静态检查
python tests\smoke_test.py      # 1024x1024 出图验收
```

报告文件：`reports/audit-latest.json`、`reports/audit-latest.md`、`docs/05-audit.md`。
