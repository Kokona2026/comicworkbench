# anima-studio · 漫画工作台

本地二次元漫画／插画创作控制台：在 Windows 上把 ComfyUI 与 Anima 出图链路收进一个面板，
带六阶段验收门、工作流模板库、产出索引与一键归档。

**仓库只放代码、配置与文档。模型权重、ComfyUI 本体、出图产物都不入库。**

## 目录

| 路径 | 说明 |
|---|---|
| `app/` | 控制台（Python 标准库，无第三方依赖） |
| `config/` | 唯一配置来源：工作流模板、提示词库、参数档位、模型清单 |
| `scripts/` | 幂等运维脚本（00–30 编号） |
| `tests/` | 离线静态检查、工作流校验、冒烟测试 |
| `docs/` | 架构、流程、参数、审计说明 |
| `环境依赖说明.md` | 版本矩阵、Python 依赖、自定义节点、模型清单、复原步骤、已知坑 |
| `安装.ps1` | 自检：写用户级环境变量、检查 ComfyUI / Python / 权重 |

## 快速开始

```powershell
git clone https://github.com/Kokona2026/comicworkbench.git anima-studio
cd anima-studio

# 1. 装 ComfyUI（约 46 GB，见 环境依赖说明.md 第 5 节）
# 2. 按第 4 节放模型权重
# 3. 自检
powershell -NoProfile -ExecutionPolicy Bypass -File .\安装.ps1 -ComfyUIDir "D:\anima-studio\ComfyUI"

# 4. 启动
.\start.cmd      # 控制台 http://127.0.0.1:8766
.\audit.cmd      # 只体检，不起服务
```

## 环境要求

Windows 11 · Python 3.12 · RTX 30/40/50 系 8 GB 显存起（SDXL 与 krea2 建议 12 GB+）。
完整版本矩阵见 [环境依赖说明.md](环境依赖说明.md)。

## 说明与边界

- 本项目**不包含**任何模型权重。模型使用需遵守各自许可，例如 Anima 标注为 CircleStone Labs 非商业许可。
- 仓库不含任何图像产出。出图结果保留在本地 `project/` 目录，`.gitignore` 已排除。
- 本项目是工具，不附带任何第三方角色素材。使用者以本工具生成内容的合规性由使用者自负。

## 许可

代码以 MIT 许可发布，见 [LICENSE](LICENSE)。第三方组件与模型的许可见 [NOTICE](NOTICE)。
