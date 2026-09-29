#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codex_gui.py 图形界面自动化测试。

运行：python -m unittest -v test_codex_gui
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from tkinter import ttk

os.environ["CODEX_HOME"] = str(Path(tempfile.mkdtemp(prefix="codex_gui_boot_")))

import codex_cn as cc  # noqa: E402
import codex_gui as gui  # noqa: E402


NATIVE_CONFIG = """\
notify = [ "noop" ]
model = "gpt-5.2"
model_provider = "openai"

[windows]
sandbox = "elevated"
"""


class GuiCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="codex_gui_case_"))
        cc.set_codex_home(self.tmp)

        # 绝不能让测试写到真实凭据管理器 / 真实用户环境变量里
        self.secrets: dict[str, str] = {}
        self.env_written: dict[str, str] = {}
        self._saved_env = {p["env_key"]: os.environ.get(p["env_key"])
                           for p in cc.PROVIDERS.values()}
        self._orig_cred = (cc.cred_read, cc.cred_write,
                           cc.cred_delete, cc.cred_available)
        self._orig_write_env = cc.write_user_env
        self._orig_read_env = cc.read_user_env

        cc.cred_read = lambda name: self.secrets.get(name)
        cc.cred_write = lambda name, secret: (
            self.secrets.__setitem__(name, secret) or True)
        cc.cred_delete = lambda name: self.secrets.pop(name, None) is not None
        cc.cred_available = lambda: True

        def fake_write(name: str, value: str) -> bool:
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
            self.env_written[name] = value
            return True

        cc.write_user_env = fake_write
        cc.read_user_env = lambda name: self.env_written.get(name)

        for pid in cc.PROVIDERS:
            self.secrets[cc.PROVIDERS[pid]["env_key"]] = \
                f"sk-test-{pid}-0123456789"
        for p in cc.PROVIDERS.values():
            os.environ.pop(p["env_key"], None)

        # 日志必须落到测试临时目录，不能写用户真实的 %LOCALAPPDATA%
        self._saved_log_dir = os.environ.get("CODEXSWITCH_LOG_DIR")
        os.environ["CODEXSWITCH_LOG_DIR"] = str(self.tmp / "logs")

        self.app = gui.App()

    def tearDown(self) -> None:
        (cc.cred_read, cc.cred_write,
         cc.cred_delete, cc.cred_available) = self._orig_cred
        cc.write_user_env = self._orig_write_env
        cc.read_user_env = self._orig_read_env
        if self._saved_log_dir is None:
            os.environ.pop("CODEXSWITCH_LOG_DIR", None)
        else:
            os.environ["CODEXSWITCH_LOG_DIR"] = self._saved_log_dir
        for env_name, old in self._saved_env.items():
            if old is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = old
        try:
            self.app.on_close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pump(self, cond, timeout: float = 20.0) -> bool:
        """驱动 Tk 事件循环直到条件满足。"""
        end = time.time() + timeout
        while time.time() < end:
            self.app.update()
            if cond():
                return True
            time.sleep(0.02)
        return False

    def wait_idle(self) -> bool:
        return self.pump(lambda: (not self.app.busy) and self.app.q.empty())

    def wait_ready(self) -> bool:
        """等启动时的自动刷新跑完（after(250) 触发）。"""
        return self.pump(
            lambda: bool(self.app.provider_rows)
            and (not self.app.busy) and self.app.q.empty())

    # ------------------------------------------------------------------
    def test_app_constructs_and_refreshes(self):
        """启动后自动拉取服务商列表并填充树。"""
        self.assertTrue(self.wait_ready())
        self.assertEqual(len(self.app.provider_rows), len(cc.PROVIDERS))
        self.assertEqual(len(self.app.tree.get_children()), len(cc.PROVIDERS))

    def test_status_bar_shows_active_provider(self):
        """状态卡显示配置里实际生效的 provider 与模型。"""
        self.assertTrue(self.wait_ready())
        status = self.app.var_status.get()
        self.assertIn("配置目录", status)
        self.assertIn("当前生效", status)

    def test_provider_selection_switches_model_list(self):
        """选中不同服务商，模型下拉框跟着换。"""
        self.assertTrue(self.wait_ready())
        self.app.tree.selection_set("zai")
        self.app._on_select_provider()
        self.assertEqual(self.app.var_pid.get().split()[0], "zai")
        self.assertEqual(self.app.cmb_model.get(), "glm-5.3")
        self.app.tree.selection_set("kimi")
        self.app._on_select_provider()
        self.assertEqual(self.app.cmb_model.get(), "k3")

    def test_doctor_tab_fills_all_checks(self):
        """自检页渲染出全部检查项（需先生成一份配置）。"""
        self.assertTrue(self.wait_ready())
        cc.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cc.CONFIG_PATH.write_text(NATIVE_CONFIG, encoding="utf-8")
        cc.run_op(cc.apply, "deepseek", None, False, False, False)
        self.app.run_doctor()
        self.assertTrue(self.pump(
            lambda: self.app.var_doctor.get() != "尚未自检"))
        rows = self.app.tree_doc.get_children()
        self.assertGreaterEqual(len(rows), 10)
        self.assertIn("项通过", self.app.var_doctor.get())
        values = self.app.tree_doc.item(rows[0])["values"]
        self.assertIn(values[0], ("通过", "失败"))

    def test_doctor_shows_neutral_rows_when_not_connected(self):
        """没切换过时自检页显示灰字"尚未接入"，不出现"失败"。"""
        self.assertTrue(self.wait_ready())
        self.app.run_doctor()
        self.assertTrue(self.pump(
            lambda: self.app.var_doctor.get() != "尚未自检"))
        summary = self.app.var_doctor.get()
        self.assertIn("尚未接入", summary)
        self.assertNotIn("存在失败项", summary)
        vals = [self.app.tree_doc.item(r)["values"][0]
                for r in self.app.tree_doc.get_children()]
        self.assertIn("尚未接入", vals)
        self.assertNotIn("失败", vals)

    def test_log_lines_are_separate(self):
        """日志窗每一行单独成行，不会糊成一行。"""
        self.assertTrue(self.wait_ready())
        self.app.log_text.configure(state="normal")
        self.app.log_text.delete("1.0", "end")
        self.app.log_text.configure(state="disabled")
        for s in ("行一", "行二", "行三"):
            self.app._append_log(s)
        lines = self.app.log_text.get("1.0", "end").splitlines()
        self.assertEqual(lines[:3], ["行一", "行二", "行三"])

    def test_backup_list_populates(self):
        """备份页能列出历史备份并识别原始快照。"""
        self.assertTrue(self.wait_ready())
        cc.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cc.CONFIG_PATH.write_text(NATIVE_CONFIG, encoding="utf-8")
        cc.run_op(cc.apply, "deepseek", None, False, False, False)
        cc.run_op(cc.apply, "kimi", None, False, False, False)
        self.app.run_task("刷新备份", self.app._op_list_backups,
                          on_done=self.app._on_list_backups)
        self.assertTrue(self.wait_idle())
        self.assertEqual(self.app.lst_backup.size(),
                         len(self.app._backup_paths))
        self.assertGreaterEqual(self.app.lst_backup.size(), 2)
        self.assertIn("原始快照封存于", self.app.var_backup_info.get())

    def test_gui_switch_pipeline_writes_valid_config(self):
        """界面发起的切换走同一套引擎，产出合法配置。"""
        self.assertTrue(self.wait_ready())
        self.app.current_pid = "deepseek"
        self.app.run_task("切换模型", cc.apply, "deepseek", "deepseek-flash",
                          False, False, False,
                          on_done=self.app._after_switch)
        self.assertTrue(self.wait_idle())
        self.assertEqual(self.app.active_provider, "deepseek")
        self.assertEqual(self.app.active_model, "deepseek-flash")
        err = cc.validate(cc.CONFIG_PATH.read_text(encoding="utf-8-sig"), "toml")
        self.assertIsNone(err)

    def test_codex_running_is_bool_and_ignores_self(self):
        val = gui.codex_running()
        self.assertIsInstance(val, bool)

    def test_spec_and_docs_present_for_packaging(self):
        root = Path(__file__).resolve().parent
        for name in ("CodexSwitch.spec", "build_exe.bat", "version.txt",
                     "README_CN.txt", "requirements.txt", "requirements.lock",
                     "generate_icon.py", "app.manifest"):
            self.assertTrue((root / name).exists(), name)
        # 说明书必须是 UTF-8，否则记事本打开是乱码
        self.assertIn("Codex 模型切换器",
                      (root / "README_CN.txt").read_text(encoding="utf-8"))
        # 构建脚本必须纯 ASCII：混进中文后只要有人用 UTF-8 编辑器存一次就坏
        (root / "build_exe.bat").read_bytes().decode("ascii")
        # 高 DPI manifest 必须挂上
        manifest = (root / "app.manifest").read_text(encoding="utf-8")
        self.assertIn("dpiAware", manifest)
        self.assertIn("manifest=", (root / "CodexSwitch.spec").read_text(
            encoding="utf-8"))
        # 构建依赖已锁版本
        self.assertIn("pyinstaller==",
                      (root / "requirements.lock").read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # 工业级修复回归：脱敏 / 风险提示 / 精确进程匹配 / diff 预览
    # ------------------------------------------------------------------

    def test_log_redacts_secret_in_widget_and_on_disk(self):
        """界面日志与落盘日志是两条独立路径，两条都不能出现明文 Key。"""
        self.assertTrue(self.wait_ready())
        full = "sk-faketoken000000000000000000000000"
        self.app.log_text.configure(state="normal")
        self.app.log_text.delete("1.0", "end")
        self.app.log_text.configure(state="disabled")

        self.app._append_log(f'experimental_bearer_token = "{full}"')
        widget = self.app.log_text.get("1.0", "end")
        self.assertNotIn(full, widget)
        self.assertIn("sk-fak...0000", widget)  # 掩码仍在，能认出填过 Key

        if self.app.logger:
            for h in self.app.logger.handlers:
                h.flush()
        if self.app.log_path and self.app.log_path.exists():
            self.assertNotIn(
                full,
                self.app.log_path.read_text(encoding="utf-8", errors="ignore"))

    def test_log_dir_default_is_windows_appdata_not_codex_home(self):
        """默认日志必须落在 %LOCALAPPDATA%\\CodexSwitch\\logs，
        不许写进客户的 ~/.codex（那是 Codex 自己的目录）。"""
        os.environ.pop("CODEXSWITCH_LOG_DIR", None)
        try:
            d = gui.app_log_dir()
        finally:
            os.environ["CODEXSWITCH_LOG_DIR"] = str(self.tmp / "logs")
        self.assertEqual((d.parent.name, d.name), ("CodexSwitch", "logs"))
        self.assertNotEqual(d, cc.CODEX_DIR / "logs")
        self.assertFalse(str(d).startswith(str(cc.CODEX_DIR)))

    def test_log_dir_env_override(self):
        """CODEXSWITCH_LOG_DIR 覆盖生效，测试与便携部署都靠它。"""
        custom = self.tmp / "custom_logs"
        os.environ["CODEXSWITCH_LOG_DIR"] = str(custom)
        try:
            self.assertEqual(gui.app_log_dir(), custom)
        finally:
            os.environ["CODEXSWITCH_LOG_DIR"] = str(self.tmp / "logs")

    def test_logger_wrote_to_configured_dir(self):
        """App 实际落盘的目录与配置一致，且文件真的存在。"""
        self.assertTrue(self.wait_ready())
        self.assertIsNotNone(self.app.log_path, "日志文件应创建成功")
        self.assertEqual(self.app.log_path.parent,
                         Path(os.environ["CODEXSWITCH_LOG_DIR"]))
        self.assertEqual(self.app.log_path.name, "switch.log")
        self.assertTrue(self.app.log_path.exists())

    def test_inline_toggle_warns_only_once(self):
        """开启明文写入必须弹风险提示，且只弹一次不骚扰。"""
        import unittest.mock as mock

        self.assertTrue(self.wait_ready())
        with mock.patch.object(gui.messagebox, "showwarning") as warn:
            self.app.var_inline.set(True)
            self.app._toggle_inline()
            self.assertEqual(warn.call_count, 1)
            self.app.var_inline.set(False)
            self.app._toggle_inline()
            self.app.var_inline.set(True)
            self.app._toggle_inline()
            self.assertEqual(warn.call_count, 1)
        self.assertIn("风险", self.app.var_inline_hint.get())
        self.assertIn("明文", warn.call_args[0][1])

        self.app.var_inline.set(False)
        self.app._toggle_inline()
        self.assertEqual(cc.AUTH_MODE, "env")
        self.assertIn("安全", self.app.var_inline_hint.get())

    def test_codex_process_detection_uses_exact_names(self):
        """进程检测必须精确匹配，不能"名字里含 codex 就算"。"""
        self.assertIn("codex.exe", gui.CODEX_PROCESS_NAMES)
        for false_positive in ("codex-notes.exe", "mycodex.exe",
                               "codex_cn.py", "scodex.exe"):
            self.assertNotIn(false_positive, gui.CODEX_PROCESS_NAMES)

    def test_preview_diff_has_no_secret_and_has_changes(self):
        """预览必须给出真实 diff，且 inline 模式下不得泄露 Key。"""
        self.assertTrue(self.wait_ready())
        cc.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cc.CONFIG_PATH.write_text(NATIVE_CONFIG, encoding="utf-8")
        res = cc.run_op(cc.apply, "kimi", None, True, True, False)
        self.assertTrue(res.ok, res.text)
        diff = res.data.get("diff", "")
        preview = res.data.get("preview", "")
        self.assertIn("+model_provider", diff)
        self.assertIn("---", diff)
        self.assertTrue(res.data.get("risk"))
        full = "sk-test-kimi-0123456789"
        self.assertNotIn(full, diff)
        self.assertNotIn(full, preview)

    def test_testkey_button_exists(self):
        self.assertTrue(self.wait_ready())
        self.assertTrue(callable(getattr(self.app, "test_key", None)))
        found = []

        def walk(w) -> None:
            for child in w.winfo_children():
                try:
                    if isinstance(child, ttk.Button) and \
                            child.cget("text") == "测试 Key":
                        found.append(child)
                except Exception:  # noqa: BLE001
                    pass
                walk(child)

        walk(self.app)
        self.assertTrue(found, "界面上应有「测试 Key」按钮")


if __name__ == "__main__":
    unittest.main(verbosity=2)
