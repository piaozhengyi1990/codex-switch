#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codex_cn.py — 把 Codex 切换到国内大模型的本地配置器。

设计约束：
  1. 只接管与"换模型"直接相关的配置：
       顶层 model / model_provider / model_catalog_json
       [model_providers.<id>] 整段
       [desktop] 里的 enabled-reasoning-efforts（各模型推理档位）
     其余 Codex 原生配置（MCP、plugins、marketplaces、sandbox、
     登录方式、web_search、notify…）一律原样保留。
  2. 首次运行会把原始 config.toml + models.json 封存到
     ~/.codex/backup-cn/original/，随时可用 `original` 命令一键切回原样。

用法：
    python codex_cn.py list                     查看所有提供商与 Key 状态
    python codex_cn.py switch deepseek          切到 DeepSeek
    python codex_cn.py switch kimi --model k3   切到 Kimi 并指定模型
    python codex_cn.py setkey deepseek sk-xxx   只更新某个提供商的 API Key
    python codex_cn.py original                 一键还原到切换前的原始状态
    python codex_cn.py show / doctor / restore  查看 / 自检 / 还原历史备份
    python codex_cn.py switch zai --dry-run     只打印不写入

只改 API Key 的最短路径：`python codex_cn.py setkey <id> sk-xxx` -> `switch <id>` -> 重启 Codex。
或者直接用图形界面 CodexSwitch.exe（Key 存进 Windows 凭据管理器，不写进源码）。

安全约定（工业级）：
  1. 源码里不存在任何 API Key，只从凭据管理器 / 环境变量读取。
  2. 所有对磁盘的写入都是"临时文件 + fsync + os.replace"，中途崩溃不会留下半截配置。
  3. 还原备份 / 快照前先做格式校验，坏文件一律拒绝安装。
  4. 输出到界面与日志的文本统一过 redact() 脱敏，密钥不会明文落盘。

依赖：Python 3.11+ 标准库，无需 pip install。
依据：https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/codex
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

# 重定向到管道/文件时用 UTF-8，避免中文乱码（真实控制台仍走系统 API）
# 注意：打包成无控制台 exe 时 sys.stdout 可能为 None，必须先判空。
_stdout, _stderr = sys.stdout, sys.stderr
if _stdout is not None and _stderr is not None \
        and not _stdout.isatty() and _stdout.encoding \
        and _stdout.encoding.lower() != "utf-8":
    try:
        _stdout.reconfigure(encoding="utf-8", errors="replace")
        _stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
del _stdout, _stderr


# ===========================================================================
# 1. API Key 从哪里来（源码里绝不出现明文 Key）
# ===========================================================================
# 读取优先级：Windows 凭据管理器 -> 当前进程环境变量 -> 持久化用户环境变量
# 写入入口只有两个：
#   命令行    python codex_cn.py setkey <id> sk-xxx
#   图形界面  CodexSwitch.exe 的「API Key」页（存进 Windows 凭据管理器）
#
# 模型 slug / 提供商 id 的白名单：渲染 config.toml 是字符串拼接，
# 输入必须先过白名单，不能只靠"生成后再校验"兜底。
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,119}$")

# 输出脱敏：凡是进界面、进日志的文本都必须先过 redact()。
_TOKEN_ASSIGN_RE = re.compile(r'(experimental_bearer_token\s*=\s*)"([^"]*)"')
_ENV_ASSIGN_RE = re.compile(r'(?i)([A-Za-z0-9_]*_api_key)(\s*=\s*)"([^"]*)"')
_RAW_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{16,}")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}")


def redact(text: str) -> str:
    """把可能的密钥换成掩码，防止明文落进界面与日志文件。"""
    if not text:
        return text
    out = _TOKEN_ASSIGN_RE.sub(lambda m: f'{m.group(1)}"{mask(m.group(2))}"', text)
    out = _ENV_ASSIGN_RE.sub(lambda m: f'{m.group(1)}{m.group(2)}"{mask(m.group(3))}"',
                             out)
    out = _RAW_KEY_RE.sub(lambda m: mask(m.group(0)), out)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)}***", out)
    return out


def build_diff(old_text: str, new_text: str,
               label_old: str = "当前 config.toml",
               label_new: str = "切换后 config.toml") -> str:
    """unified diff（已脱敏），供界面预览真实改动，而不是只给一整份配置。"""
    diff = "\n".join(difflib.unified_diff(
        (old_text or "").splitlines(), (new_text or "").splitlines(),
        fromfile=label_old, tofile=label_new, lineterm=""))
    return redact(diff)

# "env"    = Key 写进环境变量，config.toml 里只有变量名（Codex 官方推荐）
# "inline" = Key 明文写进 config.toml（桌面 App 读不到环境变量时用）
AUTH_MODE = "env"

# 是否额外写入 DeepSeek 官方脚本那几个非模型字段：
#   preferred_auth_method / forced_login_method / model_reasoning_effort
#   web_search / show_raw_agent_reasoning
# 这些会改变 Codex 原生行为（登录方式、联网搜索、推理档位…）。
# 默认 False —— 严格遵守"只替换模型，不碰 Codex 原生功能"。
# 需要 DeepSeek 官方完整配置时改成 True。
OFFICIAL_EXTRAS = False

# 下面两个常量只在 OFFICIAL_EXTRAS = True 时生效
FORCE_API_AUTH = True
SHOW_RAW_REASONING = True


# ===========================================================================
# 2. 提供商定义（只收录已确认支持 OpenAI Responses API 的国内服务商）
# ===========================================================================

EFFORT_DESC = {
    "minimal": "Minimal reasoning",
    "low": "Light reasoning",
    "medium": "Balances speed and reasoning depth",
    "high": "Enhanced reasoning",
    "xhigh": "Extra high reasoning depth for complex problems",
    "ultra": "Ultra deep reasoning",
    "max": "Deep reasoning",
}

# 顶层 model_reasoning_effort 的合法枚举，超出的一律降级
VALID_TOP_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}

PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/",
        "env_key": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-flash",
        "reasoning_effort": "high",
        "web_search": "disabled",
        "efforts": ["low", "medium", "high", "xhigh", "ultra", "max"],
        "doc": "https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/codex",
        "models": [
            {
                "slug": "deepseek-flash",
                "name": "DeepSeek-Flash",
                "desc": "Latest frontier agentic coding model with image input.",
                "ctx": 1048576,
                "image": True,
                "efforts": ["low", "high", "max"],
                "default": "high",
                "priority": 1,
                "summaries": True,
                "patch": "freeform",
            },
            {
                # 兜底元数据。DeepSeek 官方一键脚本写入的完整目录会被本脚本原样保留。
                "slug": "deepseek-v4-pro",
                "name": "DeepSeek-V4-Pro",
                "desc": "DeepSeek V4 Pro coding model.",
                "ctx": 131072,
                "image": False,
                "efforts": ["low", "high", "max"],
                "default": "high",
                "priority": 2,
                "summaries": True,
                "patch": "freeform",
            },
        ],
    },

    "dashscope": {
        "name": "Alibaba Model Studio / Qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env_key": "DASHSCOPE_API_KEY",
        "default_model": "qwen3.8-max",
        "reasoning_effort": "xhigh",
        "web_search": None,
        "efforts": ["low", "medium", "high", "xhigh"],
        "doc": "https://help.aliyun.com/zh/model-studio/codex",
        "models": [
            {"slug": "qwen3.8-max", "name": "qwen3.8-max",
             "desc": "DashScope model: qwen3.8-max",
             "ctx": 983616, "image": True, "efforts": ["low", "medium", "xhigh"],
             "default": "xhigh", "priority": 1, "summaries": False, "shell": "default"},
            {"slug": "qwen3.8-flash", "name": "qwen3.8-flash",
             "desc": "DashScope model: qwen3.8-flash",
             "ctx": 983616, "image": True, "efforts": ["low", "medium", "xhigh"],
             "default": "xhigh", "priority": 2, "summaries": False, "shell": "default"},
            {"slug": "qwen3.7-max", "name": "qwen3.7-max",
             "desc": "DashScope model: qwen3.7-max",
             "ctx": 1000000, "image": False,
             "efforts": ["low", "medium", "high", "xhigh"],
             "default": "medium", "priority": 3, "summaries": False, "shell": "default"},
            {"slug": "qwen3.7-plus", "name": "qwen3.7-plus",
             "desc": "DashScope model: qwen3.7-plus",
             "ctx": 1000000, "image": True,
             "efforts": ["low", "medium", "high", "xhigh"],
             "default": "medium", "priority": 4, "summaries": False, "shell": "default"},
            {"slug": "qwen3.6-flash", "name": "qwen3.6-flash",
             "desc": "DashScope model: qwen3.6-flash",
             "ctx": 1000000, "image": True,
             "efforts": ["low", "medium", "high", "xhigh"],
             "default": "medium", "priority": 5, "summaries": False, "shell": "default"},
        ],
    },
}
PROVIDERS.update({
    "zai": {
        "name": "ZAI",
        "base_url": "https://open.bigmodel.cn/api/v1",
        "env_key": "ZAI_API_KEY",
        "default_model": "glm-5.3",
        "reasoning_effort": "high",  # 官方写 "max"，超出 Codex 枚举，降级为 high
        "web_search": None,
        "efforts": ["low", "high", "max"],
        "doc": "https://docs.bigmodel.cn/cn/coding-plan/tool/codex",
        "models": [
            {"slug": "glm-5.3", "name": "glm-5.3",
             "desc": "Z.ai's latest flagship model",
             "ctx": 1048576, "image": False, "efforts": ["low", "high", "max"],
             "default": "max", "priority": 0, "summaries": True, "patch": "freeform"},
            {"slug": "glm-5-turbo", "name": "glm-5-turbo",
             "desc": "Agent-optimized model",
             "ctx": 204800, "image": False, "efforts": [],
             "default": "max", "priority": 1, "summaries": True, "patch": "freeform"},
        ],
    },

    "kimi": {
        "name": "Kimi",
        "base_url": "https://api.kimi.com/coding/v1",
        "env_key": "KIMI_API_KEY",
        "default_model": "k3-256k",
        "reasoning_effort": "high",
        "web_search": "live",
        "efforts": ["low", "high", "max"],
        "doc": "https://www.kimi.com/code/docs/third-party-tools/codex.html",
        "models": [
            {"slug": "k3", "name": "K3", "desc": "K3, 1M context",
             "ctx": 1048576, "image": True, "efforts": ["low", "high", "max"],
             "default": "high", "priority": 0, "summaries": True},
            {"slug": "k3-256k", "name": "K3 256K", "desc": "K3 256K context",
             "ctx": 262144, "image": True, "efforts": ["low", "high", "max"],
             "default": "high", "priority": 1, "summaries": True},
        ],
    },

    "volcengine": {
        "name": "Volcengine Ark / Doubao",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "env_key": "ARK_API_KEY",
        # 必须填控制台的推理接入点 ID（ep-xxx）或已开通的模型名，不要写 "doubao"
        "default_model": "REPLACE_WITH_ENDPOINT_ID",
        "reasoning_effort": "high",
        "web_search": None,
        "efforts": ["low", "medium", "high", "xhigh"],
        "doc": "https://www.volcengine.com/docs/82379/1958524",
        "models": [
            {"slug": "REPLACE_WITH_ENDPOINT_ID", "name": "Ark Endpoint",
             "desc": "Replace with your Volcengine endpoint ID (ep-xxx)",
             "ctx": 131072, "image": False,
             "efforts": ["low", "medium", "high", "xhigh"],
             "default": "high", "priority": 0, "summaries": False},
        ],
    },
})

# 仅 Chat Completions、Codex 无法直连的服务商（列出以免误配）
UNSUPPORTED = {
    "Kimi 开放平台 (api.moonshot.cn)": "官方仅 Chat Completions，请改用 kimi",
    "智谱标准接口 (api/paas/v4)": "请改用 zai (open.bigmodel.cn/api/v1)",
    "SiliconFlow 硅基流动": "公开 /v1/responses 返回 404，暂不可直连",
    "MiniMax": "未确认原生 Responses 支持，暂不写入配置",
}

# 会被清理的旧 / 别名 provider id（残留 wire_api = "chat" 会让整个配置加载失败）
LEGACY_PROVIDER_IDS = {
    "ZAI", "zhipu", "bigmodel", "qwen", "moonshot",
    "Model_Studio", "Model_Studio_Token_Plan",
    "Model_Studio_Token_Plan_Personal", "Model_Studio_Coding_Plan",
}

# 本脚本只接管与"换模型"直接相关的三个顶层键，
# 其余 Codex 原生配置一律原样保留。
MANAGED_TOP_KEYS = [
    "model", "model_provider", "model_catalog_json",
]

# OFFICIAL_EXTRAS = True 时才会接管的额外键
EXTRA_TOP_KEYS = [
    "model_reasoning_effort", "web_search", "show_raw_agent_reasoning",
    "preferred_auth_method", "forced_login_method",
]

# [desktop] 里唯一接管的键：各模型的推理档位列表（换模型必须同步）
DESKTOP_EFFORTS_KEY = "enabled-reasoning-efforts"


# ===========================================================================
# 3. 路径与环境变量
# ===========================================================================

def codex_home() -> Path:
    return Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") \
        else Path.home() / ".codex"


CODEX_DIR = codex_home()
CONFIG_PATH = CODEX_DIR / "config.toml"
CATALOG_PATH = CODEX_DIR / "models.json"
BACKUP_DIR = CODEX_DIR / "backup-cn"
ORIGINAL_DIR = BACKUP_DIR / "original"


def set_codex_home(path) -> None:
    """把配置目录指向别处（自动化测试用）。"""
    global CODEX_DIR, CONFIG_PATH, CATALOG_PATH, BACKUP_DIR, ORIGINAL_DIR
    CODEX_DIR = Path(path)
    CONFIG_PATH = CODEX_DIR / "config.toml"
    CATALOG_PATH = CODEX_DIR / "models.json"
    BACKUP_DIR = CODEX_DIR / "backup-cn"
    ORIGINAL_DIR = BACKUP_DIR / "original"


def is_placeholder(value: str | None) -> bool:
    if not value or not value.strip():
        return True
    v = value.strip()
    return v.startswith("<") or v.startswith("REPLACE") or "你的" in v \
        or "xxxx" in v.lower()


def read_user_env(name: str) -> str | None:
    """读持久化的用户环境变量（Windows 读注册表，setx 后当前进程也能读到）。"""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
                return value if isinstance(value, str) else None
        except OSError:
            return None
    return os.environ.get(name)


def write_user_env(name: str, value: str) -> bool:
    """持久化用户环境变量。成功返回 True；失败打印原因并返回 False。

    走 winreg 直接写 HKCU\\Environment（不再用 setx）：
      - setx 会把 Key 明文暴露在命令行上，任何同权限进程都能在进程列表里看到；
      - setx 失败时的返回码此前被 check=False 丢掉，属于静默失败。
    写完广播 WM_SETTINGCHANGE，让 Explorer 拉起的新进程能读到新值。
    """
    if os.name != "nt":
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)
        print(f'  请自行把这行加进 ~/.zshrc 或 ~/.bashrc：\n    export {name}="{value}"')
        return True

    err = _env_registry_set(name, value)
    if err:
        print(f"[错误] 写入用户环境变量 {name} 失败：{err}")
        print("       环境变量没有变化，本次操作不能算成功。")
        return False

    _broadcast_env_change()
    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)
    return True


