# Kokona2026 · 漫画工作台（anima-studio）

本机 Windows 11 上的二次元漫画／插画创作控制台：ComfyUI + Anima 出图链路、
六阶段验收门、控制台 UI、工作流模板库。

## 内容

| 路径 | 说明 |
|---|---|
| `环境依赖说明.md` | 版本矩阵、Python 依赖、5 个自定义节点、模型清单（含底模与 LoRA）、复原步骤、已知坑 |
| `安装.ps1` | 自检脚本：写用户级环境变量、检查 ComfyUI/Python/权重 |
| `start.cmd` / `audit.cmd` | 一键启动 / 只体检 |
| `app/` | 控制台（Python 标准库，无第三方依赖） |
| `config/` | 唯一配置来源：8 个工作流模板、提示词库、参数档位、模型清单 |
| `scripts/` | 幂等运维脚本（00–30 编号） |
| `tests/` | 离线静态检查、工作流校验、冒烟测试 |
| `docs/` | 架构、流程、参数、审计说明 |
| `project/` | 成品：插画与漫画页 |

## 快速开始（目标机器）

```powershell
# 1. 解压分卷（若下载的是 .001–.004）
.\合并.cmd            # 或 powershell -File .\合并.ps1

# 2. 解压 anima-studio-portable-20261031.zip

# 3. 装 ComfyUI（约 46 GB）：见 环境依赖说明.md 第 5 节
# 4. 放模型权重：见 环境依赖说明.md 第 4 节
# 5. 自检
powershell -NoProfile -ExecutionPolicy Bypass -File .\安装.ps1 -ComfyUIDir "D:\anima-studio\ComfyUI"

# 6. 启动
.\start.cmd          # 控制台 http://127.0.0.1:8766
```

## 包内不含

ComfyUI 本体（46.8 GB）、约 20 GB 模型权重、`.cache`、运行时日志。
原因与补装方式写在 `环境依赖说明.md` 第 0 节与第 5 节。

## 校验

```
anima-studio-portable-20261031.zip
SHA256 AEBE1844AAD93502FBF780556EB00DD0F28BF121877916BEC960B11694866E1C
```
