# Codex 模型切换器

把 Codex（CLI / 桌面端）一键切换到国内大模型：**DeepSeek / 通义千问 / 智谱 GLM / Kimi / 火山方舟**。

只接管“用哪个模型”这几个配置项，Codex 的其他原生功能一律不动，随时可切回原生状态。

- **形态**：tkinter 图形界面 + 单文件 `exe`，也有纯命令行 `codex_cn.py`
- **版本**：v1.0.0　**测试**：62 项 unittest 全绿　**平台**：Windows
- **原则**：不碰 Codex 的登录方式、联网搜索、推理档位等原生行为；改坏了可一键还原

## 目录

- [支持的服务商](#支持的服务商)
- [快速开始](#快速开始)
- [命令一览](#命令一览)
- [它到底改了什么](#它到底改了什么)
- [API Key 存在哪里](#api-key-存在哪里)
- [备份与还原](#备份与还原)
- [配置自检](#配置自检)
- [日志](#日志)
- [首次使用与 Codex 登录](#首次使用与-codex-登录)
- [自己构建](#自己构建)
- [项目结构](#项目结构)
- [测试](#测试)
- [FAQ](#faq)

## 支持的服务商

| id | 厂商 | base_url | 环境变量 | 默认模型 |
|---|---|---|---|---|
| `deepseek` | DeepSeek | `https://api.deepseek.com/` | `DEEPSEEK_API_KEY` | `deepseek-flash` |
| `dashscope` | 阿里云百炼（通义千问） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` | `qwen3.8-max` |
| `zai` | 智谱 ZAI | `https://open.bigmodel.cn/api/v1` | `ZAI_API_KEY` | `glm-5.3` |
| `kimi` | 月之暗面 Kimi | `https://api.kimi.com/coding/v1` | `KIMI_API_KEY` | `k3-256k` |
| `volcengine` | 火山方舟（豆包） | `https://ark.cn-beijing.volces.com/api/v3` | `ARK_API_KEY` | 填真实推理接入点 `ep-xxx` |

`native` 是一个特殊目标：撤销本工具接管的字段，切回 Codex 原生内置模型。

## 快速开始

### 图形界面

1. 双击 `CodexSwitch.exe`（首次会弹 SmartScreen 提示：更多信息 → 仍要运行）
2. 「切换模型」页选服务商 → 选模型（火山方舟填真实 `ep-xxx`）
3. 粘贴 API Key →「保存 Key」
4. 「预览更改」查看 diff → 「应用切换」
5. **完全退出 Codex**（桌面端和终端都要关）再重开 —— 这一步不做不生效

### 命令行

```powershell
python codex_cn.py setkey deepseek sk-你的Key      # 写入凭据管理器 + 环境变量
python codex_cn.py testkey deepseek                # 只读验证 Key 是否有效
python codex_cn.py switch deepseek                 # 切换
python codex_cn.py doctor                          # 13 项自检
```

## 命令一览

| 命令 | 作用 |
|---|---|
| `list` | 列出所有服务商与模型 |
| `switch <provider> [--model X] [--dry-run] [--refresh-catalog]` | 切换；`switch native` 切回原生 |
| `setkey <provider> <key> [--dry-run]` | 写 Key（凭据管理器 + 用户环境变量） |
| `testkey <provider> [--timeout 8]` | 测端点连通性与 Key，只读 |
| `show` | 打印当前 `config.toml`（已脱敏） |
| `doctor` | 配置自检 13 项 |
| `original` | 还原到本工具首次运行前的原始快照 |
| `restore [index]` | 还原历史备份（切换前自动备份，最多 10 份） |
| `--inline switch ...` | 把 Key 明文写进 `config.toml`（**不推荐**，会先给风险提示） |

## 它到底改了什么

只接管这些键，其余配置原样保留：

- 顶层 `model` / `model_provider` / `model_catalog_json`
- `[model_providers.<id>]` 整段
- `[desktop].enabled-reasoning-efforts`

**不碰**（默认关闭，可用设置页手动打开并会先弹风险提示）：
`preferred_auth_method`、`forced_login_method`、`web_search`、`model_reasoning_effort`、`show_raw_agent_reasoning`。

其他工程约束：

- 生成的 `wire_api` 一定是 `responses`（新版 Codex 见到 `chat` 会拒绝加载整份配置）
- `env_key` 只写环境变量名，不写 Key 本身
- 模型 slug 过白名单 `^[A-Za-z0-9][A-Za-z0-9._:\-]{0,119}$`，非法值直接拒绝
- 写文件走临时文件 + `fsync` + `os.replace`，失败自动回滚

## API Key 存在哪里

1. **Windows 凭据管理器**（系统加密，界面永远掩码显示，可一键清除）
2. **当前用户的环境变量**（`winreg` 直写注册表并广播变更；Codex 只认环境变量，这一步躲不掉）

不会写进 `config.toml` 明文，除非手动打开 `--inline` / 设置页那个开关（会先弹风险提示）。

安全方面：

- 界面日志、落盘日志、diff 预览**全链路脱敏**（`redact()` 统一处理）
- 源码与测试中无任何真实密钥字面量（有专门的回归测试守着）
- 「测试 Key」只发一次只读 `GET /models`：401/403 判失败，连不上只提示网络问题、不算失败

## 备份与还原

| 动作 | 说明 |
|---|---|
| 每次切换前 | 自动备份到 `~/.codex/backup-cn/<时间戳>/`，保留 10 份 |
| 首次运行 | 把原始 `config.toml` + `models.json` 封存到 `backup-cn/original/` |
| 「切回原生」 | 只撤销本工具接管的字段 |
| 「还原原始快照」 | 回到本工具第一次运行前的状态（≠ 出厂 OpenAI 配置，而是当时的本地状态） |

还原前会**先读取并校验备份内容**，坏备份直接拒绝安装，当前配置不会被动。

## 配置自检

`doctor` /「配置自检」页共 13 项：目录与文件是否存在、TOML/JSON 是否合法、`wire_api` 是否残留 `chat`、`base_url` 是否正确、环境变量是否真正持久化、模型是否在目录里……

- 还没切换过时，相关项显示灰字 **「尚未接入：在切换模型页点应用切换后自动生成」**，这是中性提示，**不算失败、不标红**
- 真故障（如 `wire_api` 被改回 `chat`）照旧标红并给出修复方法

## 日志

- 位置：`%LOCALAPPDATA%\CodexSwitch\logs\switch.log`（按 Windows 规范放用户数据目录，不写进客户的 `~/.codex`）
- 512 KB × 4 份轮转；写不进去自动退到 `%TEMP%\CodexSwitch\logs`
- 可用环境变量 `CODEXSWITCH_LOG_DIR` 覆盖（测试与便携部署用）
- 界面底部「操作日志」实时滚动，同一份内容也会落盘

## 首次使用与 Codex 登录

**本工具的切换不依赖 Codex 登录态。** 实测（codex-cli 0.157.1）：`CODEX_HOME` 下没有 `auth.json`、`codex login status` 显示 `Not logged in` 时，`codex exec` 依然能用配置里的 `env_key` 正常发起请求——认证完全走环境变量里的 Key。

- 目录、`config.toml`、`models.json` 都不存在时，切换会自动创建
- Codex 桌面端**首次启动**可能显示登录引导，那是 Codex 自己的流程，与本工具无关；按它自己的方式登录后再切换即可，两者互不影响，本工具也不碰 Codex 的登录信息
- 切换后**必须完全退出 Codex 再重开**才生效（环境变量对已运行的进程不生效）

## 自己构建

```powershell
# 1. 依赖（有锁文件，优先用它）
pip install -r requirements.lock          # 没有锁文件时退回 requirements.txt

# 2. 打包（纯 ASCII + CRLF 的批处理，6 步）
build_exe.bat
#    产物 dist\CodexSwitch.exe（约 11.6 MB）
#    同时拷贝 README_CN.txt 与 codex_cn.py（命令行版）到 dist\
```

打包相关约定：

- `app.manifest` 声明 PerMonitorV2 高 DPI + Common Controls v6，已在 `CodexSwitch.spec` 中挂载
- 图标由 `generate_icon.py` 生成（`icon.png` → `icon.ico`）
- 版本号写在 `version.txt`（PyInstaller 版本资源）

## 项目结构

```
codex_cn.py        引擎：providers / 配置解析与渲染 / 备份还原 / 自检 / 命令行入口
codex_gui.py       图形界面：切换、Key 管理、备份还原、预览 diff、自检、日志
test_codex_cn.py   引擎测试（含脱敏、原子写、还原校验、白名单、中性自检）
test_codex_gui.py  界面测试（临时目录隔离，不碰真实注册表/凭据/日志）
build_exe.bat      打包脚本（ASCII + CRLF）
CodexSwitch.spec   PyInstaller 配置（含 manifest）
app.manifest       高 DPI / 公共控件清单
README_CN.txt      随 exe 分发的简明说明书（UTF-8 BOM）
```

## 测试

```powershell
python -m unittest discover -s . -p "test_*.py"    # 62 项
```

测试全程隔离：临时 `CODEX_HOME`、假凭据库、`CODEXSWITCH_LOG_DIR` 指向临时目录，**绝不写真实注册表、真实凭据管理器、真实用户环境变量**。

## FAQ

**Q: 切换成功了但 Codex 没变？**
没有完全退出重启。桌面端要彻底退出，终端要新开一个。

**Q: 改坏了怎么恢复？**
「备份与还原」页选最近一份还原；或用「还原原始快照」回到最初状态；`native` 可只撤销本工具接管的字段。

**Q: 日志在哪？**
`%LOCALAPPDATA%\CodexSwitch\logs\switch.log`，界面底部也会实时显示。

**Q: 桌面端第一次打开要登录，是不是得先登录？**
那个引导是 Codex 自己的，与本工具无关（见上文实测结论）。

## 免责声明

内部使用，禁止外传与商业使用。切换到第三方模型时，你的提示词会发往对应厂商的接口，请自行遵守其数据策略。