def _env_registry_set(name: str, value: str) -> str | None:
    """写 HKCU\\Environment。返回 None 表示成功，否则返回错误描述。"""
    try:
        import winreg
    except ModuleNotFoundError:
        return "当前平台没有 winreg 模块"
    try:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                                 winreg.KEY_SET_VALUE)
        except FileNotFoundError:  # 极少数精简系统没有这个键
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0,
                                     winreg.KEY_SET_VALUE)
        with key:
            if value:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            else:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass  # 本来就没有，等价于清除成功
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _broadcast_env_change() -> None:
    """通知 Explorer 等进程重新加载环境变量（setx 也是这么做的）。"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer("Environment")
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, ctypes.addressof(buf), 0x0002, 5000, None)
    except Exception:  # noqa: BLE001  广播失败不影响本进程写入结果
        pass


def resolve_key(pid: str) -> str | None:
    """优先级：凭据管理器 -> 当前进程环境变量 -> 持久化环境变量。"""
    env_name = PROVIDERS[pid]["env_key"]
    stored = cred_read(env_name)
    if stored and not is_placeholder(stored):
        return stored.strip()
    for candidate in (os.environ.get(env_name), read_user_env(env_name)):
        if candidate and not is_placeholder(candidate):
            return candidate.strip()
    return None


def mask(key: str | None) -> str:
    if not key:
        return "未设置"
    if len(key) <= 10:
        return "****"
    return f"{key[:6]}...{key[-4:]}"


# ===========================================================================
# 3.5 凭据管理器（Windows Credential Manager，界面保存 API Key 用）
#
# 为什么还要写环境变量：Codex 只认 env_key 指向的环境变量，
# 这一步躲不掉。凭据管理器的价值在于：
#   - 界面上永远掩码显示，不回显明文
#   - 不写进 config.toml / .env 之类的明文文件
#   - 支持一键集中清除
# ===========================================================================

CRED_PREFIX = "codex_cn:"

_CRED_STRUCTS: list = []


def _cred_api():
    """返回 (CredWriteW, CredReadW, CredFree, CREDENTIAL)；非 Windows 返回 None。"""
    global _CRED_STRUCTS
    if os.name != "nt":
        return None
    if _CRED_STRUCTS:
        return _CRED_STRUCTS
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    PCRED = ctypes.POINTER(CREDENTIAL)
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        advapi32.CredWriteW.restype = wintypes.BOOL
        advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                        wintypes.DWORD, ctypes.POINTER(PCRED)]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree.restype = None
        advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                          wintypes.DWORD]
        advapi32.CredDeleteW.restype = wintypes.BOOL
    except (AttributeError, OSError):
        return None

    _CRED_STRUCTS = [advapi32.CredWriteW, advapi32.CredReadW,
                     advapi32.CredFree, advapi32.CredDeleteW, CREDENTIAL, PCRED]
    return _CRED_STRUCTS


def cred_target(env_name: str) -> str:
    return f"{CRED_PREFIX}{env_name}"


def cred_available() -> bool:
    return _cred_api() is not None


def cred_write(env_name: str, secret: str) -> bool:
    """把 Key 存进 Windows 凭据管理器。失败返回 False（不抛异常）。"""
    api = _cred_api()
    if not api:
        return False
    write_w, _read_w, _free, _del, CREDENTIAL, _pc = api
    import ctypes
    blob = secret.encode("utf-16-le")
    cred = CREDENTIAL()
    cred.Flags = 0
    cred.Type = 1                      # CRED_TYPE_GENERIC
    cred.TargetName = cred_target(env_name)
    cred.Comment = None
    cred.CredentialBlobSize = len(blob)
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = 2                   # CRED_PERSIST_LOCAL_MACHINE（当前用户、重启仍在）
    cred.AttributeCount = 0
    cred.Attributes = None
    cred.TargetAlias = None
    cred.UserName = "codex"
    return bool(write_w(ctypes.byref(cred), 0))


def cred_read(env_name: str) -> str | None:
    api = _cred_api()
    if not api:
        return None
    _write_w, read_w, free_w, _del, _CREDENTIAL, PCRED = api
    import ctypes
    # 文档说 Flags 必须为 0，但部分 Windows 版本只接受 1，两种都试一遍。
    for flags in (0, 1):
        pc = PCRED()
        if not read_w(cred_target(env_name), flags, 0, ctypes.byref(pc)):
            continue
        try:
            blob_size = int(pc.contents.CredentialBlobSize)
            if blob_size <= 0:
                return None
            raw = ctypes.string_at(pc.contents.CredentialBlob, blob_size)
            return raw.decode("utf-16-le") or None
        except (ValueError, UnicodeDecodeError, OSError):
            return None
        finally:
            try:
                if pc:
                    free_w(pc)
            except Exception:  # noqa: BLE001
                pass
    return None


def cred_delete(env_name: str) -> bool:
    api = _cred_api()
    if not api:
        return False
    _write_w, _read_w, _free, delete_w, _C, _P = api
    try:
        return bool(delete_w(cred_target(env_name), 1, 0))
    except OSError:
        return False


# ===========================================================================
# 3.6 结构化结果（供图形界面读取；命令行仍直接打印）
# ===========================================================================

# 每次操作后由命令函数填充，GUI 读取；命令行忽略。
LAST: dict = {}


class OpResult:
    """一次操作的完整结果：退出码 + 全部输出行 + 结构化数据。"""

    __slots__ = ("code", "lines", "data")

    def __init__(self, code: int, lines: list[str], data: dict) -> None:
        self.code = code
        self.lines = lines
        self.data = data

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class _Capture:
    """把 print 输出收起来给界面显示。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def write(self, s: str) -> int:
        self._lines.append(s)
        return len(s)

    def flush(self) -> None:  # 与 file 协议兼容
        return None

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"


def run_op(fn, *args, **kwargs) -> OpResult:
    """执行一个命令函数，捕获其输出并返回结构化结果。

    GUI 用它替代直接调用；命令行路径不经过这里（行为完全不变）。
    stdout 与 stderr 都收：只收 stdout 会把 warnings/异常提示静默吞掉。
    """
    LAST.clear()
    lines: list[str] = []
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = _Capture(lines)
    sys.stderr = _Capture(lines)
    try:
        code = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        # 异常只压成 exit code 会让界面无从排查，把 traceback 一起带出去
        lines.append(f"[异常] {type(exc).__name__}: {exc}")
        lines.extend(traceback.format_exc().rstrip().splitlines())
        code = 1
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    if not isinstance(code, int):
        code = 0
    return OpResult(code, [ln for ln in "".join(lines).splitlines()], dict(LAST))


# ===========================================================================
# 4. config.toml 结构化编辑（MCP / profiles 等用户配置原样保留）
# ===========================================================================

HEADER_RE = re.compile(r"^\s*(\[{1,2})([^\]\[]+?)(\]{1,2})\s*(#.*)?$")
ASSIGN_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*=")
CHAT_WIRE_RE = re.compile(r"""^\s*wire_api\s*=\s*["']chat["']""")


def split_blocks(text: str) -> tuple[list[str], list[list[str]]]:
    pre: list[str] = []
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if HEADER_RE.match(line):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            pre.append(line)
    return pre, blocks


def header_id(header: str) -> str | None:
    m = HEADER_RE.match(header)
    if not m:
        return None
    m2 = re.match(r"^model_providers\.(.+)$", m.group(2).strip())
    return m2.group(1) if m2 else None


