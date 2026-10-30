# 参数速查

表中的值是起点，最终以你机器上的实测为准。唯一来源是 `config/params.json`，
控制台「提示词」页与 `tests/smoke_test.py` 都从它取默认值。

## 出图与生成

| 环节 | 模型 | 分辨率 | 步数 | CFG | 采样器 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| Anima 出图 | anima-base-v1.0 | 约 1MP（512²–1536²） | 30–50 | 4–5 | er_sde，软线条用 euler_a | `masterpiece, best quality, score_7` |
| Anima Turbo | Anima-Turbo | 约 1MP | 8–12 | 1 | euler | 快、多样性低 |
| Anima Aesthetic | Anima-Aesthetic | 约 1MP | 按模型卡 | 按模型卡 | euler | 作者建议不用 score 标签 |
| LoRA 测试 | base + LoRA 0.6–1.0 | 约 1MP | 30 | 4 | er_sde | 固定 3 个种子对比 |
| 姿势严格镜头 | Illustrious XL + ControlNet | 原生最高 1536² | 按底模卡 | 按底模卡 | 按底模卡 | union / anytest 类 ControlNet |
| 换装与变体 | Qwen-Image 2.1 编辑 | 按模板 | 按模板 | 按模板 | 按模板 | 先说保持什么，再说一个改变 |
| 图生视频 | Wan 2.2 I2V A14B + lightx2v | 832×480 | 4 或 8 | 1 | Euler + simple | Shift 5.0，81 帧 ≈ 5 秒 |

## LoRA 训练

| 项目 | 起点 | 说明 |
| --- | --- | --- |
| 底模 | anima-base-v1.0 | 模型卡说明 LoRA 应在 Base 版本上训练 |
| 分辨率 | 1024 | 开启宽高比分桶 |
| 学习率 | 1e-5 – 2e-5 | 社区案例与官方讨论都落在这个区间 |
| rank | 32–64 | 社区多用 64；16GB 都放得下 |
| batch | 1–4 | 按显存调整 |
| 优化器 | AdamW8bit | 训练工具默认 |
| 保存间隔 | 每 200 步 | 用来事后挑最早稳定的那个 |
| 显存占用 | 约 7–14GB | 溢出到内存会明显变慢 |

步数以样图为准，不以数字为准：社区报告从约 1.8k 步成熟、2.4k–3k 步开始过拟合，
到 Prodigy 选 6k 步，再到 12k 步以上方向控制反而更差，差异主要来自数据集与训练器。

## 漫画分格

| 项 | 值 |
| --- | --- |
| 横格 | 1152 × 896 |
| 竖格 | 896 × 1152 |
| 方图 | 1024 × 1024 |
| 原则 | 保持 1MP 左右，不要为贴合格子硬拉极端比例 |

可选景别与视角：`close-up`、`portrait`、`upper body`、`cowboy shot`、`full body`、
`from above`、`from below`、`from side`、`from behind`。

常用表情：`smile`、`angry`、`crying`、`surprised`、`embarrassed`、`serious`。

## 基准记录

`tests/smoke_test.py` 会把实测耗时、分辨率、显存写进 `reports/baseline.json`。
改动机器的任何一项（驱动、ComfyUI 版本、权重量化）之后重跑一次，基线才有意义。
