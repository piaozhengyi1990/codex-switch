#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codex_cn.py 自动化测试。

运行：python -m unittest -v test_codex_cn
     或 python test_codex_cn.py

覆盖需求：
  R1 接入 DeepSeek 等国内模型，改 Key 即可切换
  R2 不修改 Codex 原生功能，只替换模型；可一键切回原样
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# 必须先设置 CODEX_HOME 再 import，避免误伤真实 ~/.codex
os.environ["CODEX_HOME"] = str(Path(tempfile.mkdtemp(prefix="codex_cn_boot_")))

import codex_cn as cc  # noqa: E402


# 一份尽可能贴近真实 Codex 原生配置的样本
NATIVE_CONFIG = """\
notify = [ "C:\\\\path\\\\to\\\\codex-computer-use.exe", "turn-ended" ]
model = "gpt-5.2"
model_provider = "openai"
model_reasoning_effort = "high"
web_search = "auto"
profile = "default"

[profiles.default]
model = "gpt-5.2"

[marketplaces.openai-primary-runtime]
source_type = "local"
source = '\\\\?\\C:\\x\\.cache\\plugins\\openai-primary-runtime'

[plugins."browser@openai-bundled"]
enabled = true

[desktop]
followUpQueueMode = "steer"
conversationDetailMode = "STEPS_COMMANDS"
ambient-suggestions-enabled = true

[mcp_servers.node_repl]
command = 'C:\\x\\node_repl.exe'
args = []
startup_timeout_sec = 120

[mcp_servers.node_repl.env]
NODE_REPL_NATIVE_PIPE_CONNECT_TIMEOUT_MS = "1000"
CODEX_HOME = 'C:\\x\\.codex'

[windows]
sandbox = "elevated"
"""

# 被"换模型"接管的顶层键
MANAGED = set(cc.MANAGED_TOP_KEYS)
# DeepSeek 官方脚本会写、但我们默认不碰的原生行为键
NATIVE_BEHAVIOR = {"model_reasoning_effort", "web_search",
                   "show_raw_agent_reasoning",
                   "preferred_auth_method", "forced_login_method"}