def is_managed_provider(header: str) -> bool:
    pid = header_id(header)
    return pid is not None and pid in set(PROVIDERS) | LEGACY_PROVIDER_IDS


def scrub_chat_wire_api(blocks: list[list[str]]) -> int:
    removed = 0
    for block in blocks:
        kept = []
        for line in block:
            if CHAT_WIRE_RE.match(line):
                removed += 1
                continue
            kept.append(line)
        block[:] = kept
    return removed


def strip_key(lines: list[str], key: str) -> int:
    pat = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    removed = 0
    kept = []
    for line in lines:
        if pat.match(line):
            removed += 1
            continue
        kept.append(line)
    lines[:] = kept
    return removed


def toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def toml_list(values: list[str]) -> str:
    return "[" + ", ".join(toml_str(v) for v in values) + "]"


def render_config(pre: list[str], blocks: list[list[str]],
                  provider_id: str, model_slug: str,
                  inline_token: str | None) -> str:
    prov = PROVIDERS[provider_id]

    managed = list(MANAGED_TOP_KEYS) + (list(EXTRA_TOP_KEYS) if OFFICIAL_EXTRAS else [])
    preamble: list[str] = []
    for line in pre:
        m = ASSIGN_RE.match(line)
        if m and m.group(1) in managed:
            continue
        preamble.append(line)
    while preamble and not preamble[-1].strip():
        preamble.pop()

    top = [f"model = {toml_str(model_slug)}",
           f"model_provider = {toml_str(provider_id)}",
           f"model_catalog_json = {toml_str('~/.codex/models.json')}"]
    if OFFICIAL_EXTRAS:
        if FORCE_API_AUTH:
            top += ['preferred_auth_method = "apikey"',
                    'forced_login_method = "api"']
        if prov.get("reasoning_effort") in VALID_TOP_EFFORTS:
            top.append(f"model_reasoning_effort = {toml_str(prov['reasoning_effort'])}")
        if prov.get("web_search"):
            top.append(f"web_search = {toml_str(prov['web_search'])}")
        if SHOW_RAW_REASONING:
            top.append("show_raw_agent_reasoning = true")

    out_blocks: list[list[str]] = []
    desktop: list[str] | None = None

    for block in blocks:
        header = block[0].strip()
        if is_managed_provider(header):
            continue
        if header == "[desktop]":
            if desktop is None:
                desktop = ["[desktop]"]
                out_blocks.append(desktop)
            desktop.extend(block[1:])
            continue
        out_blocks.append(block)

    if desktop is None:
        desktop = ["[desktop]"]
        out_blocks.append(desktop)
    strip_key(desktop, DESKTOP_EFFORTS_KEY)
    desktop.insert(1, f"{DESKTOP_EFFORTS_KEY} = {toml_list(prov['efforts'])}")

    block = [f"[model_providers.{provider_id}]",
             f"name = {toml_str(prov['name'])}",
             f"base_url = {toml_str(prov['base_url'])}",
             'wire_api = "responses"']
    if inline_token:
        block.append(f"experimental_bearer_token = {toml_str(inline_token)}")
    else:
        block.append(f"env_key = {toml_str(prov['env_key'])}")
    out_blocks.append(block)

    lines = preamble + top
    for blk in out_blocks:
        lines.append("")
        lines.extend(blk)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines).rstrip() + "\n")


# ===========================================================================
# 5. models.json 生成与合并
# ===========================================================================

def build_model_entry(m: dict) -> dict:
    entry = {
        "slug": m["slug"],
        "display_name": m.get("name", m["slug"]),
        "description": m.get("desc", ""),
        "default_reasoning_level": m.get("default", "high"),
        "supported_reasoning_levels": [
            {"effort": e, "description": EFFORT_DESC.get(e, e)}
            for e in m.get("efforts", [])
        ],
        "shell_type": m.get("shell", "shell_command"),
        "visibility": "list",
        "supported_in_api": True,
        "priority": m.get("priority", 0),
        "base_instructions": "",
        "supports_reasoning_summaries": m.get("summaries", True),
        "default_reasoning_summary": "none",
        "support_verbosity": False,
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "context_window": m["ctx"],
        "max_context_window": m["ctx"],
        "effective_context_window_percent": 95,
        "supports_parallel_tool_calls": True,
        "experimental_supported_tools": [],
        "input_modalities": ["text", "image"] if m.get("image") else ["text"],
    }
    if m.get("patch"):
        entry["apply_patch_tool_type"] = m["patch"]
    return entry


def merge_catalog(refresh: bool = False, extra: list[dict] | None = None
                  ) -> tuple[dict, int]:
    """已存在的条目（尤其 DeepSeek 官方写入的完整元数据）默认原样保留。"""
    existing: dict = {"models": []}
    if CATALOG_PATH.exists():
        try:
            existing = json.loads(CATALOG_PATH.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"[警告] models.json 解析失败，将重建：{exc}")
            existing = {"models": []}

    seen: dict[str, dict] = {}
    order: list[str] = []
    for m in existing.get("models", []):
        slug = m.get("slug")
        if slug and slug != "REPLACE_WITH_ENDPOINT_ID" and slug not in seen:
            seen[slug] = m
            order.append(slug)

    added = 0
    for prov in list(PROVIDERS.values()) + ([{"models": extra}] if extra else []):
        for m in prov["models"]:
            slug = m["slug"]
            if slug == "REPLACE_WITH_ENDPOINT_ID":
                continue  # 占位符不进模型选择器
            if slug in seen and not refresh:
                continue
            if slug not in seen:
                added += 1
                order.append(slug)
            seen[slug] = build_model_entry(m)

    return {"models": [seen[s] for s in order]}, added


# ===========================================================================
# 6. 写入流程
# ===========================================================================

def atomic_write(path: Path, text: str) -> None:
    """原子写单个文件：临时文件 -> flush+fsync -> os.replace。

    直接 Path.write_text 在写到一半时崩溃会留下半截文件，
    Codex 下次启动整个配置加载失败。
    """
    atomic_write_many([(path, text)])


def atomic_write_many(pairs: list[tuple[Path, str]]) -> None:
    """原子写多个文件；任一步失败，已替换的文件全部回滚。

    config.toml 与 models.json 必须同时生效，只写成功一个会指向
    旧目录或半新半旧的配置。
    """
    staged: list[tuple[Path, Path]] = []
    old: dict[Path, bytes | None] = {}
    done: list[Path] = []
    try:
        for path, text in pairs:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
            except BaseException:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            staged.append((path, tmp))
            old[path] = path.read_bytes() if path.exists() else None

        for path, tmp in staged:
            os.replace(tmp, path)
            done.append(path)
    except BaseException:
        for path in done:  # 先装的往回退
            data = old.get(path)
            if data is None:
                try:
                    path.unlink()
                except OSError:
                    pass
            else:
                try:
                    path.write_bytes(data)
                except OSError:
                    pass
        for _path, tmp in staged:  # 没被替换掉的临时文件一并清掉
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def backup_config() -> Path | None:
    if not CONFIG_PATH.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest_dir = BACKUP_DIR / stamp
    seq = 1
    while dest_dir.exists():  # 同一秒内多次切换时不能互相覆盖
        seq += 1
        dest_dir = BACKUP_DIR / f"{stamp}-{seq}"
    dest = dest_dir / "config.toml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CONFIG_PATH, dest)
    if CATALOG_PATH.exists():
        shutil.copy2(CATALOG_PATH, dest.parent / "models.json")
    backups = [b for b in sorted(BACKUP_DIR.glob("*/config.toml"))
               if b.parent != ORIGINAL_DIR]
    for old in backups[:-10]:
        shutil.rmtree(old.parent, ignore_errors=True)
    return dest


