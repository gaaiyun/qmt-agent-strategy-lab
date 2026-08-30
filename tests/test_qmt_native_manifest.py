# coding: utf-8
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from build_qmt_native_manifest import (
    analyze_source,
    build_manifest,
    parse_qmt_logs,
    render_markdown,
    sanitize_manifest,
)


SAFE_SOURCE = '''\
# coding: utf-8
ENABLE_ORDERS = False
BACKTEST_ORDERS = True
LIVE_CONFIRM_TOKEN = ""
VALID_ACCOUNTS = ()
RISK_UNIVERSE = ("510300.SH", "510500.SH")
DEFENSIVE_SYMBOL = "511010.SH"
UNIVERSE = RISK_UNIVERSE + (DEFENSIVE_SYMBOL,)
MODEL_CAPITAL = 100000.0
def init(C):
    C.set_universe(UNIVERSE)
def handlebar(C):
    return None
'''


def log_lines(account="BACKTEST", universe=3):
    return "\n".join([
        "2026-08-30 06:00:00,000 [INFO] [parser]parse complete, name = QMT_TEST_SAFE",
        "2026-08-30 06:00:01,000 [INFO] ContextInfo::set_universe requestID:0D:\\QMT\\python\\QMT_TEST_SAFE.py_SH000001, sl:[%s, ]" % universe,
        "2026-08-30 06:00:01,001 [INFO] start back test mode, reqid = 0D:\\QMT\\python\\QMT_TEST_SAFE.py_SH000001",
        "2026-08-30 06:00:02,000 [INFO] >>> [PYTHON PASSORDER] python passorder start, opType:23, accountID:%s, strategyName1:test_safe, quickTrade:0, requestID:0D:\\QMT\\python\\QMT_TEST_SAFE.py_SH000001, barpos:10" % account,
        "2026-08-30 06:00:03,000 [INFO] calc backtest index, reqid = 0D:\\QMT\\python\\QMT_TEST_SAFE.py_SH000001",
    ]) + "\n"


