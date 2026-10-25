"""Contract tests for the bootstrap-bundle inline workflow scripts."""

import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github/workflows/release.yml"


def heredoc_script(step_name):
    text = WORKFLOW.read_text(encoding="utf-8").split(
        "\n  verify-bootstrap-bundle:\n", 1)[1]
    position = text.index("name: " + step_name)
    block = text[position:]
    match = re.search(r"<<'PY'\n(.*?)\n {10}PY\n", block, re.S)
    assert match, f"no heredoc after step {step_name}"
    return textwrap.dedent(match[1])


def lock_json(revision, module_sha):
    return json.dumps({"api_version": 1, "module_sha256": module_sha,
                       "repository": "warelik/machine-maintenance",
                       "revision": revision, "schema": 1}, indent=2) + "\n"


class LockSelectorTests(unittest.TestCase):
    def run_selector(self, tmp):
        tmp = pathlib.Path(tmp)
        args = [str(tmp / name) for name in (
            "candidate_lock", "workflow_lock", "candidate_loader",
            "workflow_loader", "verifier_lock", "verifier_loader")]
        namespace = {"__name__": "__main__"}
        stdout = io.StringIO()
        with patch.object(sys, "argv", ["-", *args]), redirect_stdout(stdout):
            exec(compile(heredoc_script("Read The Exact Private Maintenance Lock"),
                            "lock_selector", "exec"), namespace)
        return stdout.getvalue()

    def files(self, tmp, *, locks=None, loaders=None):
        tmp = pathlib.Path(tmp)
        locks = locks or (lock_json("b" * 40, "d" * 64),
                          lock_json("b" * 40, "d" * 64),
                          lock_json("a" * 40, "c" * 64))
        (tmp / "candidate_lock").write_text(locks[0])
        (tmp / "workflow_lock").write_text(locks[1])
        (tmp / "verifier_lock").write_text(locks[2])
        loaders = loaders or (b"loader-bytes",) * 3
        (tmp / "candidate_loader").write_bytes(loaders[0])
        (tmp / "workflow_loader").write_bytes(loaders[1])
        (tmp / "verifier_loader").write_bytes(loaders[2])

    def test_new_pair_and_old_verifier_emit_both_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.files(tmp)
            out = self.run_selector(tmp)
        self.assertIn(f"revision={'b' * 40}", out)
        self.assertIn(f"verifier_revision={'a' * 40}", out)

    def test_mismatched_pair_locks_or_loaders_reject(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.files(tmp, locks=(lock_json("b" * 40, "d" * 64),
                                   lock_json("e" * 40, "d" * 64),
                                   lock_json("a" * 40, "c" * 64)))
            with self.assertRaises(SystemExit):
                self.run_selector(tmp)
        for loaders in ((b"one", b"two", b"one"), (b"one", b"one", b"two")):
            with self.subTest(loaders=loaders), tempfile.TemporaryDirectory() as tmp:
                self.files(tmp, loaders=loaders)
                with self.assertRaises(SystemExit):
                    self.run_selector(tmp)


class BundleVerifierTests(unittest.TestCase):
    REPO = "warelik/zed-titlebarless"

    def fixture(self):
        policy = "5" * 40
        full_pr, delta_pr = "123", "124"
        candidate = {"schema": 1, "repository": self.REPO,
                     "configuration_sha": policy, "application_sha": "6" * 40,
                     "upstream_sha": "7" * 40, "patchset_sha256": "8" * 64,
                     "recipe_sha256": "9" * 64, "platform": "macos/aarch64"}
        shared = {"repository": "warelik/machine-maintenance",
                  "revision": "b" * 40, "module_sha256": "d" * 64,
                  "api_version": 1, "dispatcher_sha": "f" * 40}
        manifest = {"shared": shared, "targets": {self.REPO: {
            "candidate": candidate, "full": {"pr": int(full_pr)},
            "delta": {"pr": int(delta_pr), "base_sha": "0" * 40}}}}
        manifest_hash = hashlib.sha256(json.dumps(
            manifest, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        receipt = {"schema": 1, "manifest_sha256": manifest_hash,
                   "manifest": manifest,
                   "private_run": {"id": 41, "run_attempt": 2,
                                   "dispatcher_sha": "f" * 40}}
        comment = {"id": 9001, "created_at": "2026-10-20T12:00:00Z",
                   "updated_at": "2026-10-20T12:00:00Z",
                   "user": {"login": self.REPO.split("/")[0], "type": "User"},
                   "body": ("note <!-- machine-maintenance-bundle:v1 "
                            + json.dumps(receipt, sort_keys=True) + " -->")}
        return policy, full_pr, delta_pr, candidate, shared, manifest, manifest_hash, receipt, comment

    def modules(self, events, *, proof=None, raises=()):
        lock_identity = {"repository": "warelik/machine-maintenance",
                         "revision": "b" * 40, "module_sha256": "d" * 64,
                         "api_version": 1}
        old = SimpleNamespace(
            _bundle_manifest=lambda m: events.append("old_manifest") or m,
            _bundle_manifest_hash=lambda m: events.append("old_hash") or
            hashlib.sha256(json.dumps(m, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode("utf-8")).hexdigest(),
            _bundle_native_pages=lambda read, route: (
                events.append("old_pages") or self._comments),
            _bundle_source_hash=lambda read, shared: (
                events.append("old_source") or (None if "source" not in raises
                                                else (_ for _ in ()).throw(ValueError("bad source")))),
            _bundle_private_run=lambda read, receipt, mh, shared: (
                events.append("old_run") or (
                    (None if "run" not in raises else (_ for _ in ()).throw(ValueError("unsuccessful C7"))),
                    None, 100)),
            _timestamp=lambda value: 200 if value == "2026-10-20T12:00:00Z" else 50,
            validated_bundle=lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("old verifier must not execute")),
            review_history=lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("old history must not execute")))
        proof = proof if proof is not None else {
            "decision": "reuse-policy-validation", "candidate_id": "2" * 64,
            "shared_source": lock_identity, "heavy_build_allowed": False,
            "application_artifact_verified": False}
        new = SimpleNamespace(
            validated_bundle=lambda candidate, comments, read: (
                events.append("new_verified") or proof),
            candidate_identity=lambda candidate: "2" * 64,
            _maintenance_lock_identity=lock_identity)
        calls = []

        def load(lock_path=None):
            root = os.environ["FORK_MAINTENANCE_ROOT"]
            calls.append((root, lock_path))
            events.append("load_verifier" if root.endswith("maintenance-verifier")
                          else "load_new")
            return old if root.endswith("maintenance-verifier") else new

        loader = SimpleNamespace(load=load)
        return old, new, loader, calls

    def run_verifier(self, tmp, loader, *, env_extra=None, api=None, pages=None,
                     mutate=None, lock_override=None):
        tmp = pathlib.Path(tmp)
        policy, full_pr, delta_pr, candidate, shared, manifest, _, receipt, comment = self.fixture()
        if mutate:
            mutate(receipt, comment)
            comment["body"] = ("note <!-- machine-maintenance-bundle:v1 "
                               + json.dumps(receipt, sort_keys=True) + " -->")
        for name in ("gate/scripts", "candidate/scripts",
                     "maintenance-verifier", "maintenance"):
            (tmp / name).mkdir(parents=True, exist_ok=True)
        (tmp / "gate/scripts/maintenance.py").write_text("loader")
        (tmp / "candidate/scripts/maintenance.py").write_text("loader")
        (tmp / "candidate/scripts/maintenance.lock.json").write_text(
            lock_override or lock_json(shared["revision"], shared["module_sha256"]))
        output = tmp / "output"
        self._comments = [comment]
        gate = SimpleNamespace(
            api=api or (lambda route, token=None: {"sha": policy}
                        if route == f"repos/{self.REPO}/commits/downstream" else {}),
            pages=pages or (lambda route: self._comments))
        env = {"GITHUB_REPOSITORY": self.REPO, "GITHUB_SHA": policy,
               "GITHUB_OUTPUT": str(output), "WORKFLOW_SHA": policy,
               "POLICY_REF": policy, "FULL_PR": full_pr, "DELTA_PR": delta_pr,
               "CODEX_REQUEST_TOKEN": "private-token"}
        env.update(env_extra or {})

        def check_output(args, **kwargs):
            if args[:3] == ["git", "-C", "candidate"] or "workflow-source" in args[2]:
                return policy + "\n"
            raise AssertionError(args)

        previous = os.getcwd()
        os.chdir(tmp)
        try:
            with (patch.dict(sys.modules, {"release": gate, "maintenance": loader}),
                  patch.dict(os.environ, env, clear=True),
                  patch.object(sys, "path", list(sys.path)),
                  patch.object(subprocess, "check_output", side_effect=check_output)):
                namespace = {"__name__": "__main__"}
                exec(compile(heredoc_script(
                    "Verify The Complete Native Pair And Private Policy Receipt"),
                    "bundle_verifier", "exec"), namespace)
        finally:
            os.chdir(previous)
        return output.read_text() if output.exists() else ""

    def test_happy_path_outputs_and_call_order(self):
        events = []
        _, _, loader, calls = self.modules(events)
        with tempfile.TemporaryDirectory() as tmp:
            out = self.run_verifier(tmp, loader)
        self.assertIn("verified=true", out)
        self.assertIn("policy_ref=" + "5" * 40, out)
        self.assertEqual(events, ["load_verifier", "old_manifest", "old_hash",
                                  "old_pages", "old_source", "old_run",
                                  "load_new", "new_verified"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0].endswith("maintenance-verifier"))
        self.assertIsNone(calls[0][1])
        self.assertTrue(calls[1][0].endswith("maintenance"))
        self.assertEqual(calls[1][1],
                         pathlib.Path("candidate/scripts/maintenance.lock.json"))

    def test_rejections_never_load_or_run_the_new_verifier(self):
        def wrong_hash(receipt, comment):
            receipt["manifest_sha256"] = "0" * 64

        def edited(receipt, comment):
            comment["updated_at"] = "2026-10-20T12:00:01Z"

        def predating(receipt, comment):
            comment["created_at"] = comment["updated_at"] = "2026-10-19T00:00:00Z"

        cases = [("manifest hash", {"mutate": wrong_hash}),
                 ("edited receipt", {"mutate": edited}),
                 ("receipt predating C7", {"mutate": predating}),
                 ("lock source mismatch", {"lock_override": lock_json("e" * 40, "0" * 64)}),
                 ("bad source", {"raises": ("source",)}),
                 ("unsuccessful C7", {"raises": ("run",)})]
        for name, options in cases:
            events = []
            _, _, loader, _ = self.modules(events, raises=options.get("raises", ()))
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises((SystemExit, ValueError)):
                    self.run_verifier(tmp, loader, mutate=options.get("mutate"),
                                      lock_override=options.get("lock_override"))
            self.assertNotIn("new_verified", events)
            self.assertNotIn("load_new", events)

    def test_new_verifier_errors_and_weak_proofs_do_not_pass(self):
        events = []
        _, new, loader, _ = self.modules(events)
        new.validated_bundle = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("current review ledger changed"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "current review ledger"):
                self.run_verifier(tmp, loader)
        lock_identity = {"repository": "warelik/machine-maintenance",
                         "revision": "b" * 40, "module_sha256": "d" * 64,
                         "api_version": 1}
        for change in ({"shared_source": lock_identity | {"revision": "e" * 40}},
                       {"heavy_build_allowed": True},
                       {"application_artifact_verified": True}):
            events = []
            proof = {"decision": "reuse-policy-validation", "candidate_id": "2" * 64,
                     "shared_source": lock_identity, "heavy_build_allowed": False,
                     "application_artifact_verified": False} | change
            _, _, loader, _ = self.modules(events, proof=proof)
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(SystemExit):
                    self.run_verifier(tmp, loader)


if __name__ == "__main__":
    unittest.main()