def snapshot_original() -> bool:
    """首次运行时把 config.toml / models.json 原样封存，供 switch original 还原。

    只创建一次，之后永不覆盖、永不被轮转删除。
    """
    if (ORIGINAL_DIR / "manifest.json").exists():
        return False
    ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config_existed": CONFIG_PATH.exists(),
        "models_existed": CATALOG_PATH.exists(),
    }
    if CONFIG_PATH.exists():
        shutil.copy2(CONFIG_PATH, ORIGINAL_DIR / "config.toml")
    if CATALOG_PATH.exists():
        shutil.copy2(CATALOG_PATH, ORIGINAL_DIR / "models.json")
    atomic_write(ORIGINAL_DIR / "manifest.json",
                 json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return True


def restore_original() -> int:
    manifest_path = ORIGINAL_DIR / "manifest.json"
    if not manifest_path.exists():
        print(f"[错误] 没有找到原始快照：{ORIGINAL_DIR}")
        print("       本脚本只在首次运行时封存原配置。")
        LAST.update(op="original", error="没有原始快照")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))

    cfg_src = ORIGINAL_DIR / "config.toml"
    cat_src = ORIGINAL_DIR / "models.json"
    want_cfg = bool(manifest.get("config_existed")) and cfg_src.exists()
    want_cat = bool(manifest.get("models_existed")) and cat_src.exists()
    snap_cfg = snap_cat = ""

    # 先读快照、先校验，全部通过才动当前配置 —— 坏快照绝不覆盖可用文件。
    if want_cfg:
        snap_cfg = cfg_src.read_text(encoding="utf-8-sig")
        err = validate(snap_cfg, "toml")
        if err:
            print(f"[错误] 原始快照的 config.toml 不合法，已放弃还原：{err}")
            print(f"       快照文件：{cfg_src}")
            print("       当前配置没有被改动。")
            LAST.update(op="original", error=f"快照 config.toml 不合法: {err}")
            return 1
    if want_cat:
        snap_cat = cat_src.read_text(encoding="utf-8-sig")
        err = validate(snap_cat, "json")
        if err:
            print(f"[错误] 原始快照的 models.json 不合法，已放弃还原：{err}")
            print(f"       快照文件：{cat_src}")
            print("       当前配置没有被改动。")
            LAST.update(op="original", error=f"快照 models.json 不合法: {err}")
            return 1

    backup_config()  # 校验通过后再把当前状态存档，避免误操作无法回退

    if want_cfg:
        atomic_write(CONFIG_PATH, snap_cfg)
        print(f"[OK] 已还原原始 config.toml  <- {cfg_src}")
    elif CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
        print("[OK] 原本没有 config.toml，已删除脚本生成的文件")

    if want_cat:
        atomic_write(CATALOG_PATH, snap_cat)
        print(f"[OK] 已还原原始 models.json  <- {cat_src}")
    elif CATALOG_PATH.exists():
        CATALOG_PATH.unlink()
        print("[OK] 原本没有 models.json，已删除脚本生成的文件")

    print(f"\n快照时间：{manifest.get('saved_at', '?')}")
    print("快照的含义：本工具第一次运行那一刻的文件状态。")
    print("  如果你此前用过第三方接入脚本（例如官方 DeepSeek 一键配置），")
    print("  那封存下来的也是那份状态，而不是 Codex 出厂的 OpenAI 配置。")
    print("  想回到真正的出厂状态，请改用 `switch native`。")
    print("下一步：完全退出 Codex 后重新打开即可回到切换前的状态。")
    print("注意：本操作只还原文件，不会改动你的环境变量。")
    LAST.update(op="original", restored=True, saved_at=manifest.get("saved_at"),
                config_restored=want_cfg, models_restored=want_cat)
    return 0


def validate(text: str, kind: str) -> str | None:
    try:
        if kind == "toml":
            if tomllib is None:
                return None
            tomllib.loads(text)
        else:
            json.loads(text)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


def sync_env(dry_run: bool) -> bool:
    """把所有已填 Key 的提供商写进用户环境变量（Key 已就位才写 provider 段）。

    返回 False 表示至少有一次写入失败，调用方必须中止切换。
    """
    for pid in PROVIDERS:
        val = resolve_key(pid)
        if not val:
            continue
        env_name = PROVIDERS[pid]["env_key"]
        if read_user_env(env_name) != val:
            if dry_run:
                print(f"[env] {env_name} = {mask(val)} (dry-run，未写入)")
                continue
            if not write_user_env(env_name, val):
                print(f"[错误] {env_name} 写入失败，已中止切换（配置未改动）。")
                return False
            print(f"[env] 已写入 {env_name} = {mask(val)}")
    return True


def render_native(pre: list[str], blocks: list[list[str]]) -> str:
    """切回 Codex 原生状态：只撤销本脚本接管的字段，
    让 Codex 回到内置 provider 与默认模型，其余配置一律不动。"""
    preamble: list[str] = []
    for line in pre:
        m = ASSIGN_RE.match(line)
        if m and m.group(1) in MANAGED_TOP_KEYS:
            continue
        preamble.append(line)
    while preamble and not preamble[-1].strip():
        preamble.pop()

    out_blocks: list[list[str]] = []
    for block in blocks:
        header = block[0].strip()
        if is_managed_provider(header):
            continue
        if header == "[desktop]":
            strip_key(block, DESKTOP_EFFORTS_KEY)
        out_blocks.append(block)

    lines = list(preamble)
    for blk in out_blocks:
        lines.append("")
        lines.extend(blk)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines).rstrip() + "\n")