class QmtNativeManifestTests(unittest.TestCase):
    def test_source_analysis_extracts_safety_universe_and_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "safe.py"
            path.write_text(SAFE_SOURCE, encoding="utf-8")
            result = analyze_source(path)
        self.assertEqual(result["syntax"], "pass")
        self.assertTrue(result["safety"]["source_gate_safe"])
        self.assertEqual(result["universe_count"], 3)
        self.assertEqual(result["parameters"]["MODEL_CAPITAL"], 100000.0)

    def test_log_parser_selects_run_and_counts_virtual_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "XtClient_Formula_20260830.log"
            path.write_text(log_lines(), encoding="utf-8")
            evidence = parse_qmt_logs([path], {"QMT_TEST_SAFE"})["QMT_TEST_SAFE"]
        run = evidence["latest_run"]
        self.assertEqual(run["backtest_passorder_calls_observed"], 1)
        self.assertEqual(run["buy_calls"], 1)
        self.assertEqual(run["accounts_observed"], ["BACKTEST"])
        self.assertEqual(evidence["all_accounts_observed"], ["BACKTEST"])
        self.assertNotIn("passorder_calls", run)
        self.assertEqual(run["universe_count_observed"], 3)
        self.assertEqual(run["calc_backtest_index_count"], 1)

    def _build(self, account="BACKTEST", universe=3):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        source = root / "source"
        deployment = root / "deployment"
        logs = root / "logs"
        source.mkdir()
        deployment.mkdir()
        logs.mkdir()
        (source / "safe.py").write_text(SAFE_SOURCE, encoding="utf-8")
        (deployment / "QMT_TEST_SAFE.py").write_text(SAFE_SOURCE, encoding="utf-8")
        os.utime(source / "safe.py", (1_700_000_000, 1_700_000_000))
        os.utime(deployment / "QMT_TEST_SAFE.py", (1_700_000_000, 1_700_000_000))
        log_path = logs / "XtClient_Formula_20260830.log"
        log_path.write_text(log_lines(account=account, universe=universe), encoding="utf-8")
        config = {
            "allowed_order_accounts": ["BACKTEST"],
            "models": [{
                "qmt_model_name": "QMT_TEST_SAFE",
                "source_file": "safe.py",
                "strategy_name": "test_safe",
            }],
        }
        return temporary, build_manifest(config, source, deployment, [log_path], source)

    def test_non_backtest_account_fails_closed(self):
        temporary, manifest = self._build(account="55000000")
        try:
            self.assertEqual(manifest["summary"]["safety_status"], "fail")
            self.assertEqual(manifest["models"][0]["safety"]["unexpected_order_accounts"], ["55000000"])
            self.assertTrue(
                manifest["models"][0]["safety"]
                ["non_backtest_account_order_actions_observed"]
            )
            self.assertEqual(
                manifest["models"][0]["log_evidence"]["latest_run"]
                ["backtest_passorder_calls_observed"],
                0,
            )
        finally:
            temporary.cleanup()

    def test_universe_drift_blocks_version_closure(self):
        temporary, manifest = self._build(universe=2)
        try:
            closure = manifest["models"][0]["version_closure"]
            self.assertEqual(closure["status"], "blocked")
            self.assertFalse(closure["universe_count_matches"])
            self.assertEqual(manifest["summary"]["verified_version_closures"], 0)
            self.assertEqual(manifest["summary"]["partial_version_closures"], 0)
            self.assertEqual(manifest["summary"]["blocked_version_closures"], 1)
            self.assertEqual(manifest["summary"]["native_validation_status"], "blocked")
            markdown = render_markdown(sanitize_manifest(manifest))
            self.assertIn("verified=0", markdown)
            self.assertIn("没有模型形成完整版本闭环", markdown)
        finally:
            temporary.cleanup()

    def test_matching_hash_without_performance_panel_is_only_partial(self):
        temporary, manifest = self._build()
        try:
            closure = manifest["models"][0]["version_closure"]
            self.assertEqual(closure["status"], "partial")
            self.assertFalse(closure["performance_panel_confirmed"])
            self.assertEqual(manifest["summary"]["verified_version_closures"], 0)
            self.assertEqual(manifest["summary"]["partial_version_closures"], 1)
            self.assertEqual(manifest["summary"]["native_validation_status"], "partial")
        finally:
            temporary.cleanup()

    def test_summary_counts_partial_closure(self):
        temporary = tempfile.TemporaryDirectory()
        try:
            root = Path(temporary.name)
            source = root / "source"
            deployment = root / "deployment"
            logs = root / "logs"
            source.mkdir()
            deployment.mkdir()
            logs.mkdir()
            (source / "safe.py").write_text(SAFE_SOURCE, encoding="utf-8")
            (deployment / "QMT_TEST_SAFE.py").write_bytes(b"MiFBO-sanitized-test-payload")
            os.utime(source / "safe.py", (1_700_000_000, 1_700_000_000))
            os.utime(deployment / "QMT_TEST_SAFE.py", (1_700_000_001, 1_700_000_001))
            log_path = logs / "XtClient_Formula_20260830.log"
            log_path.write_text(log_lines(), encoding="utf-8")
            manifest = build_manifest(
                {"models": [{"qmt_model_name": "QMT_TEST_SAFE", "source_file": "safe.py"}]},
                source,
                deployment,
                [log_path],
                source,
            )
            self.assertEqual(manifest["summary"]["verified_version_closures"], 0)
            self.assertEqual(manifest["summary"]["partial_version_closures"], 1)
            self.assertEqual(manifest["summary"]["blocked_version_closures"], 0)
            self.assertEqual(manifest["summary"]["native_validation_status"], "partial")
        finally:
            temporary.cleanup()

    def test_sanitized_outputs_remove_accounts_paths_reqids_and_secret_fields(self):
        temporary, manifest = self._build(account="55000000")
        try:
            manifest["roots"]["source"] = r"C:\Users\Alice\private\source"
            manifest["models"][0]["performance_panel"] = {
                "status": "not_captured",
                "PASSWORD": "do-not-copy",
                "api_key": "fake-key",
                "api-key-backup": "fake-key-2",
                "nested": {"credentialPath": r"C:\Users\Alice\.credentials.json"},
            }
            sanitized = sanitize_manifest(manifest)
            serialized = json.dumps(sanitized, ensure_ascii=False)
            markdown = render_markdown(sanitized)
            combined = (serialized + markdown).lower()
            self.assertNotIn("55000000", combined)
            self.assertNotIn("alice", combined)
            self.assertNotIn(r"c:\users", combined)
            self.assertNotIn(r"0d:\qmt\python", combined)
            self.assertNotIn("do-not-copy", combined)
            self.assertNotIn("fake-key", combined)
            self.assertNotIn("fake-key-2", combined)
            for sensitive_name in (
                "valid_accounts", "password", "api_key", "token", "secret", "credential"
            ):
                self.assertNotIn(sensitive_name, combined)
            masked = sanitized["models"][0]["log_evidence"]["all_accounts_observed"]
            self.assertEqual(len(masked), 1)
            self.assertRegex(masked[0], r"^ACCOUNT_SHA256_[0-9A-F]{12}$")
            self.assertRegex(
                sanitized["models"][0]["log_evidence"]["latest_run"]["reqid"],
                r"^reqid/[0-9A-F]{16}$",
            )
            self.assertTrue(
                sanitized["models"][0]["source"]["path"].startswith("evidence/")
            )
        finally:
            temporary.cleanup()

    def test_sensitive_source_constants_are_not_copied_to_parameters(self):
        source = SAFE_SOURCE.replace(
            "MODEL_CAPITAL = 100000.0",
            'MODEL_CAPITAL = 100000.0\nBROKER_PASSWORD = "do-not-copy"\nDATA_API_KEY = "fake-key"',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "safe.py"
            path.write_text(source, encoding="utf-8")
            result = analyze_source(path)
        serialized = json.dumps(result["parameters"], ensure_ascii=False).lower()
        self.assertNotIn("password", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("do-not-copy", serialized)
        self.assertNotIn("fake-key", serialized)

    def test_single_symbol_constant_is_counted_as_universe(self):
        source = SAFE_SOURCE.replace(
            'RISK_UNIVERSE = ("510300.SH", "510500.SH")\nDEFENSIVE_SYMBOL = "511010.SH"\nUNIVERSE = RISK_UNIVERSE + (DEFENSIVE_SYMBOL,)',
            'SYMBOL = "510300.SH"',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "single.py"
            path.write_text(source, encoding="utf-8")
            result = analyze_source(path)
        self.assertEqual(result["universe_symbols"], ["510300.SH"])
        self.assertEqual(result["universe_count"], 1)

    def test_tool_source_has_no_qmt_runtime_import_or_order_call(self):
        source = (REPO_ROOT / "scripts" / "build_qmt_native_manifest.py").read_text(encoding="utf-8")
        self.assertNotIn("import xtquant", source)
        self.assertNotIn("from xtquant", source)
        self.assertNotIn("passorder(", source)


if __name__ == "__main__":
    unittest.main()