def run(func, *args, **kwargs) -> tuple[int, str]:
    """执行命令函数，捕获 stdout，返回 (退出码, 输出)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            code = func(*args, **kwargs)
        except SystemExit as exc:  # argparse
            code = int(exc.code or 0)
    return code, buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="codex_cn_case_"))
        cc.set_codex_home(self.tmp)

        # ---- 隔离所有外部写入面：不碰真实注册表 / 凭据管理器 / 环境变量 ----
        self.secrets: dict[str, str] = {}      # 假的凭据管理器
        self.env_written: dict[str, str] = {}  # 假的"持久化"用户环境变量
        self._orig_cred = (cc.cred_read, cc.cred_write,
                           cc.cred_delete, cc.cred_available)
        self._orig_write_env = cc.write_user_env
        self._orig_read_env = cc.read_user_env
        self._orig_environ = dict(os.environ)

        cc.cred_read = lambda name: self.secrets.get(name)
        cc.cred_write = lambda name, secret: (
            self.secrets.__setitem__(name, secret) or True)
        cc.cred_delete = lambda name: self.secrets.pop(name, None) is not None
        cc.cred_available = lambda: True

        def fake_write_env(name: str, value: str) -> bool:
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
            self.env_written[name] = value
            return True

        cc.write_user_env = fake_write_env
        # 持久化读取指向测试内的字典：既能让 doctor 判"已持久化"，
        # 又绝不读写真实注册表。
        cc.read_user_env = lambda name: self.env_written.get(name)

        # 每个提供商都给测试 Key，避免触发交互式 input()
        for pid in cc.PROVIDERS:
            self.secrets[cc.PROVIDERS[pid]["env_key"]] = \
                f"sk-test-{pid}-0123456789"
        for p in cc.PROVIDERS.values():
            os.environ.pop(p["env_key"], None)

    def tearDown(self) -> None:
        (cc.cred_read, cc.cred_write,
         cc.cred_delete, cc.cred_available) = self._orig_cred
        cc.write_user_env = self._orig_write_env
        cc.read_user_env = self._orig_read_env
        os.environ.clear()
        os.environ.update(self._orig_environ)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def give_key(self, pid: str, value: str) -> None:
        """给某个提供商塞一个测试 Key（同时进假凭据管理器）。"""
        self.secrets[cc.PROVIDERS[pid]["env_key"]] = value

    def write_native(self) -> None:
        cc.CONFIG_PATH.write_text(NATIVE_CONFIG, encoding="utf-8")

    def read_config(self) -> str:
        return cc.CONFIG_PATH.read_text(encoding="utf-8-sig")

    def toml(self) -> dict:
        import tomllib
        return tomllib.loads(self.read_config())

    # ---------- R2：不修改 Codex 原生功能 ----------

    def test_r2_native_lines_preserved(self):
        """切换后，所有非接管的原生行必须逐行保留。"""
        self.write_native()
        code, _ = run(cc.apply, "deepseek", None, False, False, False)
        self.assertEqual(code, 0)

        original = [l for l in NATIVE_CONFIG.splitlines() if l.strip()]
        now = self.read_config()
        managed_lines = re.compile(
            r"^\s*(model|model_provider|model_catalog_json)\s*=")
        for line in original:
            if managed_lines.match(line):
                continue
            self.assertIn(line, now, f"原生配置行被改动/丢失：{line!r}")

    def test_r2_no_native_behavior_keys_written(self):
        """默认 OFFICIAL_EXTRAS=False，不得写入登录/联网/推理档位等原生行为键。"""
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        text = self.read_config()
        for key in ("preferred_auth_method", "forced_login_method",
                    "show_raw_agent_reasoning"):
            self.assertNotIn(key, text, f"不应写入 {key}")
        # 原本就有的 model_reasoning_effort / web_search 原样保留
        self.assertIn('model_reasoning_effort = "high"', text)
        self.assertIn('web_search = "auto"', text)

    def test_r2_nested_tables_and_profiles_preserved(self):
        """嵌套表、profiles、mcp env 子表必须完整保留。"""
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        cfg = self.toml()
        self.assertEqual(cfg["profiles"]["default"]["model"], "gpt-5.2")
        self.assertEqual(cfg["mcp_servers"]["node_repl"]["command"],
                         "C:\\x\\node_repl.exe")
        self.assertEqual(
            cfg["mcp_servers"]["node_repl"]["env"]["CODEX_HOME"], "C:\\x\\.codex")
        self.assertEqual(cfg["windows"]["sandbox"], "elevated")
        self.assertEqual(cfg["desktop"]["followUpQueueMode"], "steer")
        self.assertEqual(cfg["marketplaces"]["openai-primary-runtime"],
                         {"source_type": "local",
                          "source": "\\\\?\\C:\\x\\.cache\\plugins\\openai-primary-runtime"})

    def test_r2_only_model_fields_changed(self):
        """接管范围仅限 model / model_provider / model_catalog_json /
        [model_providers.*] / [desktop].enabled-reasoning-efforts。"""
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        cfg = self.toml()

        self.assertEqual(cfg["model"], "deepseek-flash")
        self.assertEqual(cfg["model_provider"], "deepseek")
        self.assertEqual(cfg["model_catalog_json"], "~/.codex/models.json")

        prov = cfg["model_providers"]["deepseek"]
        self.assertEqual(prov["wire_api"], "responses")
        self.assertEqual(prov["base_url"], "https://api.deepseek.com/")
        self.assertEqual(prov["env_key"], "DEEPSEEK_API_KEY")

        self.assertIn("enabled-reasoning-efforts", cfg["desktop"])
        self.assertEqual(cfg.get("profile"), "default")

    # ---------- R1：接入国内模型 ----------

    def test_r1_all_providers_configurable(self):
        self.write_native()
        for pid in cc.PROVIDERS:
            if pid == "volcengine":
                continue  # 需要真实 ep-id，单独测
            code, _ = run(cc.apply, pid, None, False, False, False)
            self.assertEqual(code, 0, f"switch {pid} 失败")
            cfg = self.toml()
            self.assertEqual(cfg["model_provider"], pid)
            self.assertEqual(cfg["model_providers"][pid]["wire_api"], "responses")
            self.assertEqual(cfg["model_providers"][pid]["base_url"],
                             cc.PROVIDERS[pid]["base_url"])
            self.assertTrue(cfg["model"] in
                            {m["slug"] for m in cc.PROVIDERS[pid]["models"]})

    def test_r1_key_only_needs_env_change(self):
        """改 Key 只需要更新环境变量，不需要再动 config.toml。"""
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        before = self.read_config()
        self.give_key("deepseek", "sk-test-deep-AAAAAAAAAAAAAAAA")
        run(cc.apply, "deepseek", None, False, False, False)
        self.assertEqual(before, self.read_config())
        self.assertEqual(self.env_written.get("DEEPSEEK_API_KEY"),
                         "sk-test-deep-AAAAAAAAAAAAAAAA")

    def test_r1_env_key_is_variable_not_key(self):
        """回归：不能把 API Key 填进 env_key（Missing environment variable）。"""
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        prov = self.toml()["model_providers"]["deepseek"]
        self.assertEqual(prov["env_key"], "DEEPSEEK_API_KEY")
        self.assertNotIn("experimental_bearer_token", prov)
        self.assertNotRegex(self.read_config(), r'env_key\s*=\s*"sk-')

    def test_r1_inline_mode_writes_token(self):
        self.write_native()
        run(cc.apply, "kimi", None, True, False, False)  # inline=True
        prov = self.toml()["model_providers"]["kimi"]
        self.assertEqual(prov["experimental_bearer_token"],
                         "sk-test-kimi-0123456789")
        self.assertNotIn("env_key", prov)

    def test_r1_scrub_removed_wire_api_chat(self):
        """残留 wire_api = "chat" 会让整个配置加载失败，必须清除。"""
        self.write_native()
        cc.CONFIG_PATH.write_text(
            self.read_config() + '\n[model_providers.legacy]\n'
            'name = "legacy"\nbase_url = "https://x"\nwire_api = "chat"\n',
            encoding="utf-8")
        code, out = run(cc.apply, "deepseek", None, False, False, False)
        self.assertEqual(code, 0)
        self.assertNotIn('"chat"', self.read_config())
        self.assertIn("wire_api", out)

    def test_r1_rejects_unknown_provider(self):
        self.write_native()
        code, out = run(cc.apply, "openai", None, False, False, False)
        self.assertEqual(code, 1)
        self.assertIn("未知提供商", out)

    def test_r1_volcengine_requires_endpoint(self):
        code, out = run(cc.apply, "volcengine", None, False, False, False)
        self.assertEqual(code, 1)
        self.assertIn("ep-xxx", out)

    # ---------- R2：可切回原样 ----------

    def test_r2_first_run_snapshots_original(self):
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        snap = cc.ORIGINAL_DIR / "config.toml"
        self.assertTrue(snap.exists())
        self.assertEqual(snap.read_text(encoding="utf-8-sig"), NATIVE_CONFIG)
        manifest = json.loads(
            (cc.ORIGINAL_DIR / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["config_existed"])
        self.assertFalse(manifest["models_existed"])

    def test_r2_snapshot_never_overwritten(self):
        """第二次运行不得覆盖快照。"""
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        run(cc.apply, "kimi", None, False, False, False)
        snap = cc.ORIGINAL_DIR / "config.toml"
        self.assertEqual(snap.read_text(encoding="utf-8-sig"), NATIVE_CONFIG)

    def test_r2_original_restores_everything(self):
        """switch deepseek -> switch kimi -> original 必须回到字节级原样。"""
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        run(cc.apply, "kimi", None, False, False, False)
        self.assertIn('model_provider = "kimi"', self.read_config())

        code, out = run(cc.restore_original)
        self.assertEqual(code, 0)
        self.assertEqual(self.read_config(), NATIVE_CONFIG)
        self.assertFalse(cc.CATALOG_PATH.exists(),
                         "原本没有 models.json，还原后不应存在")
        self.assertIn("已还原原始 config.toml", out)

    def test_r2_switch_native_returns_to_builtin(self):
        """switch native 切回 Codex 内置 provider/默认模型，且不动其他原生配置。"""
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        code, _ = run(cc.apply, "native", None, False, False, False)
        self.assertEqual(code, 0)

        cfg = self.toml()
        self.assertNotIn("model", cfg)
        self.assertNotIn("model_provider", cfg)
        self.assertNotIn("model_catalog_json", cfg)
        self.assertNotIn("deepseek", cfg.get("model_providers", {}))
        self.assertNotIn("enabled-reasoning-efforts", cfg["desktop"])
        # 其他原生行为键原样保留
        self.assertEqual(cfg["model_reasoning_effort"], "high")
        self.assertEqual(cfg["web_search"], "auto")
        self.assertEqual(cfg["desktop"]["followUpQueueMode"], "steer")
        self.assertEqual(cfg["windows"]["sandbox"], "elevated")

    def test_r2_restore_backup_reinstates_catalog(self):
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        run(cc.apply, "kimi", None, False, False, False)  # 此时 models.json 已存在
        cc.CATALOG_PATH.unlink()
        backups = [b for b in sorted(cc.BACKUP_DIR.glob("*/config.toml"), reverse=True)
                   if b.parent != cc.ORIGINAL_DIR]
        self.assertTrue(backups, "应当产生备份")
        self.assertTrue((backups[0].parent / "models.json").exists(),
                        "备份应同时包含 models.json")

        class A:
            index = 0
        code, out = run(cc.cmd_restore, A)
        self.assertEqual(code, 0)
        self.assertTrue(cc.CATALOG_PATH.exists())
        self.assertIn("models.json", out)

    # ---------- 质量护栏 ----------

    def test_generated_toml_and_json_are_valid(self):
        self.write_native()
        code, _ = run(cc.apply, "deepseek", None, False, False, False)
        self.assertEqual(code, 0)
        self.assertIsNone(cc.validate(self.read_config(), "toml"))
        self.assertIsNone(cc.validate(
            cc.CATALOG_PATH.read_text(encoding="utf-8-sig"), "json"))

    def test_output_validation_failure_aborts_write(self):
        """生成的 TOML 不合法时必须放弃写入，不能破坏原文件。"""
        self.write_native()
        original_render = cc.render_config
        cc.render_config = lambda *a, **k: "model = broken"
        try:
            code, out = run(cc.apply, "deepseek", None, False, False, False)
        finally:
            cc.render_config = original_render
        self.assertEqual(code, 1)
        self.assertIn("不合法", out)
        self.assertEqual(self.read_config(), NATIVE_CONFIG)

    def test_catalog_preserves_existing_rich_entries(self):
        """已有的官方 models.json 条目不被覆盖，占位符条目被清理。"""
        rich = {"models": [
            {"slug": "deepseek-v4-pro", "display_name": "官方条目",
             "context_window": 1048576, "official": True},
            {"slug": "REPLACE_WITH_ENDPOINT_ID", "display_name": "占位"},
        ]}
        cc.CATALOG_PATH.write_text(json.dumps(rich, ensure_ascii=False),
                                   encoding="utf-8")
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        models = json.loads(cc.CATALOG_PATH.read_text(encoding="utf-8-sig"))["models"]
        by_slug = {m["slug"]: m for m in models}
        self.assertEqual(by_slug["deepseek-v4-pro"]["display_name"], "官方条目")
        self.assertTrue(by_slug["deepseek-v4-pro"]["official"])
        self.assertNotIn("REPLACE_WITH_ENDPOINT_ID", by_slug)
        # 其他提供商的模型被补进来
        for pid in ("dashscope", "zai", "kimi"):
            self.assertTrue(any(m.startswith(
                {"dashscope": "qwen", "zai": "glm", "kimi": "k3"}[pid])
                for m in by_slug), f"{pid} 的模型未写入目录")

    def test_doctor_passes_after_switch(self):
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        code, out = run(cc.cmd_doctor, None)
        self.assertEqual(code, 0, out)
        self.assertIn("全部通过", out)

    def test_doctor_neutral_when_never_connected(self):
        """从没切换过时自检必须是中性提示，不许吓客户：exit 0 + 无 FAIL。"""
        code, out = run(cc.cmd_doctor, None)
        self.assertEqual(code, 0, out)
        self.assertIn("尚未接入", out)
        self.assertNotIn("FAIL", out)
        self.assertNotIn("存在失败项", out)
        self.assertNotIn("全部通过", out)
        self.assertTrue(cc.LAST["ok"])
        self.assertGreater(cc.LAST["neutral"], 0)
        self.assertEqual(cc.LAST["passed"], cc.LAST["total"])

    def test_doctor_neutral_with_native_provider(self):
        """原生配置（provider=openai，本工具未接管）同样中性，不算失败。"""
        self.write_native()
        code, out = run(cc.cmd_doctor, None)
        self.assertEqual(code, 0, out)
        self.assertIn("尚未接入", out)
        self.assertIn("openai", out)
        self.assertNotIn("FAIL", out)

    def test_doctor_still_fails_on_real_breakage(self):
        """中性化不能吃掉真故障：wire_api 残留 chat 必须仍然标红。"""
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        cfg = self.read_config().replace('wire_api = "responses"',
                                         'wire_api = "chat"')
        cc.atomic_write(cc.CONFIG_PATH, cfg)
        code, out = run(cc.cmd_doctor, None)
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL", out)

    def test_dry_run_writes_nothing(self):
        self.write_native()
        code, out = run(cc.apply, "deepseek", None, False, True, False)
        self.assertEqual(code, 0)
        self.assertIn("dry-run", out)
        self.assertEqual(self.read_config(), NATIVE_CONFIG)
        self.assertFalse(cc.CATALOG_PATH.exists())
        self.assertFalse(cc.ORIGINAL_DIR.exists())

    def test_bom_config_handled(self):
        """Notepad 保存的 UTF-8 with BOM 也能读。"""
        cc.CONFIG_PATH.write_bytes(b"\xef\xbb\xbf" + NATIVE_CONFIG.encode("utf-8"))
        code, _ = run(cc.apply, "deepseek", None, False, False, False)
        self.assertEqual(code, 0)
        self.assertIsNone(cc.validate(self.read_config(), "toml"))

    # ------------------------------------------------------------------
    # 工业级修复回归：脱敏 / 失败必报 / 原子写 / 还原校验 / 白名单
    # ------------------------------------------------------------------

    def test_redact_masks_every_secret_form(self):
        key = "sk-faketoken000000000000000000000000"
        samples = [
            f'experimental_bearer_token = "{key}"',
            f'DEEPSEEK_API_KEY = "{key}"',
            f"Authorization: Bearer {key}",
            f"raw token: {key} end",
        ]
        for s in samples:
            self.assertNotIn(key, cc.redact(s), s)
        # 变量名不是密钥，不能被打码
        self.assertEqual(cc.redact('env_key = "DEEPSEEK_API_KEY"'),
                         'env_key = "DEEPSEEK_API_KEY"')
        self.assertEqual(cc.redact("model = \"deepseek-flash\""),
                         'model = "deepseek-flash"')

    def test_source_has_no_hardcoded_secret(self):
        """源码里不允许出现任何像 Key 的字面量（防止有人把 Key 提交进来）。"""
        src = Path(cc.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(src, r"sk-[A-Za-z0-9_\-]{20,}")
        self.assertNotRegex(src, r"^KEYS\s*:", re.M)

    def test_dry_run_output_never_leaks_full_key(self):
        """inline 模式的 dry-run 会打印整份配置，必须脱敏后再输出。"""
        self.write_native()
        code, out = run(cc.apply, "kimi", None, True, True, False)
        self.assertEqual(code, 0)
        self.assertNotIn("sk-test-kimi-0123456789", out)
        self.assertNotIn("sk-test-kimi-0123456789", cc.LAST.get("preview", ""))
        self.assertNotIn("sk-test-kimi-0123456789", cc.LAST.get("diff", ""))
        self.assertIn("sk-tes", out)  # 掩码还在，看得出填过 Key

    def test_cmd_show_redacts_inline_token(self):
        self.write_native()
        run(cc.apply, "kimi", None, True, False, False)  # inline 写入
        self.assertIn("sk-test-kimi-0123456789", self.read_config())
        code, out = run(cc.cmd_show, None)
        self.assertEqual(code, 0)
        self.assertNotIn("sk-test-kimi-0123456789", out)
        self.assertIn("experimental_bearer_token", out)

    def test_setkey_persists_to_credential_and_env(self):
        key = "sk-test-new-01234567890123"
        ns = SimpleNamespace(provider="deepseek", key=key, dry_run=False)
        code, out = run(cc.cmd_setkey, ns)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.secrets.get("DEEPSEEK_API_KEY"), key)
        self.assertEqual(self.env_written.get("DEEPSEEK_API_KEY"), key)
        self.assertNotIn(key, out)  # 输出只给掩码

    def test_setkey_rejects_empty_and_placeholder(self):
        self.secrets.pop("DEEPSEEK_API_KEY", None)
        for bad in ("", "   ", "<your-key>", "REPLACE_ME", "你的key", "xxxxx"):
            ns = SimpleNamespace(provider="deepseek", key=bad, dry_run=False)
            code, out = run(cc.cmd_setkey, ns)
            self.assertEqual(code, 1, f"应当拒绝：{bad!r}，输出：{out}")
        self.assertNotIn("DEEPSEEK_API_KEY", self.secrets)

    def test_setkey_env_failure_stops_before_credential(self):
        """环境变量写失败必须报错退出，且不能留下半截状态。"""
        self.secrets.pop("DEEPSEEK_API_KEY", None)

        def boom(name: str, value: str) -> bool:
            return False

        orig = cc.write_user_env
        cc.write_user_env = boom
        try:
            ns = SimpleNamespace(provider="deepseek",
                                 key="sk-test-abc-0123456789", dry_run=False)
            code, out = run(cc.cmd_setkey, ns)
        finally:
            cc.write_user_env = orig
        self.assertEqual(code, 1)
        self.assertIn("没有保存", out)
        self.assertNotIn("DEEPSEEK_API_KEY", self.secrets)

    def test_write_user_env_reports_registry_failure(self):
        orig = cc._env_registry_set
        cc._env_registry_set = lambda name, value: "模拟注册表写入失败"
        try:
            # setUp 已把 cc.write_user_env 换成假的，这里必须调真实实现
            ok = self._orig_write_env("CODEX_CN_TEST_TMP_KEY", "sk-test-x")
        finally:
            cc._env_registry_set = orig
        self.assertFalse(ok)
        self.assertNotIn("CODEX_CN_TEST_TMP_KEY", os.environ)

    def test_doctor_fails_when_env_only_lives_in_process(self):
        """只在本进程有值、没持久化 -> 自检必须失败（否则新 Codex 读不到）。"""
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        os.environ["DEEPSEEK_API_KEY"] = "sk-test-deep-012345678901"
        self.env_written.pop("DEEPSEEK_API_KEY", None)
        code, out = run(cc.cmd_doctor, None)
        self.assertEqual(code, 1)
        self.assertIn("只有当前进程有值", out)

    def test_atomic_write_is_atomic_and_leaves_no_tmp(self):
        cc.atomic_write(cc.CONFIG_PATH, 'model = "x"\n')
        self.assertEqual(self.read_config(), 'model = "x"\n')
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])

    def test_atomic_write_many_rolls_back_on_second_failure(self):
        cc.atomic_write(cc.CONFIG_PATH, "old = true\n")
        before = self.read_config()
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("模拟磁盘故障")
            return real_replace(src, dst)

        os.replace = flaky
        try:
            with self.assertRaises(OSError):
                cc.atomic_write_many([(cc.CONFIG_PATH, "new = true\n"),
                                      (cc.CATALOG_PATH, '{"models": []}\n')])
        finally:
            os.replace = real_replace

        self.assertEqual(self.read_config(), before, "已替换的文件必须回滚")
        self.assertFalse(cc.CATALOG_PATH.exists())
        self.assertEqual(list(self.tmp.glob("*.tmp")), [], "临时文件必须清掉")

    def test_restore_rejects_corrupt_backup(self):
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        run(cc.apply, "kimi", None, False, False, False)
        before = self.read_config()
        backups = [b for b in sorted(cc.BACKUP_DIR.glob("*/config.toml"),
                                     reverse=True)
                   if b.parent != cc.ORIGINAL_DIR]
        self.assertTrue(backups)
        backups[0].write_text("model = broken", encoding="utf-8")

        class A:
            index = 0

        code, out = run(cc.cmd_restore, A)
        self.assertEqual(code, 1)
        self.assertIn("拒绝还原", out)
        self.assertEqual(self.read_config(), before, "坏备份不得覆盖当前配置")

    def test_restore_original_rejects_corrupt_snapshot(self):
        self.write_native()
        run(cc.apply, "deepseek", None, False, False, False)
        before = self.read_config()
        (cc.ORIGINAL_DIR / "config.toml").write_text("model = broken",
                                                     encoding="utf-8")
        code, out = run(cc.restore_original)
        self.assertEqual(code, 1)
        self.assertIn("不合法", out)
        self.assertEqual(self.read_config(), before, "坏快照不得覆盖当前配置")

    def test_rejects_malicious_model_slug(self):
        self.write_native()
        evil = 'x"\nmodel = "pwned'
        code, out = run(cc.apply, "deepseek", evil, False, False, False)
        self.assertEqual(code, 1)
        self.assertIn("非法的模型 slug", out)
        self.assertEqual(self.read_config(), NATIVE_CONFIG)

    def test_inline_mode_prints_risk_warning(self):
        self.write_native()
        code, out = run(cc.apply, "kimi", None, True, True, False)
        self.assertEqual(code, 0)
        self.assertIn("[提醒]", out)
        self.assertIn("明文", out)
        self.assertTrue(cc.LAST.get("risk"))

    def test_dry_run_provides_unified_diff(self):
        self.write_native()
        code, _ = run(cc.apply, "deepseek", None, False, True, False)
        self.assertEqual(code, 0)
        diff = cc.LAST.get("diff", "")
        self.assertTrue(diff.startswith("---"), diff[:60])
        self.assertIn('+model_provider = "deepseek"', diff)
        self.assertNotIn("sk-", diff)

    # ------------------------------------------------------------------
    # testkey：连通性探测的结论口径
    # ------------------------------------------------------------------

    def _with_urlopen(self, mode, fn, *a, **k):
        """把 urllib.request.urlopen 换成假的，跑完恢复（不发真实网络请求）。"""
        import urllib.error
        import urllib.request

        def fake(req, timeout=None):
            if mode == "ok":
                class Resp:
                    def getcode(self):
                        return 200

                    def read(self, n=-1):
                        return b"{}"

                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return False
                return Resp()
            if mode == "401":
                raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized",
                                             {}, io.BytesIO(b"{}"))
            raise urllib.error.URLError("getaddrinfo failed")

        orig = urllib.request.urlopen
        urllib.request.urlopen = fake
        try:
            return fn(*a, **k)
        finally:
            urllib.request.urlopen = orig

    def test_testkey_2xx_counts_as_success(self):
        ns = SimpleNamespace(provider="deepseek", timeout=1)
        code, out = self._with_urlopen("ok", run, cc.cmd_testkey, ns)
        self.assertEqual(code, 0)
        self.assertIs(cc.LAST.get("ok"), True)
        self.assertNotIn("sk-test-deep", out)

    def test_testkey_401_counts_as_failure(self):
        ns = SimpleNamespace(provider="deepseek", timeout=1)
        code, out = self._with_urlopen("401", run, cc.cmd_testkey, ns)
        self.assertEqual(code, 1)
        self.assertIn("401", out)
        self.assertNotIn("sk-test-deep", out)

    def test_testkey_network_error_is_not_failure(self):
        """按约定：连不上只是提示，不能算自检失败（离线同事不受伤害）。"""
        ns = SimpleNamespace(provider="deepseek", timeout=1)
        code, out = self._with_urlopen("net", run, cc.cmd_testkey, ns)
        self.assertEqual(code, 0)
        self.assertTrue(cc.LAST.get("network_error"))
        self.assertIn("网络", out)
        self.assertNotIn("sk-test-deep", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