def apply(pid: str, model_slug: str | None, inline: bool,
          dry_run: bool, refresh_catalog: bool) -> int:
    if pid == "native":
        raw = CONFIG_PATH.read_text(encoding="utf-8-sig") \
            if CONFIG_PATH.exists() else ""
        pre, blocks = split_blocks(raw)
        scrubbed = scrub_chat_wire_api(blocks)
        text = render_native(pre, blocks)
        err = validate(text, "toml")
        if err:
            print(f"[错误] 生成的 config.toml 不合法，已放弃写入：{err}")
            LAST.update(op="switch", provider="native", error=err)
            return 1
        if dry_run:
            shown = redact(text)
            print("\n----- 与当前配置的差异 (dry-run) -----")
            print(build_diff(raw, shown) or "（没有差异）")
            print("\n----- config.toml (dry-run) -----\n" + shown)
            LAST.update(op="switch", provider="native", dry_run=True,
                        preview=shown, diff=build_diff(raw, shown),
                        scrubbed=scrubbed)
            return 0
        first_run = snapshot_original()
        backup = backup_config()
        CODEX_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write(CONFIG_PATH, text)
        if first_run:
            print(f"[OK] 已封存原始配置快照 -> {ORIGINAL_DIR}")
        if scrubbed:
            print(f'[OK] 已清除 {scrubbed} 处 wire_api = "chat"')
        print("[OK] 已切回 Codex 原生状态（内置 provider / 默认模型）")
        print(f"[OK] config.toml = {CONFIG_PATH}")
        print("     models.json 保留但不再被引用（model_catalog_json 已移除）")
        if backup:
            print(f"[OK] 切换前的备份 = {backup}")
        print("\n下一步：完全退出 Codex 后重新打开。")
        LAST.update(op="switch", provider="native", model=None, dry_run=False,
                    snapshot=first_run, scrubbed=scrubbed,
                    backup=str(backup) if backup else "",
                    config=str(CONFIG_PATH))
        return 0

    if pid not in PROVIDERS:
        print(f"[错误] 未知提供商：{pid}")
        print("       可选：native, " + ", ".join(PROVIDERS))
        LAST.update(op="switch", provider=pid, error="未知提供商")
        return 1

    model = model_slug or PROVIDERS[pid]["default_model"]
    if model == "REPLACE_WITH_ENDPOINT_ID":
        print("[错误] volcengine 需要真实的推理接入点 ID（ep-xxx）。")
        print("       请编辑脚本 PROVIDERS['volcengine']['default_model'] 后重试。")
        LAST.update(op="switch", provider=pid, error="需要 ep-xxx")
        return 1
    if not SLUG_RE.match(model):
        # 模型 slug 是被拼进 config.toml 的字符串，必须先过白名单，
        # 不能只靠"生成后再校验"兜底。
        print(f"[错误] 非法的模型 slug：{model!r}")
        print("       只允许字母、数字和 . _ : - ，长度 1-120，且不能以符号开头。")
        LAST.update(op="switch", provider=pid, error="非法的模型 slug")
        return 1

    key = resolve_key(pid)
    if not key:
        env_name = PROVIDERS[pid]["env_key"]
        print(f"[{pid}] 还没有 API Key。获取地址：{PROVIDERS[pid]['doc']}")
        try:
            key = input(f"       粘贴 {env_name} 的值（回车取消）：").strip()
        except EOFError:
            key = ""
        if key:
            # 粘贴的 Key 必须落到持久化存储，否则换个进程就丢了
            if not write_user_env(env_name, key):
                LAST.update(op="switch", provider=pid, error="环境变量写入失败")
                return 1
            if cred_available() and cred_write(env_name, key):
                print(f"[OK] 已保存到 Windows 凭据管理器：{cred_target(env_name)}")
            print(f"[OK] 已保存 {env_name} = {mask(key)}")
        else:
            print(f"[错误] 没有 Key，无法切换。可先：python codex_cn.py setkey {pid} sk-xxx")
            LAST.update(op="switch", provider=pid, error="缺少 API Key",
                        need_key=env_name, doc=PROVIDERS[pid]["doc"])
            return 1

    sync_env_ok = sync_env(dry_run)
    if not sync_env_ok:
        LAST.update(op="switch", provider=pid, error="环境变量写入失败")
        return 1

    extra: list[dict] = []
    if pid == "volcengine":
        extra = [{"slug": model, "name": model,
                  "desc": f"Volcengine endpoint {model}",
                  "ctx": 131072, "image": False,
                  "efforts": PROVIDERS[pid]["efforts"],
                  "default": "high", "priority": 0, "summaries": False}]
    catalog, added = merge_catalog(refresh_catalog, extra)
    catalog_text = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    err = validate(catalog_text, "json")
    if err:
        print(f"[错误] 生成的 models.json 不合法：{err}")
        LAST.update(op="switch", provider=pid, error=f"models.json 不合法: {err}")
        return 1

    raw = CONFIG_PATH.read_text(encoding="utf-8-sig") if CONFIG_PATH.exists() else ""
    pre, blocks = split_blocks(raw)
    scrubbed = scrub_chat_wire_api(blocks)

    inline_token = key if (inline or AUTH_MODE == "inline") else None
    text = render_config(pre, blocks, pid, model, inline_token)
    err = validate(text, "toml")
    if err:
        print("[错误] 生成的 config.toml 不合法，已放弃写入：")
        print("       " + err)
        LAST.update(op="switch", provider=pid, error=f"config.toml 不合法: {err}")
        return 1

    shown = redact(text)
    diff = build_diff(raw, shown)
    risk = ""
    if inline_token:
        risk = ("inline 模式会把 API Key 明文写进 config.toml；"
                "该文件还会被历史备份与原始快照各复制一份。")
        if not dry_run:
            print("[提醒] " + risk)
            print("       如非必要请改用默认的环境变量模式（config 里只留变量名）。")

    if dry_run:
        print("\n----- 与当前配置的差异 (dry-run) -----")
        print(diff or "（没有差异）")
        print("\n----- config.toml (dry-run) -----")
        print(shown)
        print("----- models.json (前 30 行) -----")
        print(redact("\n".join(catalog_text.splitlines()[:30])))
        if risk:
            print("\n[提醒] " + risk)
        LAST.update(op="switch", provider=pid, model=model, dry_run=True,
                    preview=shown, catalog_preview=catalog_text, diff=diff,
                    inline=bool(inline_token), risk=risk,
                    scrubbed=scrubbed, added=added)
        return 0

    first_run = snapshot_original()
    backup = backup_config()
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_many([(CONFIG_PATH, text), (CATALOG_PATH, catalog_text)])
    if first_run:
        print(f"[OK] 已封存原始配置快照 -> {ORIGINAL_DIR}"
              f"（可用 `python codex_cn.py original` 一键还原）")

    auth = f"experimental_bearer_token" if inline_token \
        else f"环境变量 {PROVIDERS[pid]['env_key']}"
    print()
    print(f"[OK] 已切换到 {PROVIDERS[pid]['name']}   provider = {pid}")
    print(f"[OK] model       = {model}")
    print(f"[OK] base_url    = {PROVIDERS[pid]['base_url']}")
    print(f"[OK] 认证方式     = {auth}")
    print(f"[OK] config.toml = {CONFIG_PATH}")
    print(f"[OK] models.json = {CATALOG_PATH} (新增 {added} 条，共 {len(catalog['models'])} 条)")
    if scrubbed:
        print(f'[OK] 已清除 {scrubbed} 处 wire_api = "chat"（新版 Codex 会拒绝加载整个配置）')
    if backup:
        print(f"[OK] 备份        = {backup}")
    print()
    print("下一步：完全退出 Codex 桌面端后重新打开，或新开一个终端执行 codex。")
    if not inline_token:
        print("提示：环境变量对已运行的 GUI 进程不生效，必须重启 Codex。")
    LAST.update(op="switch", provider=pid, model=model, dry_run=False,
                base_url=PROVIDERS[pid]["base_url"],
                auth=auth, config=str(CONFIG_PATH), catalog=str(CATALOG_PATH),
                added=added, total=len(catalog["models"]),
                scrubbed=scrubbed, snapshot=first_run,
                inline=bool(inline_token), risk=risk, diff=diff,
                backup=str(backup) if backup else "",
                env_key=PROVIDERS[pid]["env_key"], masked=mask(key))
    return 0


# ===========================================================================
# 7. 子命令
# ===========================================================================

def cmd_list(_: argparse.Namespace) -> int:
    print(f"Codex 配置目录: {CODEX_DIR}\n")
    print(f"{'id':<13}{'模型':<34}{'env_key':<20}{'Key 状态':<20}")
    print("-" * 87)
    for pid, p in PROVIDERS.items():
        models = "/".join(m["slug"] for m in p["models"][:3])
        print(f"{pid:<13}{models:<34}{p['env_key']:<20}{mask(resolve_key(pid)):<20}")
    print()
    if CONFIG_PATH.exists() and tomllib:
        try:
            cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            print(f"当前 provider = {cfg.get('model_provider', '?')}"
                  f"   model = {cfg.get('model', '?')}")
            current = {"provider": cfg.get("model_provider"),
                       "model": cfg.get("model")}
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] config.toml 解析失败：{exc}")
            current = {"provider": None, "model": None}
    else:
        print("尚未生成 config.toml")
        current = {"provider": None, "model": None}
    print("\n额外可用：")
    print("  native  切回 Codex 原生 provider / 默认模型（内置 GPT）")
    print(f"  original 一键还原首次运行前的快照"
          f"（{'已封存' if (ORIGINAL_DIR / 'manifest.json').exists() else '尚未封存'}）")
    print("           快照 = 本工具第一次运行时的文件状态，不等于 Codex 出厂配置；")
    print("           若此前用过第三方接入脚本，还原回来的也是那份状态。")
    print("  testkey 测试端点连通性与 Key 是否有效（只发一个只读 GET 请求）")
    print("\n以下服务商仅支持 Chat Completions，Codex 无法直连：")
    for name, why in UNSUPPORTED.items():
        print(f"  - {name}: {why}")
    rows = [{"id": pid, "name": p["name"],
             "models": [m["slug"] for m in p["models"]],
             "env_key": p["env_key"], "masked": mask(resolve_key(pid))}
            for pid, p in PROVIDERS.items()]
    LAST.update(op="list", rows=rows, current=current, unsupported=dict(UNSUPPORTED))
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    return apply(args.provider, args.model, args.inline,
                 args.dry_run, args.refresh_catalog)


