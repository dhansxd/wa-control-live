import fcntl
from io import StringIO
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "whatsapp_control_lifecycle_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
PLUGIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PLUGIN
SPEC.loader.exec_module(PLUGIN)
from whatsapp_control_lifecycle_test import cli


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = workspace / "plugin-data"
        self.directory = workspace / "worker"
        self.enterContext(patch.object(cli, "socket_dir", return_value=self.directory))
        self.show = self.enterContext(patch.object(cli, "show"))

    def test_cold_purge_uses_lock_and_reports_unconfirmed_unlink(self):
        self.root.mkdir()
        (self.root / "accounts.json").write_text('{"shop": {}}', encoding="utf-8")
        original_flock = fcntl.flock

        def acquire(lock, flags):
            self.assertTrue(self.root.exists())
            self.assertEqual(Path(lock.name), self.directory / "lock")
            return original_flock(lock, flags)

        with patch.object(cli.fcntl, "flock", side_effect=acquire) as lock:
            cli.cleanup(self.root, purge=True)
        lock.assert_called_once()
        self.assertFalse(self.root.exists())
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.show.call_args.args[0]["accounts"], [
            {"account": "shop", "local_removed": True, "remote_unlinked": False},
        ])

    def test_worker_that_wins_cold_purge_race_keeps_its_data(self):
        self.root.mkdir()
        accounts = self.root / "accounts.json"
        accounts.write_text('{"shop": {}}', encoding="utf-8")
        original_flock = fcntl.flock

        def acquire(lock, flags):
            with (self.directory / "lock").open("a", encoding="utf-8") as worker:
                original_flock(worker, flags)
                return original_flock(lock, flags)

        with patch.object(cli.fcntl, "flock", side_effect=acquire), \
                patch.object(cli.time, "monotonic", side_effect=[0, 13]):
            with self.assertRaisesRegex(RuntimeError, "worker has not exited"):
                cli.cleanup(self.root, purge=True)
        self.assertTrue(accounts.is_file())
        self.assertTrue((self.directory / "lock").is_file())
        self.show.assert_not_called()

    def test_empty_profile_purge_leaves_no_artifacts(self):
        cli.cleanup(self.root, purge=True)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.show.call_args.args[0]["accounts"], [])

    def test_only_explicit_purge_removes_the_unified_runtime_under_cleanup_lock(self):
        self.root.mkdir()

        def cleanup_runtime(*, purge):
            self.assertTrue(self.root.is_dir())
            with (self.directory / "lock").open("a", encoding="utf-8") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return {"unified_runtime_removed": purge}

        cleanup = Mock(side_effect=cleanup_runtime)
        cli.cleanup(self.root, purge=False, cleanup_runtime=cleanup)
        self.assertTrue(self.root.exists())
        self.assertTrue(self.show.call_args.args[0]["data_retained"])
        cli.cleanup(self.root, purge=True, cleanup_runtime=cleanup)
        self.assertEqual([call.kwargs["purge"] for call in cleanup.call_args_list], [False, True])
        self.assertTrue(self.show.call_args.args[0]["unified_runtime_removed"])
        self.assertFalse(self.root.exists())

    def test_active_report_keeps_each_device_outcome_separate(self):
        self.root.mkdir()
        bot = "628120002222"
        (self.root / "accounts.json").write_text(json.dumps({
            "work": {"_transport": "rust", "_identity": {"phone": bot}}, "shop": {},
        }))
        bridge = self.root / "bridge"
        bridge.mkdir()
        db = sqlite3.connect(bridge / "transport.sqlite3")
        with db:
            db.execute("CREATE TABLE auth (account TEXT, kind TEXT, id TEXT)")
            db.execute("INSERT INTO auth VALUES ('work', 'creds', 'current')")
        db.close()
        self.directory.mkdir()
        (self.directory / "rpc").touch()
        active = [{"account": "work", "runtime": "unified", "remote_unlinked": True, "unlink_requested": True},
                  {"account": "shop", "remote_unlinked": False, "unlink_requested": True}]
        runtimes = [{"account": "bot", "runtime": "unified", "bot_number": bot, "remote_unlinked": False},
                    {"account": "bot", "runtime": "legacy", "bot_number": bot, "remote_unlinked": False}]
        with patch.object(cli, "invoke", return_value=active) as worker:
            cli.cleanup(self.root, purge=True, cleanup_runtime=lambda **_: {"runtime_accounts": runtimes})
        worker.assert_called_once_with(self.root, "purge")
        result = self.show.call_args.args[0]
        self.assertEqual({(row["account"], row.get("runtime")): row["remote_unlinked"] for row in result["accounts"]}, {
            ("work", "unified"): True, ("shop", None): False, ("work", "baileys"): False, ("bot", "legacy"): False,
        })
        self.assertTrue(all(row["local_removed"] for row in result["accounts"]))

    def test_symlinked_profile_storage_is_refused_before_worker_contact(self):
        foreign = self.root.parent / "foreign"
        foreign.mkdir()
        marker = foreign / "accounts.json"
        marker.write_text('{}')
        self.root.symlink_to(foreign, target_is_directory=True)
        with patch.object(cli, "invoke") as invoke, self.assertRaisesRegex(ValueError, "belong to this profile"):
            cli.cleanup(self.root, purge=True)
        invoke.assert_not_called()
        self.assertTrue(marker.exists())

    def test_status_reports_retained_accounts_after_interrupted_worker(self):
        self.root.mkdir()
        (self.root / "accounts.json").write_text('{"shop": {}}', encoding="utf-8")
        (self.root / "setup.json").write_text('{"default_account": "shop"}', encoding="utf-8")
        self.directory.mkdir()
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(self.directory / "rpc"))
        constants = SimpleNamespace(get_hermes_home=lambda: self.root.parent)
        with patch.dict(sys.modules, {"hermes_constants": constants}), \
                patch.object(cli, "runtime_path", return_value=self.root), \
                patch("whatsapp_control_lifecycle_test.runtime.socket_dir", return_value=self.directory):
            cli.run(None, "", SimpleNamespace(action="status"))
        self.assertEqual(self.show.call_args.args[0], {
            "running": False, "default_account": "shop", "accounts": {"shop": {"status": "offline"}},
        })

    def test_unified_control_pair_explains_which_phone_to_scan_before_showing_qr(self):
        home = self.root.parent / "profiles" / "cobalt"
        constants = SimpleNamespace(get_hermes_home=lambda: home)
        result = {"account": "personal"}

        def pair(root, action, args, on_event):
            self.assertEqual((root, action, args), (cli.runtime_path(home), "pair", result))
            on_event({"terminal": "fake QR code"})
            return result

        with patch.dict(sys.modules, {"hermes_constants": constants}), \
                patch.object(cli, "invoke", side_effect=pair), \
                patch.object(sys, "stdout", new_callable=StringIO) as output, \
                patch.object(output, "isatty", return_value=True):
            cli.run(None, "", SimpleNamespace(action="pair", account="personal"), bot_runtime=self.root)
        self.assertEqual(output.getvalue().splitlines(), [
            "Scan this QR code from the additional Control account 'personal'.",
            "On that phone, open WhatsApp > Linked devices > Link a device.",
            "fake QR code",
        ])
        self.show.assert_called_once_with(result)

    def test_pair_or_repair_with_stopped_service_guides_the_same_profile(self):
        home = self.root.parent / "profiles" / "cobalt"
        constants = SimpleNamespace(get_hermes_home=lambda: home)
        for action, bot_runtime, command in [
            ("pair", self.root, "wa-control-live"),
            ("repair", None, "whatsapp-control"),
        ]:
            with self.subTest(action=action), patch.dict(sys.modules, {"hermes_constants": constants}), \
                    patch.object(cli, "invoke", side_effect=cli.ServiceStopped("offline")), \
                    patch.object(sys.stdout, "isatty", return_value=True), \
                    patch.object(sys, "stderr", new_callable=StringIO) as output, \
                    self.assertRaises(SystemExit):
                cli.run(None, "", SimpleNamespace(action=action, account="personal"), bot_runtime=bot_runtime)
            self.assertIn(f"`hermes -p cobalt {command} serve`", output.getvalue())
