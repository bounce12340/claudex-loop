"""Contract tests use real subprocesses and disposable Git repositories, no model calls."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runner", ROOT / "skills/claudex-loop/scripts/runner.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
SESSION = "12345678-1234-4567-8123-123456789abc"
GOOD = {"verdict": "APPROVED", "summary": "The supplied acceptance criteria are consistent.",
        "findings": [], "coverage": ["docs/custom plan.md"], "limitations": []}

FAKE_CLI = r'''
import json, os, pathlib, sys, time
if '--version' in sys.argv:
    print('fake-cli 1.0')
    sys.exit(0)
prompt = sys.stdin.read()
case = os.environ.get('FAKE_CASE', 'ok')
if case == 'timeout':
    time.sleep(30)
if case == 'exit':
    print('Authentication failed', file=sys.stderr)
    sys.exit(7)
if case == 'empty':
    sys.exit(0)
if case == 'mutate_plan':
    pathlib.Path(os.environ['FAKE_PLAN']).write_text('Changed after launch')
if case == 'mutate_code':
    pathlib.Path('new.py').write_text('changed during inspection')
if case == 'build':
    pathlib.Path('built.py').write_text('print(42)\n')
session = '12345678-1234-4567-8123-123456789abc'
if case == 'wrong_session':
    session = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
review = {'verdict':'APPROVED', 'summary':'Inspected supplied plan.',
          'findings':[], 'coverage':['custom plan.md'], 'limitations':[]}
if case == 'revise':
    review.update(verdict='REVISE', findings=[{'id':'R1','severity':'high','path':'plan',
                  'evidence':'Deletion before successful copy loses the only copy.',
                  'fix':'Verify the new copy before removing the old one.'}])
if case == 'blocked':
    review.update(verdict='BLOCKED', coverage=[], limitations=['Required schema unavailable.'])
if case == 'malformed':
    review = {'verdict':'APPROVED'}
if 'exec' in sys.argv:
    output = pathlib.Path(sys.argv[sys.argv.index('-o')+1])
    output.write_text('Built; proof passed.' if case == 'build' else json.dumps(review))
    print(json.dumps({'type':'thread.started', 'thread_id':session}))
    if case == 'turn_failed':
        print(json.dumps({'type':'turn.failed', 'error':{'message':'quota'}}))
    elif case != 'incomplete':
        print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
else:
    value = {'type':'result','subtype':'success','is_error':False,'session_id':session,
             'structured_output':review, 'result':'Built; proof passed.',
             'modelUsage':{'claude-test':{'inputTokens':10}},'usage':{'input_tokens':10}}
    if case == 'turn_failed':
        value.update(subtype='error_during_execution',is_error=True)
    print(json.dumps([{'type':'system','subtype':'init'}, value] if case == 'array' else value))
'''


FAKE_GROK_CLI = r'''
import json, os, pathlib, sys, time
if '--version' in sys.argv:
    print('fake-grok 1.0')
    sys.exit(0)
case = os.environ.get('FAKE_CASE', 'ok')
if case == 'timeout':
    time.sleep(30)
session = '12345678-1234-4567-8123-123456789abc'
if case == 'wrong_session':
    session = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
review = {'verdict':'APPROVED', 'summary':'Inspected supplied plan.',
          'findings':[], 'coverage':['custom plan.md'], 'limitations':[]}
if case == 'revise':
    review.update(verdict='REVISE', findings=[{'id':'R1','severity':'high','path':'plan',
                  'evidence':'Deletion before successful copy loses the only copy.',
                  'fix':'Verify the new copy before removing the old one.'}])
if case == 'blocked':
    review.update(verdict='BLOCKED', coverage=[], limitations=['Required schema unavailable.'])
if case == 'build':
    pathlib.Path('built.py').write_text('print(42)\n')
    text = 'Built; proof passed.'
elif case == 'malformed':
    text, structured = 'not json at all', None
else:
    text, structured = json.dumps(review), review
if case == 'multiturn':
    # Real multi-turn shape: per-turn schema JSON concatenated in text (intermediates first,
    # final last), and the authoritative result in structuredOutput (absent here to test fallback).
    text = json.dumps({'verdict': 'BLOCKED', 'summary': 'turn',
                       'findings': [], 'coverage': ['x'], 'limitations': []}) + json.dumps(review)
    structured = None
if case == 'exit':
    print('Authentication failed', file=sys.stderr)
    sys.exit(7)
if case == 'empty':
    sys.exit(0)
if case == 'mutate_plan':
    pathlib.Path(os.environ['FAKE_PLAN']).write_text('Changed after launch')
if case == 'mutate_code':
    pathlib.Path('new.py').write_text('changed during inspection')
if case == 'turn_failed':
    envelope_text, stop = '', 'max_turns'
else:
    envelope_text, stop = text, 'end_turn'
print(json.dumps({'text': envelope_text, 'structuredOutput': structured,
                  'stopReason': stop, 'sessionId': session,
                  'modelUsage': {'grok-test': {'inputTokens': 10}},
                  'usage': {'input_tokens': 10}, 'total_cost_usd': 0.01,
                  'num_turns': 2 if case == 'multiturn' else 1}))
'''

class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claudex-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        self.plan = self.root / "custom plan.md"
        self.plan.write_text("# Work order\nKeep the original until the copy is verified.\n", encoding="utf-8")
        self.artifacts = self.root / "runs"
        self.cli = self.root / "fake_cli.py"
        self.cli.write_text(FAKE_CLI)
        self.grok_cli = self.root / "fake_grok_cli.py"
        self.grok_cli.write_text(FAKE_GROK_CLI)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "existing.py").write_text("original\n")
        (self.repo / "delete.py").write_text("delete me\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, stderr=subprocess.PIPE).decode()

    def invoke(self, host="claude", mode="review", case="ok", extra=(), fake="claude"):
        args = [mode, "--host", host, "--repo", str(self.repo), "--plan", str(self.plan),
                "--artifacts", str(self.artifacts), *extra]
        old = set(self.artifacts.glob("*/result.json")) if self.artifacts.exists() else set()
        output, error = io.StringIO(), io.StringIO()
        fake_path = self.grok_cli if fake == "grok" else self.cli
        with patch.object(runner, "cli_prefix", return_value=[sys.executable, str(fake_path)]), \
             patch.dict(os.environ, {"FAKE_CASE": case, "FAKE_PLAN": str(self.plan)}), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = runner.main(args)
        new = set(self.artifacts.glob("*/result.json")) - old if self.artifacts.exists() else set()
        path = next(iter(new)) if new else None
        return code, json.loads(path.read_text()) if path else None, path, error.getvalue()

    def test_host_role_defaults_and_builder_override(self):
        self.assertEqual(runner.resolve_roles("claude")["reviewer"], "codex")
        self.assertEqual(runner.resolve_roles("codex")["reviewer"], "claude")
        roles = runner.resolve_roles("codex", builder="claude")
        self.assertEqual((roles["planner"], roles["builder"], roles["inspector"]), ("codex", "claude", "codex"))
        with self.assertRaises(runner.RunError):
            runner.resolve_roles("codex", "codex")

    def test_three_provider_roles_grok_optin_and_defaults_unchanged(self):
        self.assertEqual(runner.resolve_roles("claude", reviewer="grok")["reviewer"], "grok")
        roles = runner.resolve_roles("claude", builder="grok")
        self.assertEqual((roles["builder"], roles["inspector"]), ("grok", "claude"))
        roles = runner.resolve_roles("codex", builder="grok")
        self.assertEqual(roles["inspector"], "claude")
        with self.assertRaises(runner.RunError):
            runner.resolve_roles("grok", reviewer="grok")
        self.assertEqual(runner.resolve_roles("claude")["reviewer"], "codex")

    def test_registry_dispatch_and_unknown_provider(self):
        self.assertEqual(runner.command("grok", "review", self.root)[0], "--prompt-file")
        self.assertEqual(runner.command("claude", "review", self.root)[0], "-p")
        self.assertEqual(runner.command("codex", "review", self.root)[0], "exec")
        with self.assertRaises(runner.RunError):
            runner.provider_adapter("gemini")

    def test_coordinator_host_roles_and_defaults(self):
        roles = runner.resolve_roles("hermes")
        self.assertEqual((roles["planner"], roles["reviewer"], roles["builder"], roles["inspector"]),
                         ("hermes", "claude", "codex", "claude"))
        self.assertEqual(runner.resolve_roles("hermes", builder="claude")["inspector"], "codex")
        self.assertEqual(runner.resolve_roles("hermes", builder="grok")["inspector"], "claude")
        self.assertEqual(runner.resolve_roles("claude")["reviewer"], "codex")

    def test_coordinator_host_end_to_end_review(self):
        code, record, path, _ = self.invoke(host="hermes")
        self.assertEqual(code, 0, record)
        self.assertEqual(record["provider"], "claude")
        self.assertEqual(record["roles"]["planner"], "hermes")
        self.assertEqual(record["plan_sha256"], runner.digest(self.plan.read_bytes()))
        self.assertEqual(record["response"]["verdict"], "APPROVED")

    def test_coordinator_host_cannot_review_or_build_as_itself(self):
        # argparse choices reject a coordinator host as provider/builder (SystemExit before run()).
        with self.assertRaises(SystemExit):
            self.invoke(host="hermes", extra=("--provider", "hermes"))
        with self.assertRaises(SystemExit):
            self.invoke(host="hermes", mode="build",
                        extra=("--builder", "hermes", "--unreviewed-spec", "--proof", "true"))

    def test_both_review_adapters_complete_and_bind_custom_plan(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                code, record, path, _ = self.invoke(host)
                self.assertEqual(code, 0, record)
                self.assertEqual(record["session_id"], SESSION)
                self.assertEqual(record["plan"], str(self.plan))
                self.assertEqual(record["plan_sha256"], runner.digest(self.plan.read_bytes()))
                self.assertIn(str(self.plan), (path.parent / "prompt.txt").read_text())
                self.assertEqual(record["response"]["verdict"], "APPROVED")

    def test_unpinned_and_explicit_model_selection(self):
        for provider in runner.PROVIDERS:
            args = runner.command(provider, "review", self.root)
            self.assertNotIn("--model", args)
            self.assertNotIn("-m", args)
            pinned = runner.command(provider, "review", self.root, "chosen-model", "high")
            self.assertIn("chosen-model", pinned)

    def test_claude_exposes_only_read_tools_and_no_mcp(self):
        args = runner.command("claude", "review", self.root)
        self.assertEqual(args[args.index("--tools")+1], "Read,Glob,Grep")
        self.assertIn("--safe-mode", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--permission-mode")+1], "dontAsk")

    def test_codex_resume_keeps_read_only_and_explicit_session(self):
        args = runner.command("codex", "review", self.root, session=SESSION)
        self.assertEqual(args[:3], ["exec", "resume", SESSION])
        self.assertIn('sandbox_mode="read-only"', args)
        self.assertNotIn("-s", args)
        self.assertNotIn("--last", args)

    def test_failures_never_approve_and_keep_diagnostics(self):
        for host in ("claude", "codex"):
            for case in ("exit", "empty", "malformed", "turn_failed"):
                with self.subTest(host=host, case=case):
                    code, record, path, _ = self.invoke(host, case=case)
                    self.assertEqual(code, 1)
                    self.assertEqual(record["status"], "failed")
                    self.assertTrue((path.parent / "stderr.txt").exists())
                    if case == "exit":
                        self.assertIn("Authentication failed", (path.parent / "stderr.txt").read_text())

    def test_missing_codex_completion_is_failure(self):
        code, record, _, _ = self.invoke(case="incomplete")
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "failed")

    def test_claude_array_envelope(self):
        code, record, _, _ = self.invoke("codex", case="array")
        self.assertEqual(code, 0)
        self.assertEqual(record["observed_models"], ["claude-test"])

    def test_revise_and_blocked_are_completed_but_not_approval(self):
        for case in ("revise", "blocked"):
            code, record, _, _ = self.invoke(case=case)
            self.assertEqual(code, 0)
            with self.assertRaises(runner.RunError):
                runner.check_approval(record, self.plan, self.repo)

    def test_empty_findings_allowed_but_contradictory_approval_rejected(self):
        runner.validate_review(copy.deepcopy(GOOD))
        value = copy.deepcopy(GOOD)
        value["findings"] = [{"id":"1", "severity":"high", "path":"plan", "evidence":"data loss", "fix":"retain copy"}]
        with self.assertRaises(runner.RunError):
            runner.validate_review(value)

    def test_changed_plan_invalidates_approval(self):
        _, record, _, _ = self.invoke()
        runner.check_approval(record, self.plan, self.repo)
        self.plan.write_text("Different requirements")
        with self.assertRaises(runner.RunError):
            runner.check_approval(record, self.plan, self.repo)

    def test_changed_plan_during_review_fails(self):
        code, record, _, _ = self.invoke(case="mutate_plan")
        self.assertEqual(code, 1)
        self.assertIn("changed during", record["error"])

    def test_resume_revised_plan_same_session(self):
        _, _, previous, _ = self.invoke(case="revise")
        self.plan.write_text("New revision")
        code, record, _, _ = self.invoke(extra=("--resume", str(previous)))
        self.assertEqual(code, 0, record)
        self.assertEqual(record["session_id"], SESSION)

    def test_wrong_session_is_refused(self):
        _, _, previous, _ = self.invoke()
        code, record, _, _ = self.invoke(case="wrong_session", extra=("--resume", str(previous)))
        self.assertEqual(code, 1)
        self.assertIn("different session", record["error"])

    def test_resume_wrong_provider_or_model_rejected_before_launch(self):
        _, _, previous, _ = self.invoke()
        code, record, _, error = self.invoke("codex", extra=("--resume", str(previous)))
        self.assertEqual(code, 1)
        self.assertIsNone(record)
        self.assertIn("provider", error)
        code, record, _, error = self.invoke(extra=("--resume", str(previous), "--model", "new-model"))
        self.assertEqual(code, 1)
        self.assertIsNone(record)
        self.assertIn("requested_model", error)

    def test_timeout_records_failure(self):
        code, record, _, _ = self.invoke(case="timeout", extra=("--timeout", "1"))
        self.assertEqual(code, 1)
        self.assertIn("timed out", record["error"])

    def test_unique_artifacts_and_failed_round_does_not_reuse_reply(self):
        _, _, first, _ = self.invoke()
        code, record, second, _ = self.invoke(case="empty")
        self.assertNotEqual(first, second)
        self.assertEqual(code, 1)
        self.assertNotIn("response", record)

    def test_snapshot_covers_staged_unstaged_deleted_and_new_files(self):
        (self.repo / "existing.py").write_text("staged version\n")
        self.git("add", "existing.py")
        (self.repo / "existing.py").write_text("unstaged final version\n")
        (self.repo / "delete.py").unlink()
        (self.repo / "new.py").write_text("brand new\n")
        snap = runner.snapshot(self.repo, self.base)
        self.assertEqual({f["path"] for f in snap["files"]}, {"existing.py", "delete.py", "new.py"})
        self.assertEqual(next(f for f in snap["files"] if f["path"] == "delete.py")["kind"], "deleted")
        self.assertEqual(next(f for f in snap["files"] if f["path"] == "existing.py")["sha256"],
                         runner.digest((self.repo / "existing.py").read_bytes()))

    def test_inspection_requires_other_provider_and_fresh_session(self):
        code, _, _, error = self.invoke(mode="inspect", extra=("--base", self.base, "--provider", "claude"))
        self.assertEqual(code, 1)
        self.assertIn("opposite the builder", error)
        _, _, previous, _ = self.invoke()
        code, _, _, error = self.invoke(mode="inspect", extra=("--base", self.base, "--resume", str(previous)))
        self.assertEqual(code, 1)
        self.assertIn("fresh session", error)

    def test_changed_code_during_inspection_fails(self):
        (self.repo / "new.py").write_text("original new file")
        code, record, _, _ = self.invoke(mode="inspect", case="mutate_code", extra=("--base", self.base))
        self.assertEqual(code, 1)
        self.assertIn("Code changed", record["error"])

    def test_build_requires_explicit_review_override_and_clean_tree(self):
        code, _, _, error = self.invoke(mode="build", extra=("--proof", "python -m unittest"))
        self.assertEqual(code, 1)
        self.assertIn("--approval", error)
        (self.repo / "user_work.py").write_text("preserve me")
        code, _, _, error = self.invoke(mode="build", extra=("--unreviewed-spec", "--proof", "test"))
        self.assertEqual(code, 1)
        self.assertIn("clean checkout", error)
        self.assertEqual((self.repo / "user_work.py").read_text(), "preserve me")

    def test_build_resume_keeps_initial_baseline_and_existing_build_changes(self):
        extra = ("--builder", "codex", "--unreviewed-spec", "--proof", "python -m unittest")
        code, record, path, _ = self.invoke(mode="build", case="build", extra=extra)
        self.assertEqual(code, 0, record)
        self.assertEqual(record["base"], self.base)
        code, record, _, _ = self.invoke(mode="build", case="build", extra=extra+("--resume", str(path)))
        self.assertEqual(code, 0, record)
        self.assertEqual(record["base"], self.base)
        (self.repo / "user_work.py").write_text("intervening edit")
        code, _, _, error = self.invoke(mode="build", case="build", extra=extra+("--resume", str(path)))
        self.assertEqual(code, 1)
        self.assertIn("Checkout changed", error)

    def test_artifacts_cannot_contaminate_target_checkout(self):
        code, _, _, error = self.invoke(extra=("--artifacts", str(self.repo / "runs")))
        self.assertEqual(code, 1)
        self.assertIn("outside", error)

    def test_grok_review_flags_plan_mode_and_schema(self):
        args = runner.command("grok", "review", self.root)
        self.assertEqual(args[args.index("--permission-mode") + 1], "plan")
        self.assertIn("--json-schema", args)
        self.assertEqual(json.loads(args[args.index("--json-schema") + 1]), runner.REVIEW_SCHEMA)
        self.assertIn("--no-subagents", args)
        self.assertIn("--disable-web-search", args)
        self.assertEqual(args[args.index("--tools") + 1], "read,glob,grep")
        self.assertNotIn("--sandbox", args)

    def test_grok_review_completes_and_binds_plan(self):
        code, record, path, _ = self.invoke(fake="grok", extra=("--provider", "grok"))
        self.assertEqual(code, 0, record)
        self.assertEqual(record["session_id"], SESSION)
        self.assertEqual(record["provider"], "grok")
        self.assertEqual(record["plan_sha256"], runner.digest(self.plan.read_bytes()))
        self.assertEqual(record["response"]["verdict"], "APPROVED")
        self.assertEqual(record["observed_models"], ["grok-test"])
        self.assertEqual(record["total_cost_usd"], 0.01)
        prompt = (path.parent / "prompt.txt").read_text()
        self.assertIn(str(self.plan), prompt)

    def test_grok_review_failures_never_approve(self):
        for case in ("exit", "empty", "malformed", "turn_failed"):
            with self.subTest(case=case):
                code, record, path, _ = self.invoke(fake="grok", case=case, extra=("--provider", "grok"))
                self.assertEqual(code, 1)
                self.assertEqual(record["status"], "failed")
                self.assertTrue((path.parent / "stderr.txt").exists())
                if case == "exit":
                    self.assertIn("Authentication failed", (path.parent / "stderr.txt").read_text())

    def test_grok_revise_and_blocked_complete_but_are_not_approval(self):
        for case in ("revise", "blocked"):
            code, record, _, _ = self.invoke(fake="grok", case=case, extra=("--provider", "grok"))
            self.assertEqual(code, 0)
            with self.assertRaises(runner.RunError):
                runner.check_approval(record, self.plan, self.repo)

    def test_grok_resume_keeps_same_session(self):
        _, _, previous, _ = self.invoke(fake="grok", case="revise", extra=("--provider", "grok"))
        self.plan.write_text("New revision")
        code, record, _, _ = self.invoke(fake="grok", extra=("--provider", "grok", "--resume", str(previous)))
        self.assertEqual(code, 0, record)
        self.assertEqual(record["session_id"], SESSION)
        args = runner.command("grok", "review", self.root, session=SESSION)
        self.assertIn("--resume", args)
        self.assertIn(SESSION, args)

    def test_grok_multiturn_concatenated_text_falls_back_to_last_object(self):
        code, record, _, _ = self.invoke(fake="grok", case="multiturn", extra=("--provider", "grok"))
        self.assertEqual(code, 0, record)
        self.assertEqual(record["response"]["verdict"], "APPROVED")
        self.assertEqual(record["num_turns"], 2)

    def test_grok_inspector_role_forbidden_when_builder_is_grok(self):
        roles = runner.resolve_roles("claude", builder="grok")
        self.assertEqual(roles["inspector"], "claude")
        code, _, _, error = self.invoke(mode="inspect",
                                        extra=("--base", self.base, "--builder", "grok", "--provider", "grok"))
        self.assertEqual(code, 1)
        self.assertIn("opposite the builder", error)


if __name__ == "__main__":
    unittest.main()