def cmd_setkey(args: argparse.Namespace) -> int:
    if args.provider not in PROVIDERS:
        print(f"[错误] 未知提供商：{args.provider}")
        LAST.update(op="setkey", provider=args.provider, error="未知提供商")
        return 1
    value = (args.key or "").strip()
    env_name = PROVIDERS[args.provider]["env_key"]
    if not value or is_placeholder(value):
        print(f"[错误] 这看起来不是有效的 API Key：{mask(value)}")
        print("       获取地址：" + PROVIDERS[args.provider]["doc"])
        LAST.update(op="setkey", provider=args.provider, env_key=env_name,
                    error="Key 为空或像占位符")
        return 1

    if args.dry_run:
        print(f"[OK] (dry-run) {env_name} = {mask(value)}  —— 没有写入任何存储")
        LAST.update(op="setkey", provider=args.provider, env_key=env_name,
                    masked=mask(value), dry_run=True)
        return 0

    # 环境变量是 Codex 真正读的东西，它写失败就一切免谈
    if not write_user_env(env_name, value):
        print("       Key 没有保存。请检查是否被系统策略限制了用户环境变量。")
        LAST.update(op="setkey", provider=args.provider, env_key=env_name,
                    error="环境变量写入失败")
        return 1
    if cred_available():
        if cred_write(env_name, value):
            print(f"[OK] 已存入 Windows 凭据管理器：{cred_target(env_name)}")
        else:
            print("[警告] 凭据管理器写入失败，Key 仅保存在环境变量")
    print(f"[OK] {env_name} = {mask(value)}")
    print(f"     接着运行：python codex_cn.py switch {args.provider}")
    LAST.update(op="setkey", provider=args.provider, env_key=env_name,
                masked=mask(value), dry_run=False)
    return 0


def cmd_show(_: argparse.Namespace) -> int:
    if not CONFIG_PATH.exists():
        print("config.toml 不存在。")
        LAST.update(op="show", error="config.toml 不存在")
        return 1
    # 展示出来的内容可能进日志 / 屏幕录像 / 远程协助，先脱敏再输出
    print(redact(CONFIG_PATH.read_text(encoding="utf-8-sig")), end="")
    LAST.update(op="show")
    return 0


def cmd_testkey(args: argparse.Namespace) -> int:
    """GET {base_url}/models 探测端点与 Key，全程不打印 Key。

    结论口径：
      2xx            -> Key 可用
      401/403        -> Key 明确无效（唯一算失败的情况）
      404 / 5xx      -> 端点不支持该接口或服务端异常，不下结论
      连不上/超时    -> 网络问题，不下结论（离线同事不应被判失败）
    """
    pid = getattr(args, "provider", None)
    if pid not in PROVIDERS:
        print(f"[错误] 未知提供商：{pid}")
        LAST.update(op="testkey", provider=pid, error="未知提供商")
        return 1
    prov = PROVIDERS[pid]
    key = resolve_key(pid)
    if not key:
        print(f"[错误] {pid} 还没有 API Key。")
        print(f"       先执行：python codex_cn.py setkey {pid} sk-xxx")
        LAST.update(op="testkey", provider=pid, error="缺少 API Key")
        return 1

    url = prov["base_url"].rstrip("/") + "/models"
    timeout = float(getattr(args, "timeout", None) or 8)
    req = urllib.request.Request(
        url, method="GET",
        headers={"Authorization": f"Bearer {key}",
                 "Accept": "application/json",
                 "User-Agent": "codex_cn/1.0"})

    print(f"[测试] GET {url}（超时 {timeout:g}s）")
    started = time.time()
    status = 0
    detail = ""
    network_error = False
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(resp.getcode() or 0)
            resp.read(4096)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        detail = str(getattr(exc, "reason", "") or "")
        try:
            exc.read(4096)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001  DNS/超时/代理/防火墙都归这里
        network_error = True
        detail = f"{type(exc).__name__}: {exc}"
    ms = int((time.time() - started) * 1000)

    if network_error:
        print(f"[提示] 连不上（{ms}ms）：{detail}")
        print("       这通常是网络不通、代理或防火墙问题，不能据此判断 Key 是否有效。")
        LAST.update(op="testkey", provider=pid, ok=None, network_error=True,
                    status=0, ms=ms, detail=detail, url=url)
        return 0
    if 200 <= status < 300:
        print(f"[OK] 端点可达，Key 被接受（HTTP {status}，{ms}ms）")
        LAST.update(op="testkey", provider=pid, ok=True, status=status,
                    ms=ms, url=url)
        return 0
    if status in (401, 403):
        print(f"[FAIL] Key 被拒绝（HTTP {status}"
              f"{' ' + detail if detail else ''}，{ms}ms）")
        print("       Key 无效、已过期或权限不足。请重新获取后 setkey。")
        LAST.update(op="testkey", provider=pid, ok=False, status=status,
                    ms=ms, detail=detail, url=url)
        return 1

    print(f"[提示] 端点返回 HTTP {status}"
          f"{' ' + detail if detail else ''}（{ms}ms），无法据此判断 Key 是否有效。")
    if status == 404:
        print("       该服务商可能没有 GET /models 接口，但域名与网络是通的。")
    LAST.update(op="testkey", provider=pid, ok=None, status=status,
                ms=ms, detail=detail, url=url)
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    ok = True
    checks: list = []
    neutral = 0
    HINT = "尚未接入：在“切换模型”页点“应用切换”后自动生成"

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  [{'OK  ' if good else 'FAIL'}] {label}"
              + (f" — {detail}" if detail and not good else ""))
        checks.append({"label": label, "ok": bool(good), "skip": False,
                       "detail": detail})
        if not good:
            ok = False

    def info(label: str, detail: str = "") -> None:
        """尚未接入时的中性提示：既不算通过也不算失败，不许标红。"""
        nonlocal neutral
        neutral += 1
        print(f"  [....] {label}" + (f" — {detail}" if detail else ""))
        checks.append({"label": label, "ok": True, "skip": True,
                       "detail": detail})

    print("Codex 接入自检\n")
    if CODEX_DIR.is_dir():
        check("~/.codex 目录存在", True)
    else:
        info("~/.codex 目录不存在", HINT + "（目录会自动创建）")

    cfg_text = ""
    cfg: dict = {}
    toml_ok = False
    if CONFIG_PATH.exists():
        cfg_text = CONFIG_PATH.read_text(encoding="utf-8-sig")
        err = validate(cfg_text, "toml")
        check("config.toml 是合法 TOML", err is None, err or "")
        if not err and tomllib:
            cfg = tomllib.loads(cfg_text)
            toml_ok = True
    else:
        info("config.toml 不存在", HINT)

    pid = cfg.get("model_provider")
    mine = pid in PROVIDERS

    if toml_ok:
        check('没有 wire_api = "chat"', '"chat"' not in cfg_text,
              "新版 Codex 会拒绝加载整个配置")

    catalog_slugs: set = set()
    if CATALOG_PATH.exists():
        try:
            cat = json.loads(CATALOG_PATH.read_text(encoding="utf-8-sig"))
            catalog_slugs = {m.get("slug") for m in cat.get("models", [])}
            check("models.json 是合法 JSON", True, f"{len(catalog_slugs)} 个模型")
            if mine:
                check("model_catalog_json 指向该文件",
                      str(cfg.get("model_catalog_json", "")).endswith(
                          "models.json"))
        except Exception as exc:  # noqa: BLE001
            check("models.json 是合法 JSON", False, str(exc))
    else:
        info("models.json 不存在", HINT)

    if mine:
        check(f"model_provider = {pid}（本工具已接管）", True)
        table = cfg.get("model_providers", {}).get(pid, {})
        check(f"[model_providers.{pid}] 存在", bool(table))
        if table:
            check("wire_api = responses", table.get("wire_api") == "responses",
                  str(table.get("wire_api")))
            check("base_url 正确", table.get("base_url") == PROVIDERS[pid]["base_url"],
                  str(table.get("base_url")))
            ek = str(table.get("env_key", ""))
            bt = str(table.get("experimental_bearer_token", ""))
            check("env_key 是变量名而不是 Key", not ek.startswith("sk-"),
                  "你把 Key 填进了 env_key，应填变量名")
            check("bearer_token 不是环境变量名",
                  not (bt.endswith("_KEY") or bt.isupper()),
                  "你把变量名填进了 experimental_bearer_token，应填真实 Key")
            if ek:
                # 只认持久化的注册表值：读 os.environ 会把"本进程刚设过"
                # 也算成功，而新拉起的 Codex 进程根本看不到它。
                persistent = read_user_env(ek)
                in_proc = os.environ.get(ek)
                if persistent and not is_placeholder(persistent):
                    check(f"环境变量 {ek} 已持久化", True, "")
                elif in_proc and not is_placeholder(in_proc):
                    check(f"环境变量 {ek} 已持久化", False,
                          "只有当前进程有值，Codex 新进程读不到（写入没有持久化）")
                else:
                    check(f"环境变量 {ek} 已持久化", False,
                          f"未设置；先执行 python codex_cn.py setkey {pid} sk-xxx")
            if bt:
                check("配置文件里的 token 已填写", not is_placeholder(bt), mask(bt))
        check("model 在模型目录里", cfg.get("model") in catalog_slugs,
              str(cfg.get("model")))
    elif CONFIG_PATH.exists() and pid:
        info(f"当前 provider = {pid}（未接管国内模型）", HINT)
    elif CONFIG_PATH.exists():
        info("model_provider 未设置", HINT)
    # config.toml 都不存在时不再重复列 provider 一项，上一条已经说明了。

    if not ok:
        tail = "存在失败项，按上面提示修复。"
    elif neutral:
        tail = (f"尚未接入本工具（{neutral} 项提示，不算失败）。\n"
                "      在“切换模型”页点“应用切换”，"
                "或执行 python codex_cn.py switch deepseek")
    else:
        tail = "全部通过。"
    print("\n" + tail)
    LAST.update(op="doctor", ok=ok, checks=checks,
                provider=cfg.get("model_provider"),
                model=cfg.get("model"),
                passed=sum(1 for c in checks if c["ok"] and not c["skip"]),
                total=sum(1 for c in checks if not c["skip"]),
                neutral=neutral)
    return 0 if ok else 1


