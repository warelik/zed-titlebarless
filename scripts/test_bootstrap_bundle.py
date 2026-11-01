"""Contract tests for the bootstrap-bundle inline workflow scripts."""

import hashlib
import importlib.util
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
import urllib.request
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
            "workflow_loader", "verifier_core")]
        namespace = {"__name__": "__main__"}
        stdout = io.StringIO()
        with patch.object(sys, "argv", ["-", *args]), redirect_stdout(stdout):
            exec(compile(heredoc_script("Read The Exact Private Maintenance Lock"),
                         "lock_selector", "exec"), namespace)
        return stdout.getvalue()

    def files(self, tmp, *, locks=None, loaders=None):
        tmp = pathlib.Path(tmp)
        locks = locks or (lock_json("b" * 40, "d" * 64),
                          lock_json("b" * 40, "d" * 64))
        (tmp / "candidate_lock").write_text(locks[0])
        (tmp / "workflow_lock").write_text(locks[1])
        loaders = loaders or (b"loader-bytes", b"loader-bytes")
        (tmp / "candidate_loader").write_bytes(loaders[0])
        (tmp / "workflow_loader").write_bytes(loaders[1])
        (tmp / "verifier_core").write_bytes(b"API_VERSION = 1\n")

    def test_matching_pair_and_private_verifier_emit_candidate_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.files(tmp)
            out = self.run_selector(tmp)
        self.assertIn(f"revision={'b' * 40}", out)

    def test_mismatched_pair_locks_or_loaders_reject(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.files(tmp, locks=(lock_json("b" * 40, "d" * 64),
                                   lock_json("e" * 40, "d" * 64)))
            with self.assertRaises(SystemExit):
                self.run_selector(tmp)
        with tempfile.TemporaryDirectory() as tmp:
            self.files(tmp, loaders=(b"one", b"two"))
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

    def modules(self, events, *, proof=None, raises=(), candidate_module_bytes=b"API_VERSION = 1\n# candidate\n"):
        candidate_digest = hashlib.sha256(candidate_module_bytes).hexdigest()
        lock_identity = {"repository": "warelik/machine-maintenance",
                         "revision": "b" * 40, "module_sha256": candidate_digest,
                         "api_version": 1}
        old = SimpleNamespace(
            API_VERSION=1,
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
            API_VERSION=1,
            validated_bundle=lambda candidate, comments, read: (
                events.append("new_verified") or proof),
            candidate_identity=lambda candidate: "2" * 64,
            _maintenance_lock_identity=lock_identity)
        return old, new, candidate_module_bytes, candidate_digest

    def run_verifier(self, tmp, events, *, old=None, new=None, env_extra=None,
                     api=None, mutate=None, lock_override=None, raises=(), proof=None):
        tmp = pathlib.Path(tmp)
        built_old, built_new, candidate_bytes, candidate_digest = self.modules(
            events, proof=proof, raises=raises)
        old = old or built_old
        new = new or built_new
        policy, full_pr, delta_pr, candidate, shared, manifest, _, receipt, comment = self.fixture()
        shared["module_sha256"] = candidate_digest
        receipt["manifest_sha256"] = hashlib.sha256(json.dumps(
            manifest, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        comment["body"] = ("note <!-- machine-maintenance-bundle:v1 "
                           + json.dumps(receipt, sort_keys=True) + " -->")
        if mutate:
            mutate(receipt, comment)
            comment["body"] = ("note <!-- machine-maintenance-bundle:v1 "
                               + json.dumps(receipt, sort_keys=True) + " -->")
        for name in ("candidate/scripts", "workflow-source/scripts",
                     "maintenance-verifier", "maintenance"):
            (tmp / name).mkdir(parents=True, exist_ok=True)
        (tmp / "candidate/scripts/maintenance.py").write_text("loader")
        (tmp / "workflow-source/scripts/maintenance.py").write_text("loader")
        (tmp / "candidate/scripts/maintenance.lock.json").write_text(
            lock_override or lock_json(shared["revision"], shared["module_sha256"]))
        (tmp / "maintenance-verifier/.maintenance-source.json").write_text(json.dumps({
            "repository": "warelik/machine-maintenance", "revision": "a" * 40}))
        (tmp / "maintenance-verifier/fork_lifecycle.py").write_bytes(b"API_VERSION = 1\n# verifier\n")
        (tmp / "maintenance/.maintenance-source.json").write_text(json.dumps({
            "repository": "warelik/machine-maintenance", "revision": shared["revision"]}))
        (tmp / "maintenance/fork_lifecycle.py").write_bytes(candidate_bytes)
        output = tmp / "output"
        self._comments = [comment]
        calls = []

        def fake_urlopen(request, timeout=60):
            url = request.full_url if hasattr(request, "full_url") else str(request)
            route = url.removeprefix("https://api.github.com/")
            base_route = route.split("?", 1)[0]
            if api is not None:
                payload = api(route)
            elif base_route == f"repos/{self.REPO}/commits/downstream":
                payload = {"sha": policy}
            elif base_route == f"repos/{self.REPO}/issues/{full_pr}/comments":
                payload = self._comments if "page=1" in route or "page=" not in route else []
            else:
                payload = {}
            raw = json.dumps(payload).encode("utf-8")

            class Resp:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *_a):
                    return False

                def read(self_inner):
                    return raw

            return Resp()

        def fake_spec(name, location):
            path_str = str(location)
            is_verifier = "maintenance-verifier" in path_str
            calls.append(path_str)
            target_ns = old if is_verifier else new

            class Loader:
                def exec_module(self_inner, module):
                    events.append("load_verifier" if is_verifier else "load_new")
                    for attr, val in vars(target_ns).items():
                        setattr(module, attr, val)

            return SimpleNamespace(name=name, loader=Loader())

        env = {"GITHUB_REPOSITORY": self.REPO, "GITHUB_SHA": policy,
               "GITHUB_OUTPUT": str(output), "WORKFLOW_SHA": policy,
               "POLICY_REF": policy, "FULL_PR": full_pr, "DELTA_PR": delta_pr,
               "GH_TOKEN": "public-token", "CODEX_REQUEST_TOKEN": "private-token"}
        env.update(env_extra or {})

        def check_output(args, **kwargs):
            if args[:3] == ["git", "-C", "candidate"] or "workflow-source" in args[2]:
                return policy + "\n"
            raise AssertionError(args)

        previous = os.getcwd()
        os.chdir(tmp)
        try:
            with (patch.dict(os.environ, env, clear=True),
                  patch.object(urllib.request.OpenerDirector, "open", side_effect=fake_urlopen),
                  patch.object(importlib.util, "spec_from_file_location", side_effect=fake_spec),
                  patch.object(importlib.util, "module_from_spec",
                               side_effect=lambda spec: SimpleNamespace(__name__=spec.name)),
                  patch.object(subprocess, "check_output", side_effect=check_output)):
                namespace = {"__name__": "__main__"}
                exec(compile(heredoc_script(
                    "Verify The Complete Native Pair And Private Policy Receipt"),
                    "bundle_verifier", "exec"), namespace)
        finally:
            os.chdir(previous)
        return (output.read_text() if output.exists() else ""), calls

    def test_happy_path_outputs_and_call_order(self):
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            out, calls = self.run_verifier(tmp, events)
        self.assertIn("verified=true", out)
        self.assertIn("policy_ref=" + "5" * 40, out)
        self.assertEqual(events, ["load_verifier", "old_manifest", "old_hash",
                                  "old_pages", "old_source", "old_run",
                                  "load_new", "new_verified"])
        self.assertEqual(len(calls), 2)
        self.assertIn("maintenance-verifier/fork_lifecycle.py", calls[0])
        self.assertIn("maintenance/fork_lifecycle.py", calls[1])

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
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises((SystemExit, ValueError)):
                    self.run_verifier(tmp, events, mutate=options.get("mutate"),
                                      lock_override=options.get("lock_override"),
                                      raises=options.get("raises", ()))
            self.assertNotIn("new_verified", events)
            self.assertNotIn("load_new", events)

    def test_new_verifier_errors_and_weak_proofs_do_not_pass(self):
        events = []
        old_err, _, _, _ = self.modules(events)
        old_err._bundle_manifest = lambda _m: (_ for _ in ()).throw(
            ValueError("manifest schema rejected"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as verifier_exit:
                self.run_verifier(tmp, events, old=old_err)
        self.assertEqual(
            str(verifier_exit.exception),
            "Private checked-bundle verification failed: ValueError: manifest schema rejected",
        )
        self.assertTrue(verifier_exit.exception.__suppress_context__)
        self.assertIsNone(verifier_exit.exception.__cause__)
        self.assertNotIn("fork_lifecycle.py", str(verifier_exit.exception))
        self.assertNotIn("load_new", events)
        self.assertNotIn("new_verified", events)

        events = []
        old, new, _, _ = self.modules(events)
        new.validated_bundle = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("current review ledger changed"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as new_exit:
                self.run_verifier(tmp, events, old=old, new=new)
        self.assertEqual(
            str(new_exit.exception),
            "Private checked-bundle verification failed: RuntimeError: current review ledger changed",
        )
        self.assertTrue(new_exit.exception.__suppress_context__)
        self.assertIsNone(new_exit.exception.__cause__)
        _, _, _, digest = self.modules([])
        lock_identity = {"repository": "warelik/machine-maintenance",
                         "revision": "b" * 40, "module_sha256": digest,
                         "api_version": 1}
        for change in ({"shared_source": lock_identity | {"revision": "e" * 40}},
                       {"heavy_build_allowed": True},
                       {"application_artifact_verified": True}):
            events = []
            proof = {"decision": "reuse-policy-validation", "candidate_id": "2" * 64,
                     "shared_source": lock_identity, "heavy_build_allowed": False,
                     "application_artifact_verified": False} | change
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(SystemExit):
                    self.run_verifier(tmp, events, proof=proof)

    def test_private_root_verifier_and_initial_baseline_manifest_contract(self):
        verifier_job = WORKFLOW.read_text(encoding="utf-8").split(
            "\n  verify-bootstrap-bundle:\n", 1)[1]
        verifier_steps = [
            step for step in re.split(r"(?m)^      - ", verifier_job)[1:]
            if "\n          path: maintenance-verifier\n" in step
        ]
        self.assertEqual(len(verifier_steps), 1)
        verifier_step = verifier_steps[0]
        self.assertIn("repository: warelik/machine-maintenance\n", verifier_step)
        self.assertRegex(verifier_step, r"(?m)^          ref: [0-9a-f]{40}$")
        self.assertIn("persist-credentials: false", verifier_step)
        for forbidden in ("c621547cc93d751d7110791049ee748befdd8831",
                          "ce28dfd224db119b0b6530bc20ceffa7c9a89516",
                          "73aede3a146982a9d47b195f6311cf5bca7410b7",
                          "__PRIVATE_MAINTENANCE_RP_SHA__",
                          "ref: main", "inputs.policy_ref", "github.sha",
                          "github.workflow_sha", "path: gate",
                          "vars.ZED_TRUSTED_POLICY"):
            self.assertNotIn(forbidden, verifier_step)

        def set_point_zero_four_targets(receipt, _comment):
            zed_target = receipt["manifest"]["targets"][self.REPO]
            for repo in ("warelik/zed-titlebarless", "warelik/9router",
                         "warelik/ghidra", "warelik/osconfig"):
                entry = (zed_target if repo == self.REPO else {
                    "candidate": {**zed_target["candidate"], "repository": repo},
                    "full": {"pr": 11},
                    "delta": {"pr": 12, "base_sha": "4" * 40},
                })
                entry["trusted_policy_sha"] = None
                entry["delta"]["accepted_baseline"] = {
                    "endpoint": f"repos/{repo}/actions/variables/FORK_ACCEPTED_BASELINE",
                    "variable": None,
                    "publication_endpoint": f"repos/{repo}/releases",
                    "publications": [],
                }
                receipt["manifest"]["targets"][repo] = entry
            receipt["manifest_sha256"] = hashlib.sha256(json.dumps(
                receipt["manifest"], ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()

        events = []
        with tempfile.TemporaryDirectory() as tmp:
            out, _ = self.run_verifier(tmp, events, mutate=set_point_zero_four_targets)
        self.assertIn("verified=true", out)
        self.assertEqual(events, ["load_verifier", "old_manifest", "old_hash",
                                  "old_pages", "old_source", "old_run",
                                  "load_new", "new_verified"])

    def test_redirect_handler_allows_same_host_https_and_refuses_foreign_or_http(self):
        workflow_text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("urllib.request.urlopen(", workflow_text)
        events = []
        with (tempfile.TemporaryDirectory() as tmp,
              patch.object(urllib.request, "build_opener",
                           wraps=urllib.request.build_opener) as build):
            self.run_verifier(tmp, events)
        handler = build.call_args.args[0]
        request = urllib.request.Request(
            "https://api.github.com/repos/warelik/zed-titlebarless/pulls/1",
            headers={"Authorization": "Bearer fixture-token"},
        )
        same_host = handler.redirect_request(
            request, None, 307, "Temporary Redirect", {},
            "https://api.github.com/repositories/1/pulls/1",
        )
        self.assertEqual(same_host.full_url, "https://api.github.com/repositories/1/pulls/1")
        for target in ("https://elsewhere.invalid/repositories/1/pulls/1",
                       "http://api.github.com/repositories/1/pulls/1"):
            with self.subTest(target=target), self.assertRaisesRegex(
                    ValueError, "authenticated GitHub host"):
                handler.redirect_request(request, None, 302, "Found", {}, target)


if __name__ == "__main__":
    unittest.main()