def cmd_restore(args: argparse.Namespace) -> int:
    if not BACKUP_DIR.exists():
        print("没有备份。")
        LAST.update(op="restore", backups=[], error="没有备份")
        return 1
    backups = sorted(BACKUP_DIR.glob("*/config.toml"), reverse=True)
    backups = [b for b in backups if b.parent != ORIGINAL_DIR]
    if not backups:
        print("没有备份。")
        LAST.update(op="restore", backups=[], error="没有备份")
        return 1
    names = [b.parent.name for b in backups[:10]]
    for i, b in enumerate(backups[:10]):
        print(f"  [{i}] {b.parent.name}")
    idx = args.index
    if idx is None:
        try:
            raw = input("要还原的序号（回车取消）：").strip()
            idx = int(raw) if raw else None
        except (ValueError, EOFError):
            idx = None
        if idx is None:
            print("已取消")
            LAST.update(op="restore", backups=names, cancelled=True)
            return 0
    if idx < 0 or idx >= len(backups):
        print("[错误] 序号超出范围")
        LAST.update(op="restore", backups=names, error="序号超出范围")
        return 1
    src_cfg = backups[idx]
    cfg_text = src_cfg.read_text(encoding="utf-8-sig")
    err = validate(cfg_text, "toml")
    if err:
        print(f"[错误] 这份备份的 config.toml 不合法，已拒绝还原：{err}")
        print(f"       备份文件：{src_cfg}")
        print("       当前配置没有被改动。")
        LAST.update(op="restore", backups=names, error=f"备份不合法: {err}")
        return 1
    saved_catalog = src_cfg.parent / "models.json"
    cat_text = ""
    if saved_catalog.exists():
        cat_text = saved_catalog.read_text(encoding="utf-8-sig")
        err = validate(cat_text, "json")
        if err:
            print(f"[错误] 这份备份的 models.json 不合法，已拒绝还原：{err}")
            print(f"       备份文件：{saved_catalog}")
            print("       当前配置没有被改动。")
            LAST.update(op="restore", backups=names, error=f"备份 models.json 不合法: {err}")
            return 1

    if CONFIG_PATH.exists():
        backup_config()  # 校验通过后再存档当前状态

    pairs = [(CONFIG_PATH, cfg_text)]
    if cat_text:
        pairs.append((CATALOG_PATH, cat_text))
    atomic_write_many(pairs)
    print(f"[OK] 已还原 {src_cfg} -> {CONFIG_PATH}")
    if cat_text:
        print(f"[OK] 已还原 {saved_catalog} -> {CATALOG_PATH}")
    LAST.update(op="restore", backups=names, restored=src_cfg.parent.name,
                catalog_restored=bool(cat_text))
    return 0


def cmd_original(_: argparse.Namespace) -> int:
    return restore_original()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 Codex 切换到国内大模型（DeepSeek / Qwen / GLM / Kimi / 火山方舟）")
    parser.add_argument("--inline", action="store_true",
                        help="把 API Key 明文写进 config.toml 而不是环境变量")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出所有提供商").set_defaults(func=cmd_list)

    p = sub.add_parser("switch", help="切换提供商（native = 切回 Codex 原生）")
    p.add_argument("provider", help="提供商 id：native / deepseek / dashscope / zai / kimi / volcengine")
    p.add_argument("--model", help="模型 slug，默认用该提供商的推荐模型")
    p.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    p.add_argument("--refresh-catalog", action="store_true",
                   help="用脚本内置数据覆盖 models.json 中的同名条目")
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("setkey", help="设置某个提供商的 API Key")
    p.add_argument("provider")
    p.add_argument("key")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_setkey)

    sub.add_parser("show", help="打印当前 config.toml（已脱敏）").set_defaults(func=cmd_show)
    sub.add_parser("doctor", help="自检配置").set_defaults(func=cmd_doctor)

    p = sub.add_parser("testkey", help="测试端点连通性与 Key 是否有效")
    p.add_argument("provider")
    p.add_argument("--timeout", type=float, default=8, help="超时秒数，默认 8")
    p.set_defaults(func=cmd_testkey)
    sub.add_parser("original",
                   help="还原到本脚本首次运行前的原始状态（config.toml + models.json）"
                   ).set_defaults(func=cmd_original)

    p = sub.add_parser("restore", help="还原备份的 config.toml")
    p.add_argument("index", nargs="?", type=int, default=None)
    p.set_defaults(func=cmd_restore)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
