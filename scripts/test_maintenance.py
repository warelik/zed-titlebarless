"""Public adapter boundary checks; private lifecycle decisions have their own suite."""

from contextlib import ExitStack
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import maintenance
import release


class PrivateMaintenanceLoaderTests(unittest.TestCase):
    def fixture(self, root, source=b"API_VERSION = 1\n"):
        directory = root / "private"
        directory.mkdir()
        (directory / "fork_lifecycle.py").write_bytes(source)
        receipt = {"repository": "warelik/machine-maintenance", "revision": "a" * 40}
        (directory / ".maintenance-source.json").write_text(json.dumps(receipt))
        lock = {"schema": 1, **receipt, "module_sha256": hashlib.sha256(source).hexdigest(),
                "api_version": 1}
        path = root / "maintenance.lock.json"
        path.write_text(json.dumps(lock))
        return directory, path, lock

    def test_exact_private_source_loads_without_copying_it_into_the_public_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory, path, _ = self.fixture(Path(temporary))
            with patch.dict(os.environ, {"FORK_MAINTENANCE_ROOT": str(directory)}):
                self.assertEqual(maintenance.load(path).API_VERSION, 1)
            self.assertEqual(set(Path(temporary).iterdir()), {directory, path})

    def test_wrong_hash_refuses_execution_of_the_private_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory, path, _ = self.fixture(Path(temporary))
            (directory / "fork_lifecycle.py").write_text("raise AssertionError('must never execute')\n")
            with (patch.dict(os.environ, {"FORK_MAINTENANCE_ROOT": str(directory)}),
                  self.assertRaisesRegex(ValueError, "checksum")):
                maintenance.load(path)

    def test_wrong_acquisition_revision_cannot_execute_the_module(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory, path, _ = self.fixture(Path(temporary))
            (directory / ".maintenance-source.json").write_text(json.dumps({
                "repository": "warelik/machine-maintenance", "revision": "b" * 40}))
            with (patch.dict(os.environ, {"FORK_MAINTENANCE_ROOT": str(directory)}),
                  self.assertRaisesRegex(ValueError, "revision")):
                maintenance.load(path)

    def test_symlink_or_missing_private_source_is_not_a_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory, path, _ = self.fixture(root)
            alias = root / "alias"
            alias.symlink_to(directory, target_is_directory=True)
            for environment in ({}, {"FORK_MAINTENANCE_ROOT": str(alias)}):
                with (self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True),
                      self.assertRaises(ValueError)):
                    maintenance.load(path)

    def test_lock_identity_and_api_are_checked_before_use(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory, path, lock = self.fixture(Path(temporary))
            for change in ({"repository": "another/private"}, {"revision": "main"},
                           {"api_version": True}, {"schema": True}, {"extra": "ignored"}):
                path.write_text(json.dumps({**lock, **change}))
                with (self.subTest(change=change),
                      patch.dict(os.environ, {"FORK_MAINTENANCE_ROOT": str(directory)}),
                      self.assertRaises(ValueError)):
                    maintenance.load(path)


class NativeBaselinePublicationTests(unittest.TestCase):
    def test_native_checkpoint_uses_owner_patch_only_after_verified_publication(self):
        repo, sha, upstream, config, patchset = "owner/zed", "a" * 40, "b" * 40, "c" * 40, "d" * 64
        endpoint = f"repos/{repo}/actions/variables/FORK_ACCEPTED_BASELINE"
        metadata = {"repository": repo, "release_id": 9, "config_sha": config,
                    "candidate_sha": sha, "upstream_sha": upstream,
                    "patchset_sha256": patchset, "version": "1.0.0"}
        raw = json.dumps(metadata).encode()
        with patch.object(release, "product_recipe_sha256", return_value="e" * 64):
            candidate = release.maintenance_candidate(repo, config, sha, upstream, patchset)
        publication = {"kind": "github-release", "id": 9,
                       "published_at": "2026-10-19T12:00:00Z",
                       "metadata_sha256": hashlib.sha256(raw).hexdigest()}
        value = {"schema": 1, "repository": repo, "candidate": candidate, "publication": publication}
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        old = {"name": "FORK_ACCEPTED_BASELINE", "value": "old checkpoint",
               "created_at": "2026-10-18T00:00:00Z", "updated_at": "2026-10-18T00:00:00Z"}
        proof = {"endpoint": endpoint, "variable": {**old, "value": encoded,
                 "updated_at": "2026-10-19T12:01:00Z"}}
        verified, updates = [], []

        def native_api(route, *, method="GET", data=None, token=None):
            if route == f"repos/{repo}/releases/9":
                return {"draft": False, "prerelease": False,
                        "assets": [{"name": "provenance.json"}],
                        "published_at": publication["published_at"]}
            self.assertEqual(route, endpoint)
            self.assertEqual(token, "checked-owner-token")
            if method == "GET":
                return old
            self.assertEqual(method, "PATCH")
            self.assertEqual(verified, [True])
            self.assertEqual(data, {"name": "FORK_ACCEPTED_BASELINE", "value": encoded})
            updates.append(data)

        environment = {"CONFIG_SHA": config, "REVIEW_CONFIG_SHA": config,
                       "CANDIDATE_SHA": sha, "UPSTREAM_SHA": upstream,
                       "PATCHSET_SHA256": patchset,
                       "MAINTENANCE_CANDIDATE": json.dumps(candidate),
                       "MAINTENANCE_BASELINE": json.dumps({"endpoint": endpoint, "variable": old})}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(release, "owner_token", return_value="checked-owner-token"), \
                patch.object(release, "api", side_effect=native_api), \
                patch.object(release, "asset_bytes", return_value=raw), \
                patch.object(release, "verify_published_release", side_effect=lambda *args: verified.append(True)), \
                patch.object(release, "check_shared_publication"), \
                patch.object(maintenance, "load", return_value=Mock()), \
                patch.object(release, "accepted_baseline", return_value=(proof, candidate, publication)):
            self.assertEqual(release.advance_published_baseline(repo, 9), proof)
        self.assertEqual(len(updates), 1)


    def test_failed_checkpoint_publication_withdraws_and_restores_only_its_own_checkpoint(self):
        repo = "owner/zed"
        candidate = {"schema": 1, "repository": repo, "application_sha": "a" * 40,
                     "configuration_sha": "b" * 40, "upstream_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        endpoint = f"repos/{repo}/actions/variables/FORK_ACCEPTED_BASELINE"
        previous = {"name": "FORK_ACCEPTED_BASELINE", "value": "verified previous checkpoint",
                    "created_at": "2026-10-18T00:00:00Z", "updated_at": "2026-10-18T00:00:00Z"}
        publication = {"kind": "github-release", "id": 9, "published_at": "2026-10-20T12:00:00Z",
                       "metadata_sha256": "f" * 64}
        encoded = json.dumps({"schema": 1, "repository": repo, "candidate": candidate,
                              "publication": publication}, sort_keys=True, separators=(",", ":"))
        env = {"MAINTENANCE_BASELINE": json.dumps({"endpoint": endpoint, "variable": previous}),
               "MAINTENANCE_CANDIDATE": json.dumps(candidate),
               "CONFIG_SHA": candidate["configuration_sha"]}
        for committed, restore_failure in ((False, None), (True, None),
                                           (True, "before"), (True, "after")):
            with self.subTest(checkpoint_write_committed=committed, restore_failure=restore_failure):
                current = dict(previous)
                events = []
                draft = True
                restore_writes = 0

                def native(route, *, method="GET", data=None, token=None):
                    nonlocal draft, restore_writes
                    if route == f"repos/{repo}/releases/9":
                        if method == "PATCH":
                            draft = data["draft"]
                            events.append(("release", draft))
                            return {}
                        return {"id": 9, "draft": draft, "tag_name": "verified-tag"}
                    self.assertEqual(route, endpoint)
                    self.assertEqual(token, "owner-token")
                    if method == "PATCH":
                        restore_writes += 1
                        events.append(("checkpoint", data["value"]))
                        if restore_writes == 1 and restore_failure == "before":
                            raise RuntimeError("restore request timed out before commit")
                        current["value"] = data["value"]
                        current["updated_at"] = "2026-10-20T12:00:01Z"
                        if restore_writes == 1 and restore_failure == "after":
                            raise RuntimeError("restore response lost after commit")
                        return {}
                    return dict(current)

                def advance():
                    if committed:
                        current["value"] = encoded
                    raise RuntimeError("lost checkpoint response")

                def validate(published):
                    if published:
                        advance()

                with (patch.dict(os.environ, env, clear=True),
                      patch.object(release, "owner_token", return_value="owner-token"),
                      patch.object(maintenance, "load", return_value=Mock()),
                      patch.object(release, "api", side_effect=native),
                      self.assertRaisesRegex(RuntimeError, "lost checkpoint response")):
                    release.publish_checked_draft(repo, 9, "notes", validate,
                                                  tag="verified-tag")
                self.assertTrue(draft)
                self.assertEqual(current["value"], previous["value"])
                self.assertEqual(events[:2], [("release", False), ("release", True)])
                expected_writes = (2 if restore_failure == "before" else 1) if committed else 0
                self.assertEqual(restore_writes, expected_writes)
                self.assertEqual(events[2:], [("checkpoint", previous["value"])] * expected_writes)

    def test_checkpoint_recovery_refuses_a_concurrent_publication(self):
        repo = "owner/zed"
        candidate = {"schema": 1, "repository": repo, "application_sha": "a" * 40,
                     "configuration_sha": "b" * 40, "upstream_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        endpoint = f"repos/{repo}/actions/variables/FORK_ACCEPTED_BASELINE"
        current = {"name": "FORK_ACCEPTED_BASELINE", "created_at": "2026-10-20T00:00:00Z",
                   "updated_at": "2026-10-20T00:00:00Z",
                   "value": json.dumps({"schema": 1, "repository": repo,
                       "candidate": {**candidate, "application_sha": "9" * 40},
                       "publication": {"kind": "github-release", "id": 10,
                           "published_at": "2026-10-20T12:00:00Z", "metadata_sha256": "f" * 64}})}
        env = {"MAINTENANCE_CANDIDATE": json.dumps(candidate),
               "MAINTENANCE_BASELINE": json.dumps({"endpoint": endpoint,
                   "variable": {"name": "FORK_ACCEPTED_BASELINE", "value": "previous"}}),
               "CONFIG_SHA": candidate["configuration_sha"]}
        with (patch.dict(os.environ, env, clear=True),
              patch.object(release, "owner_token", return_value="owner-token"),
              patch.object(maintenance, "load", return_value=Mock()),
              patch.object(release, "api", return_value=current) as native,
              self.assertRaisesRegex(ValueError, "concurrently changed checkpoint")):
            release.restore_publication_baseline(repo, 9)
        native.assert_called_once_with(endpoint, token="owner-token")



class SharedZedReviewTests(unittest.TestCase):
    def test_dismissed_review_passes_native_dismissal_events_without_inventing_its_time(self):
        repo, source = "owner/zed", "c" * 40
        candidate = {"repository": repo, "application_sha": "b" * 40,
                     "configuration_sha": "a" * 40}
        request = {"id": 7, "user": {"login": "owner", "type": "User"},
                   "created_at": "2026-10-19T09:00:00Z", "updated_at": "2026-10-19T09:00:00Z",
                   "body": "exact fixture request"}
        event = {"id": 72, "event": "review_dismissed", "dismissed_review": {"review_id": 44},
                 "created_at": "2026-10-19T10:02:00Z",
                 "url": f"https://api.github.com/repos/{repo}/issues/events/72"}
        for state in ("APPROVED", "DISMISSED"):
            with self.subTest(native_review_state=state):
                reviews = [{"id": 44, "state": state, "commit_id": source,
                            "submitted_at": "2026-10-19T10:00:00Z"}]

                def native_pages(route):
                    if route == f"repos/{repo}/issues/7/comments":
                        return [request]
                    if route == f"repos/{repo}/pulls/7/reviews":
                        return reviews
                    if route == f"repos/{repo}/pulls/7/comments":
                        return []
                    if state == "DISMISSED" and route == f"repos/{repo}/issues/7/events":
                        return [event]
                    self.fail(f"Unexpected native event read: {route}")

                def native_api(route):
                    if route == f"repos/{repo}/pulls/7":
                        return {"head": {"ref": f"review/{source}"}}
                    if route.startswith(f"repos/{repo}/git/commits/"):
                        return {"sha": route.rsplit("/", 1)[1], "tree": {"sha": "e" * 40}}
                    self.fail(f"Unexpected native source read: {route}")

                module = Mock()
                with (patch.object(release.maintenance, "load", return_value=module),
                      patch.object(release, "maintenance_marker", return_value=request["body"]),
                      patch.object(release, "api", side_effect=native_api),
                      patch.object(release, "pages", side_effect=native_pages)):
                    release.maintenance_review(candidate, "full", 7, source, "d" * 40)
                target = module.evaluate_review.call_args.args[0]
                self.assertEqual(target["review_dismissals"], [event] if state == "DISMISSED" else [])

    def test_native_bundle_reuse_remains_bound_to_the_public_loader_lock(self):
        candidate = {"repository": "owner/zed"}
        comments = [{"id": 7}]
        lock_identity = {"repository": "warelik/machine-maintenance", "revision": "a" * 40,
                         "module_sha256": "b" * 64, "api_version": 1}
        private = type("PrivateLifecycle", (), {})()
        private._maintenance_lock_identity = lock_identity
        private.candidate_identity = lambda _candidate: "c" * 64
        verifier = Mock(return_value={"decision": "reuse-policy-validation", "candidate_id": "c" * 64,
                                      "heavy_build_allowed": False,
                                      "application_artifact_verified": False,
                                      "shared_source": lock_identity})
        private.validated_bundle = verifier
        with patch.object(release, "owner_token", return_value="fixture-owner"), \
                patch.object(release, "pages", return_value=comments), \
                patch.object(release, "api", return_value={}) as api, \
                patch.object(release.maintenance, "load", return_value=private):
            self.assertTrue(release.verified_policy_bundle(candidate, 91))
            verifier.call_args.args[2]("repos/owner/zed/pulls/91")
        self.assertEqual(verifier.call_args.args[:2], (candidate, comments))
        api.assert_called_once_with("repos/owner/zed/pulls/91", token="fixture-owner")

    def test_verified_closed_delta_is_reused_without_reopening_or_another_request(self):
        repo, head, base = "owner/zed", "a" * 40, "b" * 40
        candidate = {"configuration_sha": head}
        proof = {"variable": {"value": json.dumps({"candidate": {"configuration_sha": base},
                    "publication": {"published_at": "2026-10-19T00:00:00Z"}})}}
        pull = {"number": 91, "state": "closed",
                "head": {"sha": head, "ref": f"review/changes-{head}", "repo": {"full_name": repo}},
                "base": {"sha": base, "ref": f"review-base/{base}", "repo": {"full_name": repo}}}
        comments = [{"id": 92, "user": {"login": "owner", "type": "User"},
                     "created_at": "2026-10-19T01:00:00Z",
                     "updated_at": "2026-10-19T01:00:00Z",
                     "body": f"<!-- zed-codex:{head} -->\nexact-shared-marker"}]
        def pages(route):
            return [pull] if "/pulls?" in route else comments
        def api(route, **kwargs):
            self.assertNotIn("method", kwargs)
            ref = f"review-base/{base}" if "review-base/" in route else f"review/changes-{head}"
            return [{"ref": f"refs/heads/{ref}", "object": {"sha": base if "review-base/" in route else head}}]
        with patch.object(release, "pages", side_effect=pages), \
                patch.object(release, "api", side_effect=api), \
                patch.object(release, "maintenance_marker", return_value="exact-shared-marker"), \
                patch.object(release, "check_review", return_value=(True, "complete")) as check:
            result = release.delta_review(repo, head, "c" * 40, candidate=candidate,
                                          accepted=proof, allow_closed=True)
        self.assertEqual(result, (91, base, head, True, "complete"))
        self.assertTrue(check.call_args.kwargs["allow_closed"])

    def test_candidate_identity_covers_the_exact_recipe_source_and_configuration(self):
        with patch.object(release, "product_recipe_sha256", return_value="e" * 64):
            first = release.maintenance_candidate(
                "owner/zed", "a" * 40, "b" * 40, "c" * 40, "d" * 64)
            changed = release.maintenance_candidate(
                "owner/zed", "e" * 40, "b" * 40, "c" * 40, "d" * 64)
            source = release.maintenance_candidate(
                "owner/zed", "a" * 40, "e" * 40, "c" * 40, "d" * 64)
        self.assertNotEqual(first["configuration_sha"], changed["configuration_sha"])
        self.assertEqual(first["recipe_sha256"], changed["recipe_sha256"])
        self.assertEqual(first["recipe_sha256"], source["recipe_sha256"])

    def test_recipe_sources_use_authenticated_api_for_local_and_upstream_blobs(self):
        data = b"immutable recipe bytes\n"
        record = {"type": "file", "encoding": "base64", "size": len(data),
                  "content": base64.b64encode(data).decode("ascii"),
                  "sha": hashlib.sha1(b"blob " + str(len(data)).encode("ascii")
                                      + b"\0" + data).hexdigest()}
        revision, path = "a" * 40, "Cargo.toml"
        for repo in ("owner/zed", release.UPSTREAM):
            with (self.subTest(repo=repo),
                  patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/zed"}),
                  patch.object(release, "api", return_value=record) as authenticated,
                  patch.object(release, "urlopen", side_effect=AssertionError("Unauthenticated read"))):
                self.assertEqual(release.native_source_file(repo, revision, path), data)
                authenticated.assert_called_once_with(
                    f"repos/{repo}/contents/{path}?ref={revision}")

    def test_recipe_source_api_failure_never_falls_back_to_unauthenticated_reads(self):
        failure = subprocess.CalledProcessError(1, ["gh", "api"])
        with (patch.object(release, "api", side_effect=failure) as authenticated,
              patch.object(release, "urlopen", side_effect=AssertionError("Unauthenticated fallback")),
              self.assertRaises(subprocess.CalledProcessError) as raised):
            release.native_source_file(release.UPSTREAM, "a" * 40, "Cargo.toml")
        self.assertIs(raised.exception, failure)
        authenticated.assert_called_once_with(
            f"repos/{release.UPSTREAM}/contents/Cargo.toml?ref={'a' * 40}")

    def test_recipe_hash_tracks_product_code_but_ignores_policy_only_helpers(self):
        root = Path(release.__file__).resolve().parent.parent
        release_source = Path(release.__file__).read_bytes()
        workflow_source = (root / ".github/workflows/release.yml").read_bytes()
        policy_source = release_source + b"\n\ndef unrelated_policy_helper():\n    return False\n"
        old_build = b'if sys.platform != "darwin":'
        self.assertEqual(release_source.count(old_build), 1)
        changed_product = release_source.replace(old_build, b'if sys.platform == "darwin":', 1)
        upstream_workflow = b"""name: Upstream\njobs:\n  run_tests_mac:\n    env:\n      CARGO_INCREMENTAL: 0\n    steps:\n      - run: cargo nextest run --workspace --no-fail-fast --no-tests=warn\n  bundle_mac_aarch64:\n    env:\n      CARGO_INCREMENTAL: 0\n    steps:\n      - run: ./script/bundle-mac aarch64-apple-darwin\n"""
        upstream_files = set(release.PRODUCT_RECIPE_UPSTREAM_FILES)

        def source(repo, revision, path):
            if repo == "owner/zed" and path == "scripts/release.py":
                return {"a" * 40: release_source, "b" * 40: policy_source,
                        "c" * 40: changed_product, "e" * 40: release_source}[revision]
            if repo == "owner/zed" and path == ".github/workflows/release.yml":
                return workflow_source
            if repo == "owner/zed" and path == "patches/source.json":
                return b'{"tests":["changed"]}' if revision == "e" * 40 else b'{"tests":["preserved"]}'

            if repo == release.UPSTREAM and path == ".github/workflows/release.yml":
                return upstream_workflow
            if repo == release.UPSTREAM and path in upstream_files:
                return b"exact upstream recipe bytes: " + path.encode()
            self.fail(f"Unexpected recipe source request: {repo}@{revision}:{path}")

        release.PRODUCT_RECIPE_CACHE.clear()
        with patch.object(release, "native_source_file", side_effect=source):
            baseline = release.product_recipe_sha256("owner/zed", "a" * 40, "d" * 40)
            policy_only = release.product_recipe_sha256("owner/zed", "b" * 40, "d" * 40)
            product_change = release.product_recipe_sha256("owner/zed", "c" * 40, "d" * 40)
            regression_change = release.product_recipe_sha256("owner/zed", "e" * 40, "d" * 40)
        self.assertEqual(baseline, policy_only)
        self.assertNotEqual(baseline, product_change)
        self.assertNotEqual(baseline, regression_change)
        release.PRODUCT_RECIPE_CACHE.clear()

    def test_only_unresolved_pair_findings_are_carried_to_future_history(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        origin = {"candidate_id": "older", "scope": "full", "finding_id": "inline:1:9",
                  "source": "inline", "source_sha": "f" * 40, "native_id": 9,
                  "body": "fixed finding", "path": "src/lib.rs", "line": 10,
                  "severity": "P2"}
        with patch.object(release.maintenance, "load") as load:
            load.return_value.candidate_identity.return_value = "a" * 64
            self.assertIsNone(release.unresolved_review_history_marker(
                {"complete": True, "findings": [], "observed_findings": []}, candidate, {}))
            empty = release.unresolved_review_history_marker(
                {"complete": True, "findings": [], "observed_findings": [origin]}, candidate, {},
                had_history=True)
            marker = release.unresolved_review_history_marker(
                {"complete": True, "findings": [origin], "observed_findings": [origin]}, candidate,
                {origin["finding_id"]: candidate})
        empty_payload = json.loads(release.REVIEW_HISTORY.search(empty).group(1))
        self.assertEqual(empty_payload["findings"], [])
        self.assertIn(origin["finding_id"], marker)

    def test_latest_complete_history_snapshot_drops_previously_resolved_findings(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        finding = {"finding_id": "inline:1:9", "candidate_id": "f" * 64,
                   "scope": "full", "source": "inline", "source_sha": "b" * 40,
                   "native_id": 9, "body": "fixed", "path": "src/lib.rs", "line": 10,
                   "severity": "P2"}
        old = {"schema": 1, "candidate_id": "f" * 64, "findings": [finding],
               "origins": {finding["finding_id"]: candidate}}
        latest = {"schema": 1, "candidate_id": "e" * 64, "findings": [], "origins": {}}
        comments = [
            {"id": 10, "created_at": "2026-10-19T12:00:00Z", "updated_at": "2026-10-19T12:00:00Z",
             "user": {"login": "owner", "type": "User"},
             "body": "<!-- fork-review-history:v1 " + json.dumps(old, separators=(",", ":")) + " -->"},
            {"id": 11, "created_at": "2026-10-19T13:00:00Z", "updated_at": "2026-10-19T13:00:00Z",
             "user": {"login": "owner", "type": "User"},
             "body": "<!-- fork-review-history:v1 " + json.dumps(latest, separators=(",", ":")) + " -->"},
        ]
        module = Mock()
        module.candidate_identity.return_value = "f" * 64
        with (patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/zed"}, clear=True),
              patch.object(release.maintenance, "load", return_value=module)):
            findings, origins = release.review_history_from_comments(comments)
        self.assertEqual(findings, [])
        self.assertEqual(origins, {})

    def test_history_snapshot_requires_immutable_native_identity_and_timestamps(self):
        stamp = "2026-10-19T13:00:00Z"
        record = {"schema": 1, "candidate_id": "e" * 64, "findings": [], "origins": {}}
        comment = {
            "id": 12, "created_at": stamp, "updated_at": stamp,
            "user": {"login": "owner", "type": "User"},
            "body": "<!-- fork-review-history:v1 " + json.dumps(record, separators=(",", ":")) + " -->",
        }
        malformed = [
            {key: value for key, value in comment.items()
             if key not in {"created_at", "updated_at"}},
            {**comment, "created_at": None, "updated_at": None},
            {**comment, "created_at": "", "updated_at": ""},
            {**comment, "created_at": "invalid", "updated_at": "invalid"},
            {**comment, "created_at": "2026-10-19T13:00:00", "updated_at": "2026-10-19T13:00:00"},
            {**comment, "updated_at": "2026-10-19T13:00:01Z"},
        ]
        malformed.extend({**comment, "id": value} for value in (None, 0, -1, True, "12"))
        malformed.extend({**comment, "body": body} for body in (
            "<!-- fork-review-history:v1",
            "<!-- fork-review-history:v1 {}",
            comment["body"] + "\n<!-- fork-review-history:v1",
            comment["body"] + "\n" + comment["body"],
        ))
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/zed"}, clear=True):
            self.assertEqual(release.review_history_from_comments([comment]), ([], {}))
            for bad in malformed:
                with self.subTest(comment=bad), self.assertRaises(ValueError):
                    release.review_history_from_comments([bad])

    def test_history_snapshot_preserves_finding_text_with_literal_markers(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        finding = {
            "finding_id": "inline:1:9", "candidate_id": "f" * 64,
            "scope": "full", "source": "inline", "source_sha": "b" * 40,
            "native_id": 9, "body": "Preserve <!-- fork-review-history:v1 {} --> and --> literally.",
            "path": "src/lib.rs", "line": 10, "severity": "P2",
        }
        module = Mock()
        module.candidate_identity.return_value = "f" * 64
        with (patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/zed"}, clear=True),
              patch.object(release.maintenance, "load", return_value=module)):
            marker = release.review_history_marker(candidate, [finding], {})
            self.assertEqual(marker.count("<!-- fork-review-history:v1"), 1)
            self.assertEqual(marker.count("-->"), 1)
            comment = {
                "id": 12, "created_at": "2026-10-19T13:00:00Z",
                "updated_at": "2026-10-19T13:00:00Z",
                "user": {"login": "owner", "type": "User"}, "body": marker,
            }
            findings, origins = release.review_history_from_comments([comment])
            legacy_payload = {
                "schema": 1, "candidate_id": "f" * 64, "findings": [finding],
                "origins": {finding["finding_id"]: candidate},
            }
            legacy_marker = "<!-- fork-review-history:v1 " + json.dumps(
                legacy_payload, sort_keys=True, separators=(",", ":")) + " -->"
            legacy_findings, legacy_origins = release.review_history_from_comments(
                [{**comment, "body": legacy_marker}])
        self.assertEqual(findings, [finding])
        self.assertEqual(origins, {finding["finding_id"]: candidate})
        self.assertEqual(legacy_findings, [finding])
        self.assertEqual(legacy_origins, origins)

    def test_history_snapshot_text_is_excluded_from_owner_metadata_scanners(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        candidate_id = "f" * 64
        finding_id = "inline:1:9"
        nested_request = "<!-- fork-review:v1 " + json.dumps({
            "schema": 1, "candidate_id": candidate_id, "scope": "full",
            "review_sha": "b" * 40, "base_sha": "a" * 40,
        }, separators=(",", ":")) + " -->"
        nested_disposition = "<!-- fork-review-disposition:v1 " + json.dumps({
            "schema": 1, "candidate_id": candidate_id, "source_sha": "c" * 40,
            "findings": [{"finding_id": "nested", "status": "fixed",
                          "evidence": "code change and regression"}],
        }, separators=(",", ":")) + " -->"
        nested_candidate = "<!-- fork-candidate:v1 " + json.dumps({**candidate, "configuration_sha": "9" * 40},
            separators=(",", ":")) + " -->"
        nested_reservation = "<!-- fork-final:v1 " + json.dumps({
            "schema": 1, "candidate_id": candidate_id, "run_id": 701,
            "run_attempt": 1, "workflow_sha": "a" * 40,
        }, separators=(",", ":")) + " -->"
        nested_retry = (f"<!-- zed-final:{'b' * 40}:{'9' * 40} --> "
                        f"https://github.com/owner/zed/actions/runs/701 "
                        f"<!-- zed-final-retry:{'b' * 40}:{'c' * 40}:701 -->")
        finding = {
            "finding_id": finding_id, "candidate_id": candidate_id,
            "scope": "full", "source": "inline", "source_sha": "b" * 40,
            "native_id": 9,
            "body": "Literal owner metadata: " + " ".join((
                nested_request, nested_disposition, nested_candidate,
                nested_reservation, nested_retry)),
            "path": "src/lib.rs", "line": 10, "severity": "P2",
        }
        snapshot = {"schema": 1, "candidate_id": candidate_id, "findings": [finding],
                    "origins": {finding_id: candidate}}
        # This is the legacy writer format: angle brackets in finding text are unescaped.
        legacy_history = "<!-- fork-review-history:v1 " + json.dumps(
            snapshot, sort_keys=True, separators=(",", ":")) + " -->"
        outer_request = "<!-- fork-review:v1 " + json.dumps({
            "schema": 1, "candidate_id": candidate_id, "scope": "delta",
            "review_sha": "c" * 40, "base_sha": "b" * 40,
        }, separators=(",", ":")) + " -->"
        outer_disposition_payload = {
            "schema": 1, "candidate_id": candidate_id, "source_sha": "c" * 40,
            "findings": [{"finding_id": "outer", "status": "fixed",
                          "evidence": "code update and regression"}],
        }
        outer_disposition = "<!-- fork-review-disposition:v1 " + json.dumps(
            outer_disposition_payload, separators=(",", ":")) + " -->"
        outer_candidate = "<!-- fork-candidate:v1 " + json.dumps(
            candidate, separators=(",", ":")) + " -->"
        outer_reservation_payload = {
            "schema": 1, "candidate_id": candidate_id, "run_id": 702,
            "run_attempt": 2, "workflow_sha": "b" * 40,
        }
        outer_reservation = "<!-- fork-final:v1 " + json.dumps(
            outer_reservation_payload, separators=(",", ":")) + " -->"
        body = "\n".join((legacy_history, outer_request, outer_disposition,
                           outer_candidate, outer_reservation))
        comment = {
            "id": 12, "created_at": "2026-10-19T13:00:00Z",
            "updated_at": "2026-10-19T13:00:00Z",
            "user": {"login": "owner", "type": "User"}, "body": body,
        }
        module = Mock()
        module.candidate_identity.return_value = candidate_id
        with (patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/zed"}, clear=True),
              patch.object(release.maintenance, "load", return_value=module)):
            payloads, metadata = release.history_snapshot_metadata(comment, "owner")
            findings, origins = release.review_history_from_comments([comment])
            dispositions = release.review_dispositions_from_comments([comment], candidate)
            reservations = release.reservations_from_history_comments([comment], owner="owner")
            with patch.object(release, "api") as api:
                self.assertIsNone(release.final_retry_authorization(
                    "owner/zed", "b" * 40, "c" * 40, [comment], previous_run=701))
                api.assert_not_called()

            bot_comment = {
                "id": 13, "created_at": comment["created_at"],
                "updated_at": comment["updated_at"],
                "user": {"login": release.BOT, "type": "Bot"},
                "body": "<!-- fork-review-history:v1 truncated",
            }
            bot_payloads, bot_body = release.history_snapshot_metadata(bot_comment, "owner")
            self.assertEqual(release.review_history_from_comments([bot_comment]), ([], {}))

        self.assertEqual(payloads, [snapshot])
        self.assertEqual(findings, [finding])
        self.assertEqual(origins, {finding_id: candidate})
        self.assertEqual([item["finding_id"] for item in dispositions], ["outer"])
        self.assertEqual(reservations, [outer_reservation_payload])
        self.assertNotIn(nested_request, metadata)
        self.assertNotIn(nested_disposition, metadata)
        self.assertNotIn(nested_candidate, metadata)
        self.assertNotIn(nested_reservation, metadata)
        self.assertNotIn(nested_retry, metadata)
        self.assertIn(outer_request, metadata)
        self.assertIn(outer_disposition, metadata)
        candidate_markers = release.FINAL_CANDIDATE.findall(metadata)
        self.assertEqual(len(candidate_markers), 1)
        self.assertEqual(release.strict_json(candidate_markers[0], "fixture"), candidate)
        self.assertIn(outer_reservation, metadata)
        self.assertEqual(bot_payloads, [])
        self.assertIn("truncated", bot_body)

    def test_outer_disposition_json_keeps_history_marker_text_opaque(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        candidate_id = "f" * 64
        stamp = "2026-10-20T10:00:00Z"
        record = {"schema": 1, "candidate_id": candidate_id,
                  "source_sha": candidate["configuration_sha"],
                  "findings": [{"finding_id": "review:7:9", "status": "disproved",
                                "evidence": "Code and regression checked; quoted "
                                            "<!-- fork-review-history:v1 { is data."}]}
        marker = "<!-- fork-review-disposition:v1 " + json.dumps(
            record, sort_keys=True, separators=(",", ":")) + " -->"
        comment = {"id": 19, "created_at": stamp, "updated_at": stamp,
                   "user": {"login": "owner", "type": "User"}, "body": marker}
        module = Mock()
        module.candidate_identity.return_value = candidate_id
        with (patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/zed"}, clear=True),
              patch.object(release.maintenance, "load", return_value=module)):
            payloads, metadata = release.history_snapshot_metadata(comment, "owner")
            dispositions = release.review_dispositions_from_comments([comment], candidate)
        self.assertEqual(payloads, [])
        self.assertIn(marker, metadata)
        self.assertEqual([item["finding_id"] for item in dispositions], ["review:7:9"])

    def test_prepublication_snapshot_edits_retire_without_hiding_outer_metadata(self):
        cutoff = "2026-10-19T09:00:00Z"
        application, configuration, upstream = "a" * 40, "b" * 40, "d" * 40
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": upstream,
                     "application_sha": application, "configuration_sha": configuration,
                     "patchset_sha256": "e" * 64, "recipe_sha256": "f" * 64,
                     "platform": "macos/aarch64"}
        candidate_id = hashlib.sha256(json.dumps(
            candidate, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        finding_id = "inline:7:11"
        finding = {"finding_id": finding_id, "candidate_id": candidate_id,
                   "body": (f"Old text <!-- zed-final:{application} --> and "
                            "<!-- fork-review-history:v1 {"),
                   "path": "crates/zed/src/lib.rs", "line": 11, "severity": "P1"}
        snapshot = {"schema": 1, "candidate_id": candidate_id, "findings": [finding],
                    "origins": {finding_id: candidate}}
        snapshot_marker = "<!-- fork-review-history:v1 " + json.dumps(
            snapshot, sort_keys=True, separators=(",", ":")) + " -->"
        outer_final = "<!-- fork-final:v1 " + json.dumps({
            "schema": 1, "candidate_id": candidate_id, "run_id": 41,
            "run_attempt": 1, "workflow_sha": configuration,
        }, sort_keys=True, separators=(",", ":")) + " -->"
        old_comment = {"id": 20, "created_at": "2026-10-19T08:00:00Z",
                       "updated_at": "2026-10-19T08:30:00Z",
                       "user": {"login": "owner", "type": "User"},
                       "body": snapshot_marker + "\n" + outer_final}
        payloads, metadata = release.history_snapshot_metadata(
            old_comment, "owner", published_at=cutoff)
        self.assertEqual(payloads, [])
        self.assertNotIn(f"<!-- zed-final:{application} -->", metadata)
        self.assertNotIn("<!-- fork-review-history:v1 {", metadata)
        self.assertIn(outer_final, metadata)

        pull = {"number": 7, "updated_at": "2026-10-19T10:10:00Z",
                "user": {"login": "owner", "type": "User"},
                "base": {"sha": upstream, "ref": f"review-base/{upstream}"},
                "head": {"sha": application, "ref": f"review/{upstream}-{'1' * 64}",
                         "repo": {"full_name": "owner/zed"}}}
        retired_comment = {**old_comment, "body": snapshot_marker}
        receipt = {"user": {"login": "github-actions[bot]", "type": "Bot"},
                   "body": (f"Final validation and single build of `{application}` / `{configuration}`: "
                            "https://github.com/owner/zed/actions/runs/42\n"
                            f"<!-- zed-final:{application} -->\n"
                            f"<!-- zed-final-workflow:{configuration} -->")}
        published = [{"id": 1, "draft": False, "prerelease": False, "published_at": cutoff}]
        run_record = {"status": "completed", "conclusion": "failure",
                      "path": ".github/workflows/release.yml", "head_sha": configuration,
                      "event": "workflow_dispatch", "updated_at": "2026-10-19T10:05:00Z"}
        job_record = {"total_count": 1, "jobs": [{
            "name": release.FINAL_HEAVY_JOB, "status": "completed", "conclusion": "failure",
            "started_at": "2026-10-19T10:01:00Z", "completed_at": "2026-10-19T10:04:00Z",
            "steps": [{"name": "Compile", "conclusion": "failure"}],
        }]}

        def pages(route):
            if route.endswith("/releases"):
                return published
            if "/pulls?" in route:
                return [pull]
            if route.endswith("/issues/7/comments"):
                return [retired_comment, receipt]
            raise AssertionError(route)

        def api(route, **_kwargs):
            if route.endswith("/runs/42"):
                return run_record
            if route.endswith("/runs/42/jobs?filter=all&per_page=100"):
                return job_record
            raise AssertionError(route)

        with patch.object(release, "pages", side_effect=pages), \
                patch.object(release, "api", side_effect=api):
            self.assertEqual(release.previous_final_failure("owner/zed"), 42)

        postpublication_edit = {**retired_comment, "updated_at": "2026-10-19T09:01:00Z"}
        def edited_pages(route):
            if route.endswith("/releases"):
                return published
            if "/pulls?" in route:
                return [pull]
            if route.endswith("/issues/7/comments"):
                return [postpublication_edit, receipt]
            raise AssertionError(route)

        with (patch.object(release, "pages", side_effect=edited_pages),
              patch.object(release, "api", side_effect=api),
              self.assertRaisesRegex(ValueError, "edited")):
            release.previous_final_failure("owner/zed")

    def test_pair_preserves_pending_and_finding_states_without_authorizing_execution(self):
        with patch.object(release, "product_recipe_sha256", return_value="e" * 64):
            candidate = release.maintenance_candidate(
                "owner/zed", "a" * 40, "b" * 40, "c" * 40, "d" * 64)
        accepted = {"endpoint": "repos/owner/zed/actions/variables/FORK_ACCEPTED_BASELINE",
                    "variable": {"name": "FORK_ACCEPTED_BASELINE", "value": json.dumps({
                        "publication": {"published_at": "2026-10-19T00:00:00Z"}}),
                                 "created_at": "2026-10-19T00:00:00Z",
                                 "updated_at": "2026-10-19T00:00:00Z"}}
        for state in ("waiting", "findings", "invalid", "ready"):
            with self.subTest(state=state), ExitStack() as stack:
                full = {"scope": "full"}
                delta = {"scope": "delta"}
                review = stack.enter_context(patch.object(release, "maintenance_review", side_effect=[full, delta]))
                stack.enter_context(patch.object(release, "native_review_history",
                                                  return_value={"findings": [], "dispositions": []}))
                module = stack.enter_context(patch.object(release.maintenance, "load")).return_value
                module.evaluate_pair.return_value = {"state": state, "findings": [], "observed_findings": []}
                if state in {"waiting", "findings", "ready"}:
                    self.assertEqual(release.maintenance_pair(
                        candidate, 1, "b" * 40, accepted, 2, "a" * 40, "e" * 40)["state"], state)
                else:
                    with self.assertRaisesRegex(ValueError, "review pair"):
                        release.maintenance_pair(
                            candidate, 1, "b" * 40, accepted, 2, "a" * 40, "e" * 40)
                self.assertEqual(review.call_count, 2)
                module.evaluate_pair.assert_called_once_with(
                    candidate, full, delta, known_findings=[], dispositions=[])
                module.authorize_final.assert_not_called()

    def test_native_review_history_reads_complete_same_repo_pr_contexts_after_publication(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        published_at = "2026-10-19T10:00:00Z"
        baseline = {"schema": 1, "repository": "owner/zed", "candidate": candidate,
                    "publication": {"published_at": published_at}}
        accepted = {"variable": {"value": json.dumps(baseline)}}
        source = "abcdef1" + "b" * 33
        pull = {"number": 7, "state": "closed", "updated_at": "2026-10-19T11:00:00Z",
                "head": {"sha": source, "repo": {"full_name": "owner/zed"}}}
        completion = {"id": 8, "created_at": "2026-10-19T10:30:00Z",
                      "updated_at": "2026-10-19T10:30:00Z",
                      "user": {"login": release.BOT, "type": "Bot"},
                      "performed_via_github_app": {"slug": release.APP},
                      "body": "**Reviewed commit:** `abcdef1`"}
        module = Mock()
        module.review_history.return_value = {"findings": [], "dispositions": []}

        def native_api(route):
            if route == ("repos/owner/zed/pulls?state=all&sort=updated&direction=desc"
                         "&per_page=100&page=1"):
                return [pull]
            if route == "repos/owner/zed/pulls/7":
                return pull
            if route == "repos/owner/zed/commits/abcdef1":
                return {"sha": source}
            self.fail(f"Unexpected native read: {route}")

        def native_pages(route):
            if route == "repos/owner/zed/issues/7/comments":
                return [completion]
            if route in {"repos/owner/zed/pulls/7/reviews", "repos/owner/zed/pulls/7/comments"}:
                return []
            self.fail(f"Unexpected native list: {route}")

        with (patch.object(release.maintenance, "load", return_value=module),
              patch.object(release, "api", side_effect=native_api) as read_native,
              patch.object(release, "pages", side_effect=native_pages)):
            self.assertEqual(release.native_review_history(candidate, accepted, current_prs=(7,)),
                             {"findings": [], "dispositions": []})

        records = module.review_history.call_args.args[1]
        self.assertEqual(module.review_history.call_args.args[2], published_at)
        self.assertIs(module.review_history.call_args.kwargs["read_native"], read_native)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["pull"], pull)
        self.assertEqual(records[0]["comments"], [completion])
        self.assertEqual(records[0]["reviews"], [])
        self.assertEqual(records[0]["inline"], [])
        self.assertEqual(records[0]["resolved_commits"], {"abcdef1": source})

    def test_current_snapshot_reads_no_repository_wide_comment_archive(self):
        repo = "owner/zed"
        pulls = [{"number": 7, "head": {"repo": {"full_name": repo}}}]
        def read(route):
            if route == f"repos/{repo}/pulls?state=open":
                return pulls
            if route in {f"repos/{repo}/issues/7/comments", f"repos/{repo}/issues/8/comments"}:
                return [{"route": route}]
            raise AssertionError("Archived or repository-wide comment read: " + route)
        with patch.object(release, "pages", side_effect=read):
            self.assertEqual(release.current_review_comments(repo, current_prs=(8,)),
                [{"route": f"repos/{repo}/issues/7/comments"},
                 {"route": f"repos/{repo}/issues/8/comments"}])

    def test_native_history_accepts_reordered_inventory_but_rejects_real_changes(self):
        candidate = {"repository": "owner/zed"}
        accepted = {"variable": {"value": json.dumps({"repository": "owner/zed", "publication": {
            "published_at": "2026-10-19T10:00:00Z"}})}}
        first = {"number": 7, "state": "open", "updated_at": "2026-10-19T12:00:00Z",
                 "head": {"sha": "a" * 40, "repo": {"full_name": "owner/zed"}}}
        second = {"number": 8, "state": "open", "updated_at": "2026-10-19T11:00:00Z",
                  "head": {"sha": "b" * 40, "repo": {"full_name": "owner/zed"}}}
        archived = {**first, "number": 99, "state": "closed",
                    "updated_at": "2026-10-21T12:00:00Z"}
        for mode in ("reorder", "changed", "duplicate"):
            module = Mock()
            module.review_history.return_value = {"findings": [], "dispositions": []}
            inventories = []
            def native_api(route):
                if "/pulls?" in route:
                    inventories.append(route)
                    if mode == "duplicate":
                        return [first, first]
                    if len(inventories) == 1:
                        return [second, first, archived]
                    moved = {**first, "head": {**first["head"], "sha": "c" * 40}}
                    return [moved if mode == "changed" else first, second, archived]
                if "/99" in route:
                    raise AssertionError("Archived review discussion must not be read")
                return first if route.endswith("/7") else second
            with (self.subTest(mode=mode),
                  patch.object(release.maintenance, "load", return_value=module),
                  patch.object(release, "api", side_effect=native_api),
                  patch.object(release, "pages", return_value=[])):
                if mode == "reorder":
                    release.native_review_history(candidate, accepted)
                    self.assertEqual([x["pull"]["number"] for x in
                                      module.review_history.call_args.args[1]], [7, 8])
                else:
                    with self.assertRaises(ValueError):
                        release.native_review_history(candidate, accepted)
                    module.review_history.assert_not_called()

    def test_full_review_sources_require_existing_matching_repository_commits(self):
        source = "abcdef1" + "b" * 33
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        accepted = {"variable": {"value": json.dumps({
            "repository": "owner/zed",
            "publication": {"published_at": "2026-10-19T10:00:00Z"}})}}
        pull = {"number": 7, "state": "closed", "updated_at": "2026-10-19T11:00:00Z",
                "head": {"sha": source, "repo": {"full_name": "owner/zed"}}}
        completion = {"id": 8, "created_at": "2026-10-19T10:30:00Z",
                      "updated_at": "2026-10-19T10:30:00Z",
                      "user": {"login": release.BOT, "type": "Bot"},
                      "performed_via_github_app": {"slug": release.APP},
                      "body": f"**Reviewed commit:** `{source}`"}
        endpoint = f"repos/owner/zed/commits/{source}"
        for outcome in ("valid", "missing", "mismatched"):
            with self.subTest(outcome=outcome):
                module = Mock()
                module.review_history.return_value = {"findings": [], "dispositions": []}
                reads = []

                def native_api(route):
                    reads.append(route)
                    if "/pulls?" in route:
                        return [pull]
                    if route == "repos/owner/zed/pulls/7":
                        return pull
                    if route == endpoint:
                        if outcome == "missing":
                            raise ValueError("Native commit is missing")
                        return {"sha": source if outcome == "valid" else "f" * 40}
                    self.fail(f"Unexpected native read: {route}")

                def native_pages(route):
                    if route == "repos/owner/zed/issues/7/comments":
                        return [completion]
                    if route in {"repos/owner/zed/pulls/7/reviews", "repos/owner/zed/pulls/7/comments"}:
                        return []
                    self.fail(f"Unexpected native list: {route}")

                with (patch.object(release.maintenance, "load", return_value=module),
                      patch.object(release, "api", side_effect=native_api),
                      patch.object(release, "pages", side_effect=native_pages)):
                    if outcome == "valid":
                        self.assertEqual(release.native_review_history(candidate, accepted, current_prs=(7,)),
                                         {"findings": [], "dispositions": []})
                        records = module.review_history.call_args.args[1]
                        self.assertEqual(records[0]["resolved_commits"], {source: source})
                    else:
                        with self.assertRaises(ValueError):
                            release.native_review_history(candidate, accepted, current_prs=(7,))
                        module.review_history.assert_not_called()
                self.assertEqual(reads.count(endpoint), 1)

    def test_native_findings_and_snapshot_findings_both_reach_the_pair_evaluator(self):
        with patch.object(release, "product_recipe_sha256", return_value="e" * 64):
            candidate = release.maintenance_candidate(
                "owner/zed", "a" * 40, "b" * 40, "c" * 40, "d" * 64)
        native = {"finding_id": "review:7:8", "candidate_id": "f" * 64,
                  "source": "review", "body": "native finding"}
        snapshot = {"finding_id": "inline:3:4", "candidate_id": "e" * 64,
                    "source": "inline", "body": "carried snapshot"}
        disposition = {"finding_id": "review:7:8", "candidate_id": "f" * 64,
                       "source_sha": "a" * 40, "status": "fixed", "evidence": "code and regression"}
        full, delta = {"scope": "full"}, {"scope": "delta"}
        module = Mock()
        module.evaluate_pair.return_value = {"state": "ready", "findings": [],
                                             "observed_findings": []}
        accepted = {"variable": {"value": json.dumps({
            "publication": {"published_at": "2026-10-19T00:00:00Z"}})}}
        with (patch.object(release, "maintenance_review", side_effect=[full, delta]),
              patch.object(release, "native_review_history", return_value={
                  "findings": [native], "dispositions": [disposition]}),
              patch.object(release.maintenance, "load", return_value=module)):
            result = release.maintenance_pair(candidate, 1, "b" * 40, accepted, 2,
                "a" * 40, "c" * 40, known_findings=[snapshot])
        module.evaluate_pair.assert_called_once_with(candidate, full, delta,
            known_findings=[snapshot, native], dispositions=[disposition])
        self.assertEqual(result["known_findings"], [snapshot, native])

    def test_native_tree_enrichment_supersedes_the_same_legacy_snapshot(self):
        native = {"finding_id": "review:7:8", "candidate_id": "f" * 64,
                  "source_sha": "b" * 40, "source_tree_sha": "c" * 40,
                  "body": "The original native finding"}
        snapshot = {key: value for key, value in native.items() if key != "source_tree_sha"}
        module = Mock()
        module.evaluate_pair.return_value = {"state": "findings", "findings": [native]}
        accepted = {"variable": {"value": json.dumps({
            "publication": {"published_at": "2026-10-19T00:00:00Z"}})}}
        with (patch.object(release, "maintenance_review", side_effect=[{}, {}]),
              patch.object(release, "native_review_history", return_value={
                  "findings": [native], "dispositions": []}),
              patch.object(release.maintenance, "load", return_value=module)):
            result = release.maintenance_pair({"upstream_sha": "d" * 40}, 1,
                "b" * 40, accepted, 2, "a" * 40, "e" * 40, known_findings=[snapshot])
        self.assertEqual(result["known_findings"], [native])
        self.assertEqual(module.evaluate_pair.call_args.kwargs["known_findings"], [native])
        conflicting = {**native, "body": "An owner copy cannot replace native prose"}
        self.assertEqual(release.merge_history_items(
            [native], [conflicting], "history", native_authoritative=True), [native])
        with self.assertRaisesRegex(ValueError, "conflicting records"):
            release.merge_history_items([native, conflicting], [], "history",
                                        native_authoritative=True)
        with self.assertRaisesRegex(ValueError, "conflicting records"):
            release.merge_history_items([native], [conflicting], "dispositions")


class ValidatedArtifactCandidateTests(unittest.TestCase):
    def candidate(self, **changes):
        return {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                "platform": "macos/aarch64"} | changes

    def history(self, sources, *, complete=True):
        return {"complete": complete,
                "attempts": [{"candidate": source,
                              "run": {"id": 41, "run_attempt": 2}}
                             for source in sources]}

    def test_exact_attempt_returns_its_source_with_only_configuration_difference(self):
        candidate = self.candidate()
        source = self.candidate(configuration_sha="f" * 40)
        module = Mock()
        with patch.object(release.maintenance, "load", return_value=module):
            self.assertIs(release.validated_artifact_candidate(
                candidate, self.history([source]), 41, 2), source)
        module.candidate_identity.assert_called_once_with(source)

    def test_incomplete_missing_and_duplicate_attempts_are_rejected(self):
        candidate = self.candidate()
        source = self.candidate(configuration_sha="f" * 40)
        with patch.object(release.maintenance, "load", return_value=Mock()):
            for incomplete in (False, None, "true", 1):
                with self.subTest(complete=incomplete), self.assertRaisesRegex(
                        ValueError, "complete native attempt history"):
                    release.validated_artifact_candidate(
                        candidate, self.history([source], complete=incomplete), 41, 2)
            with self.assertRaisesRegex(ValueError, "exactly one"):
                release.validated_artifact_candidate(
                    candidate, self.history([source]), 42, 2)
            with self.assertRaisesRegex(ValueError, "exactly one"):
                release.validated_artifact_candidate(
                    candidate, self.history([source], complete=True), 41, 1)
            with self.assertRaisesRegex(ValueError, "exactly one"):
                release.validated_artifact_candidate(
                    candidate, self.history([source, source]), 41, 2)

    def test_every_source_field_beyond_configuration_is_bound(self):
        candidate = self.candidate()
        for key, value in (("schema", 2), ("repository", "other/zed"),
                           ("upstream_sha", "f" * 40), ("application_sha", "f" * 40),
                           ("patchset_sha256", "0" * 64), ("recipe_sha256", "0" * 64),
                           ("platform", "linux/x86_64")):
            source = self.candidate(configuration_sha="f" * 40, **{key: value})
            with (self.subTest(key=key),
                  patch.object(release.maintenance, "load", return_value=Mock()),
                  self.assertRaisesRegex(ValueError, "differs from the assembled")):
                release.validated_artifact_candidate(
                    candidate, self.history([source]), 41, 2)


class SharedFinalArtifactReuseTests(unittest.TestCase):
    def test_final_reservation_readback_ignores_cosmetic_views_but_not_protocol_mutation(self):
        record = {"id": 7, "body": "exact protocol", "created_at": "2026-10-23T10:00:00Z",
                  "updated_at": "2026-10-23T10:00:00Z", "user": {"id": 8, "login": "owner", "type": "User"}}
        cosmetic = {**record, "reactions": {"total_count": 1},
                    "user": {**record["user"], "avatar_url": "changed"}}
        self.assertEqual(release.final_reservation_identity(record), release.final_reservation_identity(cosmetic))
        for mutation in ({"body": "changed"}, {"id": 9}, {"updated_at": "2026-10-23T10:00:01Z"},
                         {"user": {**record["user"], "login": "foreign"}}):
            self.assertNotEqual(release.final_reservation_identity(record),
                                release.final_reservation_identity({**record, **mutation}))

    def test_active_final_workflow_waits_without_a_new_reservation(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        history = {"complete": True, "baseline": {"publication": {
                       "published_at": "2026-10-19T10:00:00Z"}}, "attempts": []}
        decision = {"decision": "wait", "allowed": False,
                    "reason": "An exact native final workflow is still active; do not overlap"}
        module = Mock()
        module.authorize_final.return_value = decision
        with (patch.dict(os.environ, {"GITHUB_RUN_ID": "90", "GITHUB_RUN_ATTEMPT": "1"},
                         clear=True),
              patch.object(release, "final_attempt_history", return_value=(history, [])),
              patch.object(release.maintenance, "load", return_value=module),
              patch.object(release, "api") as native):
            for allow_reuse in (False, True):
                with self.subTest(allow_reuse=allow_reuse):
                    self.assertEqual(release.authorize_shared_final(
                        candidate, {}, allow_reuse=allow_reuse), decision)
            native.assert_not_called()
        self.assertEqual(module.authorize_final.call_count, 2)
        module.authorize_final.assert_called_with(candidate, history)

    def test_recovery_snapshot_is_used_without_recollecting_and_requires_the_exact_attempt(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        history = {"complete": True, "baseline": {"publication": {
                       "published_at": "2026-10-19T10:00:00Z"}}, "attempts": []}
        for decision in ({"decision": "allow"}, {"decision": "wait"},
                         {"decision": "policy-only"}, {"decision": "reuse"},
                         {"decision": "already-validated",
                          "validated_run": {"id": 42, "run_attempt": 1}}):
            module = Mock()
            module.authorize_final.return_value = decision
            with (self.subTest(decision=decision["decision"]),
                  patch.dict(os.environ, {"GITHUB_RUN_ID": "90", "GITHUB_RUN_ATTEMPT": "1"},
                             clear=True),
                  patch.object(release, "final_attempt_history",
                               side_effect=AssertionError("history must not be recollected")),
                  patch.object(release, "reservations_from_history_comments", return_value=[]),
                  patch.object(release.maintenance, "load", return_value=module),
                  patch.object(release, "api") as native,
                  self.assertRaisesRegex(ValueError, "exact earlier validated run")):
                release.authorize_shared_final(
                    candidate, {}, allow_reuse=True, history_snapshot=(history, []),
                    required_validated_run=(41, 2))
            native.assert_not_called()

    def test_recovery_accepts_only_the_exact_already_validated_original_attempt(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        marker = {"schema": 1, "candidate_id": "1" * 64, "run_id": 41, "run_attempt": 2,
                  "workflow_sha": "9" * 40}
        body = ("Native reservation for one exact final Zed application attempt.\n"
                "<!-- fork-candidate:v1 " + json.dumps(candidate, sort_keys=True,
                separators=(",", ":")) + " -->\n<!-- fork-final:v1 "
                + json.dumps(marker, sort_keys=True, separators=(",", ":")) + " -->")
        comments = [{"id": 51, "body": body, "created_at": "2026-10-20T10:00:00Z",
                     "updated_at": "2026-10-20T10:00:00Z",
                     "user": {"login": "owner", "type": "User"}}]
        names = release.candidate_artifact_names(candidate["application_sha"], 41, 2)
        artifacts = [{"id": index + 1, "name": name, "expired": False,
                      "size_in_bytes": 100, "digest": "sha256:" + "0" * 64,
                      "workflow_run": {"id": 41}}
                     for index, name in enumerate(names.values())]
        history = {"complete": True, "baseline": {"publication": {
                       "published_at": "2026-10-19T10:00:00Z"}}, "attempts": []}
        module = Mock()
        module.candidate_identity.return_value = "1" * 64
        module.authorize_final.return_value = {
            "decision": "already-validated", "candidate_id": "1" * 64,
            "validated_run": {"id": 41, "run_attempt": 2}}
        with (patch.dict(os.environ, {"GITHUB_RUN_ID": "90", "GITHUB_RUN_ATTEMPT": "1"},
                         clear=True),
              patch.object(release, "final_attempt_history",
                           side_effect=AssertionError("history must not be recollected")),
              patch.object(release, "reservations_from_history_comments", return_value=[]),
              patch.object(release.maintenance, "load", return_value=module),
              patch.object(release, "api", return_value={
                  "total_count": 2, "artifacts": artifacts}) as native):
            decision = release.authorize_shared_final(
                candidate, {}, history_snapshot=(history, comments),
                required_validated_run=(41, 2))
        self.assertEqual(decision["decision"], "already-validated")
        self.assertEqual(decision["validated_run"], {"id": 41, "run_attempt": 2})
        self.assertEqual(decision["artifact_workflow_sha"], "9" * 40)
        self.assertEqual(decision["artifact_names"], names)
        self.assertEqual(decision["artifact_ids"],
                         {name: item["id"] for name, item in zip(names.values(), artifacts)})
        native.assert_called_once_with(
            "repos/owner/zed/actions/runs/41/artifacts?per_page=100")

    def test_repair_workflow_source_uses_exact_native_files_and_cached_source_keys(self):
        repo, head, base = "owner/zed", "a" * 40, "b" * 40
        run = {"id": 99, "run_attempt": 1, "head_sha": head,
               "node_id": "WFR_fixture",
               "path": ".github/workflows/release.yml", "event": "pull_request_target",
               "head_branch": "repair/example"}
        files = {sha: {"sha": "c" * 40, "source_fixture": sha} for sha in (head, base)}
        graph = {"data": {"node": {"id": run["node_id"], "databaseId": run["id"]}}}

        def read_native(route, **kwargs):
            if route == "graphql":
                self.assertEqual(kwargs["method"], "POST")
                self.assertEqual(kwargs["data"]["variables"], {"id": run["node_id"]})
                return graph
            return files[route.rsplit("=", 1)[1]]

        module, cache = Mock(), {}
        module.validate_workflow_source.return_value = True
        with (patch.object(release.maintenance, "load", return_value=module),
              patch.object(release, "api", side_effect=read_native) as native):
            proof = release.native_workflow_source(repo, run, base, cache=cache)
            self.assertEqual(proof, {"run_file": files[head], "reserved_file": files[base],
                                     "workflow_run": graph})
            self.assertEqual(release.native_workflow_source(repo, run, base, cache=cache), proof)
            self.assertEqual(native.call_count, 3)
            module.validate_workflow_source.assert_called_with(
                repo, run, base, ".github/workflows/release.yml", proof)
            self.assertIsNone(release.native_workflow_source(repo, run, head))
            self.assertEqual(native.call_count, 3)
            module.validate_workflow_source.side_effect = ValueError("workflow blobs differ")
            with self.assertRaisesRegex(ValueError, "workflow blobs differ"):
                release.native_workflow_source(repo, run, base, cache=cache)
            module.validate_workflow_source.side_effect = None
            module.validate_workflow_source.return_value = False
            with self.assertRaisesRegex(ValueError, "did not verify"):
                release.native_workflow_source(repo, run, base, cache=cache)

    def test_unproved_current_workflow_cannot_create_a_final_reservation(self):
        repo, sha = "owner/zed", "a" * 40
        module = Mock()
        module.authorize_final.return_value = {"decision": "allow", "candidate_id": "f" * 64}
        current = {"id": 99, "run_attempt": 1, "head_sha": "b" * 40}
        history = {"baseline": {"publication": {"published_at": "2026-10-19T10:00:00Z"}}}
        with (patch.dict(os.environ, {"GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1",
                                     "GITHUB_SHA": sha, "CANDIDATE_PR": "7"}, clear=True),
              patch.object(release.maintenance, "load", return_value=module),
              patch.object(release, "final_attempt_history", return_value=(history, [])),
              patch.object(release, "api", return_value=current) as native,
              patch.object(release, "native_workflow_source", side_effect=ValueError("unproved workflow")),
              self.assertRaisesRegex(ValueError, "unproved workflow")):
            release.authorize_shared_final({"repository": repo}, {})
        native.assert_called_once_with(f"repos/{repo}/actions/runs/99/attempts/1")

    def test_current_config_cannot_spend_a_second_build_for_a_successful_identical_product(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        prior = {**candidate, "configuration_sha": "f" * 40}
        step = {"status": "completed", "conclusion": "success", "started_at": "2026-10-19T12:02:00Z"}
        attempt = {"candidate": prior,
                   "run": {"status": "completed", "conclusion": "success"},
                   "jobs": [{"name": release.FINAL_HEAVY_JOB, "status": "completed",
                             "conclusion": "success", "started_at": "2026-10-19T12:01:00Z",
                             "steps": [step]}]}
        history = {"complete": True, "baseline": {"publication": {
                       "published_at": "2026-10-19T10:00:00Z"}},
                   "required_heavy_jobs": [release.FINAL_HEAVY_JOB],
                   "attempts": [attempt]}
        module = Mock()
        module.authorize_final.return_value = {"decision": "allow", "candidate_id": "1" * 64}
        with (patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/zed", "GITHUB_SHA": "c" * 40,
                                     "GITHUB_RUN_ID": "90", "GITHUB_RUN_ATTEMPT": "1"}, clear=True),
              patch.object(release, "final_attempt_history", return_value=(history, [])),
              patch.object(release, "reservations_from_history_comments", return_value=[]),
              patch.object(release.maintenance, "load", return_value=module),
              patch.object(release, "api") as native,
              self.assertRaisesRegex(ValueError, "exact native blob-preserving adoption proof")):
            release.authorize_shared_final(candidate, {})
        native.assert_not_called()
        module.authorize_final.assert_called_once_with(candidate, history)

    def test_published_baseline_reuse_is_exposed_only_to_verified_bootstrap(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        history = {"complete": True, "baseline": {"publication": {
                       "published_at": "2026-10-19T10:00:00Z"}}, "attempts": []}
        module = Mock()
        module.authorize_final.return_value = {"decision": "reuse", "candidate_id": "1" * 64}
        with (patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/zed", "GITHUB_RUN_ID": "90",
                                     "GITHUB_RUN_ATTEMPT": "1"}, clear=True),
              patch.object(release, "final_attempt_history", return_value=(history, [])),
              patch.object(release, "reservations_from_history_comments", return_value=[]),
              patch.object(release.maintenance, "load", return_value=module)):
            with self.assertRaisesRegex(ValueError, "Shared final validation is blocked"):
                release.authorize_shared_final(candidate, {})
            result = release.authorize_shared_final(candidate, {}, allow_reuse=True)
        self.assertEqual(result["decision"], "reuse")
        self.assertEqual(module.authorize_final.call_count, 2)

    def test_bootstrap_does_not_convert_a_blocked_candidate_into_an_application_attempt(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        history = {"complete": True, "baseline": {"publication": {
                       "published_at": "2026-10-19T10:00:00Z"}}, "attempts": []}
        module = Mock()
        module.authorize_final.return_value = {"decision": "blocked", "reason": "spent attempt"}
        with (patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/zed", "GITHUB_RUN_ID": "90",
                                     "GITHUB_RUN_ATTEMPT": "1"}, clear=True),
              patch.object(release, "final_attempt_history", return_value=(history, [])),
              patch.object(release, "reservations_from_history_comments", return_value=[]),
              patch.object(release.maintenance, "load", return_value=module),
              patch.object(release, "api") as native,
              self.assertRaisesRegex(ValueError, "Shared final validation is blocked")):
            release.authorize_shared_final(candidate, {}, allow_reuse=True)
        native.assert_not_called()

    def test_exact_owner_retry_grant_still_allows_a_run_after_a_later_failure(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        prior = {**candidate, "configuration_sha": "f" * 40}
        step = {"status": "completed", "conclusion": "success", "started_at": "2026-10-19T12:02:00Z"}
        successful = {"candidate": prior,
                      "run": {"status": "completed", "conclusion": "success"},
                      "jobs": [{"name": release.FINAL_HEAVY_JOB, "status": "completed",
                                "conclusion": "success", "started_at": "2026-10-19T12:01:00Z",
                                "steps": [step]}]}
        failed = {"candidate": candidate,
                  "run": {"status": "completed", "conclusion": "failure"},
                  "jobs": [{"name": release.FINAL_HEAVY_JOB, "status": "completed",
                            "conclusion": "failure", "started_at": "2026-10-19T13:01:00Z",
                            "steps": [{"status": "completed", "conclusion": "failure",
                                       "started_at": "2026-10-19T13:02:00Z"}]}]}
        history = {"complete": True, "baseline": {"publication": {
                       "published_at": "2026-10-19T10:00:00Z"}},
                   "required_heavy_jobs": [release.FINAL_HEAVY_JOB],
                   "attempts": [successful, failed]}
        decision = {"decision": "allow", "candidate_id": "1" * 64, "grant_id": 41}
        module = Mock()
        module.authorize_final.return_value = decision
        created = {}

        def native_api(path, *, method="GET", data=None, token=None):
            if path.endswith("/actions/runs/90/attempts/1"):
                return {"id": 90, "run_attempt": 1}
            self.assertEqual(method, "POST")
            created.update({"id": 7, "body": data["body"],
                            "user": {"login": "owner", "type": "User"}})
            return created

        with (patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/zed", "GITHUB_SHA": "9" * 40,
                                     "GITHUB_RUN_ID": "90", "GITHUB_RUN_ATTEMPT": "1",
                                     "CANDIDATE_PR": "7"}, clear=True),
              patch.object(release, "final_attempt_history", return_value=(history, [])),
              patch.object(release, "reservations_from_history_comments", return_value=[]),
              patch.object(release.maintenance, "load", return_value=module),
              patch.object(release, "native_workflow_source", return_value={"verified": True}),
              patch.object(release, "owner_token", return_value="owner-token"),
              patch.object(release, "api", side_effect=native_api),
              patch.object(release, "pages", side_effect=lambda _path: [created])):
            result = release.authorize_shared_final(candidate, {})
        self.assertEqual(result["grant_id"], 41)
        self.assertEqual(result["artifact_workflow_sha"], "9" * 40)
        self.assertIn("fork-final:v1", created["body"])

    def test_preparation_reserves_with_explicit_pr_without_candidate_environment(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        prior = {**candidate, "configuration_sha": "f" * 40}
        step = {"status": "completed", "conclusion": "success", "started_at": "2026-10-19T12:02:00Z"}
        successful = {"candidate": prior,
                      "run": {"status": "completed", "conclusion": "success"},
                      "jobs": [{"name": release.FINAL_HEAVY_JOB, "status": "completed",
                                "conclusion": "success", "started_at": "2026-10-19T12:01:00Z",
                                "steps": [step]}]}
        failed = {"candidate": candidate,
                  "run": {"status": "completed", "conclusion": "failure"},
                  "jobs": [{"name": release.FINAL_HEAVY_JOB, "status": "completed",
                            "conclusion": "failure", "started_at": "2026-10-19T13:01:00Z",
                            "steps": [{"status": "completed", "conclusion": "failure",
                                       "started_at": "2026-10-19T13:02:00Z"}]}]}
        history = {"complete": True, "baseline": {"publication": {
                       "published_at": "2026-10-19T10:00:00Z"}},
                   "required_heavy_jobs": [release.FINAL_HEAVY_JOB],
                   "attempts": [successful, failed]}
        decision = {"decision": "allow", "candidate_id": "1" * 64, "grant_id": 41}
        module = Mock()
        module.authorize_final.return_value = decision
        created = {}
        posts = []
        reads = []
        def readback(path):
            reads.append(path)
            return [] if len(reads) == 1 else [{**created,
                "reactions": {"total_count": 1},
                "user": {**created["user"], "avatar_url": "changed-view"}}]

        def native_api(path, *, method="GET", data=None, token=None):
            if path.endswith("/actions/runs/90/attempts/1"):
                return {"id": 90, "run_attempt": 1}
            self.assertEqual(path, "repos/owner/zed/issues/7/comments")
            self.assertEqual(method, "POST")
            posts.append(path)
            created.update({"id": 7, "body": data["body"],
                            "user": {"login": "owner", "type": "User"}})
            return created

        with (patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/zed", "GITHUB_SHA": "9" * 40,
                                     "GITHUB_RUN_ID": "90", "GITHUB_RUN_ATTEMPT": "1"}, clear=True),
              patch.object(release, "final_attempt_history", return_value=(history, [])),
              patch.object(release, "reservations_from_history_comments", return_value=[]),
              patch.object(release.maintenance, "load", return_value=module),
              patch.object(release, "native_workflow_source", return_value={"verified": True}),
              patch.object(release, "owner_token", return_value="owner-token"),
              patch.object(release, "api", side_effect=native_api),
              patch.object(release, "pages", side_effect=readback),
              patch.object(release.time, "sleep") as pause):
            result = release.authorize_shared_final(candidate, {}, full_pr=7)
        self.assertEqual(result["grant_id"], 41)
        self.assertEqual(result["artifact_workflow_sha"], "9" * 40)
        self.assertIn("fork-final:v1", created["body"])
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(reads), 2)
        pause.assert_called_once_with(1)

    def test_a_changed_product_does_not_match_the_prior_repair_attempt(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        prior = {**candidate, "application_sha": "9" * 40, "configuration_sha": "f" * 40}
        successful = {"candidate": prior,
                      "run": {"status": "completed", "conclusion": "success"},
                      "jobs": [{"name": release.FINAL_HEAVY_JOB, "status": "completed",
                                "conclusion": "success", "started_at": "2026-10-19T12:01:00Z",
                                "steps": [{"status": "completed", "conclusion": "success",
                                           "started_at": "2026-10-19T12:02:00Z"}]}]}
        self.assertFalse(release._successful_product_attempt_under_other_config(
            candidate, {"required_heavy_jobs": [release.FINAL_HEAVY_JOB],
                        "attempts": [successful]}))

    def test_only_the_exact_native_validated_attempt_artifact_is_reused(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        candidate_id, run_id, attempt = "f" * 64, 12, 3
        names = release.candidate_artifact_names(candidate["application_sha"], run_id, attempt)
        final = {"schema": 1, "candidate_id": candidate_id, "run_id": run_id,
                 "run_attempt": attempt, "workflow_sha": "9" * 40}
        comment = {"id": 7, "created_at": "2026-10-19T11:00:00Z", "updated_at": "2026-10-19T11:00:00Z",
                   "user": {"login": "owner", "type": "User"}, "body": "<!-- fork-candidate:v1 "
                   + json.dumps(candidate, sort_keys=True, separators=(",", ":"))
                   + " -->\n<!-- fork-final:v1 "
                   + json.dumps(final, sort_keys=True, separators=(",", ":")) + " -->"}
        artifacts = [{"id": index, "name": name, "expired": False, "size_in_bytes": 100,
                      "digest": "sha256:" + "a" * 64,
                      "workflow_run": {"id": run_id}}
                     for index, name in enumerate(names.values(), start=1)]
        decision = {"decision": "already-validated", "candidate_id": candidate_id, "validated_run": {
            "id": run_id, "run_attempt": attempt}}
        module = Mock()
        module.candidate_identity.return_value = candidate_id
        module.authorize_final.return_value = decision
        with (patch.dict(os.environ, {"GITHUB_RUN_ID": "90", "GITHUB_RUN_ATTEMPT": "1"}, clear=True),
              patch.object(release, "final_attempt_history", return_value=(
                  {"complete": True, "baseline": {"publication": {"published_at": "2026-10-19T10:00:00Z"}}},
                  [comment, {**comment, "id": 8, "user": {"login": "other", "type": "User"}}])),
              patch.object(release, "reservations_from_history_comments", return_value=[]),
              patch.object(release.maintenance, "load", return_value=module),
              patch.object(release, "api", return_value={"total_count": 2, "artifacts": artifacts})):
            result = release.authorize_shared_final(candidate, {})
        self.assertEqual(result["artifact_workflow_sha"], final["workflow_sha"])
        self.assertEqual(result["artifact_names"], names)
        self.assertEqual(result["artifact_ids"], {name: index for index, name in enumerate(names.values(), 1)})
        module.authorize_final.assert_called_once()

    def test_logical_artifact_keys_do_not_stand_in_for_exact_native_artifacts(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        candidate_id, run_id, attempt = "f" * 64, 12, 3
        names = release.candidate_artifact_names(candidate["application_sha"], run_id, attempt)
        final = {"schema": 1, "candidate_id": candidate_id, "run_id": run_id,
                 "run_attempt": attempt, "workflow_sha": "9" * 40}
        comment = {"id": 7, "created_at": "2026-10-19T11:00:00Z", "updated_at": "2026-10-19T11:00:00Z",
                   "user": {"login": "owner", "type": "User"}, "body": "<!-- fork-candidate:v1 "
                   + json.dumps(candidate, sort_keys=True, separators=(",", ":"))
                   + " -->\n<!-- fork-final:v1 "
                   + json.dumps(final, sort_keys=True, separators=(",", ":")) + " -->"}
        artifacts = [{"id": index, "name": role, "expired": False, "size_in_bytes": 100,
                      "digest": "sha256:" + "a" * 64,
                      "workflow_run": {"id": run_id}}
                     for index, role in enumerate(names, start=1)]
        module = Mock()
        module.candidate_identity.return_value = candidate_id
        module.authorize_final.return_value = {"decision": "already-validated", "candidate_id": candidate_id,
            "validated_run": {"id": run_id, "run_attempt": attempt}}
        with (patch.dict(os.environ, {"GITHUB_RUN_ID": "90", "GITHUB_RUN_ATTEMPT": "1"}, clear=True),
              patch.object(release, "final_attempt_history", return_value=(
                  {"complete": True, "baseline": {"publication": {"published_at": "2026-10-19T10:00:00Z"}}},
                  [comment])),
              patch.object(release, "reservations_from_history_comments", return_value=[]),
              patch.object(release.maintenance, "load", return_value=module),
              patch.object(release, "api", return_value={"total_count": 2, "artifacts": artifacts}),
              self.assertRaisesRegex(ValueError, "missing one exact artifact")):
            release.authorize_shared_final(candidate, {})

    def test_adopted_repair_receipt_keeps_the_runtime_config_and_all_asset_blobs_bound(self):
        repo, version = "owner/zed", "1.2.3"
        candidate = {"schema": 1, "repository": repo, "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        runtime_config, workflow_sha = "f" * 40, "9" * 40
        release_id = 123
        tag = release.release_tag(version, candidate["upstream_sha"],
                                  candidate["patchset_sha256"], candidate["application_sha"])
        symbols = f"zed-{version}-titlebarless-debug-symbols-macos-aarch64.tar.gz"
        payloads = {"Zed.dmg": b"reviewed runtime DMG", symbols: b"reviewed matching symbols"}
        asset_info = {name: {"size": len(data), "digest": hashlib.sha256(data).hexdigest()}
                      for name, data in payloads.items()}
        identity = {"schema": 1, "repository": repo, "upstream_repository": release.UPSTREAM,
                    "platform": "macos/aarch64", "candidate_sha": candidate["application_sha"],
                    "upstream_sha": candidate["upstream_sha"],
                    "patchset_sha256": candidate["patchset_sha256"], "config_sha": runtime_config,
                    "version": version, "release_id": release_id}
        provenance = {**identity, "archive": "Zed.dmg",
                      "archive_sha256": asset_info["Zed.dmg"]["digest"],
                      "archive_size": asset_info["Zed.dmg"]["size"],
                      "symbols_archive": symbols, "symbols_sha256": asset_info[symbols]["digest"],
                      "symbols_size": asset_info[symbols]["size"],
                      "binary_uuid": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
                      "remote_server_uuid": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"}
        provenance_bytes = (json.dumps(provenance, indent=2) + "\n").encode()
        manifest = {key: identity[key] for key in (
            "schema", "version", "release_id", "platform", "candidate_sha",
            "upstream_sha", "patchset_sha256")}
        manifest.update(url=f"https://github.com/{repo}/releases/download/{tag}/Zed.dmg",
                        sha256=asset_info["Zed.dmg"]["digest"], size=asset_info["Zed.dmg"]["size"])
        manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
        payloads.update({"provenance.json": provenance_bytes, "latest.json": manifest_bytes})
        checksums = {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}
        sums = "".join(f"{checksums[name]}  {name}\n" for name in sorted(checksums))
        payloads["SHA256SUMS"] = sums.encode()
        receipt_assets = {name: {"size": len(data),
                                 "sha256": hashlib.sha256(data).hexdigest()}
                          for name, data in sorted(payloads.items())}
        receipt = {"schema": 1, "repository": repo, "candidate": candidate,
                   "candidate_id": "1" * 64,
                   "validation_run": {"id": 12, "run_attempt": 3,
                                      "workflow_sha": workflow_sha},
                   "release": {"version": version, "release_id": release_id, "tag": tag},
                   "assets": receipt_assets}
        module = Mock()
        module.candidate_identity.return_value = receipt["candidate_id"]
        environment = {"GITHUB_REPOSITORY": repo, "GITHUB_SHA": workflow_sha,
                       "CONFIG_SHA": runtime_config, "APP_VERSION": version,
                       "RELEASE_ID": str(release_id), "RELEASE_TAG": tag}

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "release"
            directory.mkdir()
            for name, data in payloads.items():
                (directory / name).write_bytes(data)
            receipt_path = Path(temporary) / "receipt.json"
            receipt_path.write_text(json.dumps(receipt))
            with (patch.dict(os.environ, environment, clear=True),
                  patch.object(release, "build_identity", return_value=identity),
                  patch.object(release.maintenance, "load", return_value=module)):
                verified = release.verify_artifact_receipt(
                    directory, receipt_path, candidate, run_id=12, run_attempt=3,
                    workflow_sha=workflow_sha)
                self.assertEqual(verified["candidate"], candidate)
                self.assertEqual(identity["config_sha"], runtime_config)
                with patch.dict(os.environ, {"CONFIG_SHA": "7" * 40}), \
                        self.assertRaisesRegex(ValueError, "Artifact identity differs"):
                    release.verify_artifact_receipt(
                        directory, receipt_path, candidate, run_id=12, run_attempt=3,
                        workflow_sha=workflow_sha)

    def test_incomplete_native_artifact_inventory_blocks_reuse(self):
        candidate = {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                     "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                     "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        final = {"schema": 1, "candidate_id": "f" * 64, "run_id": 12,
                 "run_attempt": 3, "workflow_sha": "9" * 40}
        comment = {"id": 7, "created_at": "2026-10-19T11:00:00Z", "updated_at": "2026-10-19T11:00:00Z",
                   "user": {"login": "owner", "type": "User"}, "body": "<!-- fork-candidate:v1 "
                   + json.dumps(candidate, sort_keys=True, separators=(",", ":"))
                   + " -->\n<!-- fork-final:v1 "
                   + json.dumps(final, sort_keys=True, separators=(",", ":")) + " -->"}
        module = Mock()
        module.candidate_identity.return_value = final["candidate_id"]
        module.authorize_final.return_value = {"decision": "already-validated", "candidate_id": final["candidate_id"],
            "validated_run": {"id": 12, "run_attempt": 3}}
        with (patch.dict(os.environ, {"GITHUB_RUN_ID": "90", "GITHUB_RUN_ATTEMPT": "1"}, clear=True),
              patch.object(release, "final_attempt_history", return_value=(
                  {"complete": True, "baseline": {"publication": {"published_at": "2026-10-19T10:00:00Z"}}},
                  [comment, {**comment, "id": 8, "user": {"login": "other", "type": "User"}}])),
              patch.object(release, "reservations_from_history_comments", return_value=[]),
              patch.object(release.maintenance, "load", return_value=module),
              patch.object(release, "api", return_value={"total_count": 0, "artifacts": []}),
              self.assertRaisesRegex(ValueError, "missing one exact artifact")):
            release.authorize_shared_final(candidate, {})


class SharedNativeHeavyHistoryTests(unittest.TestCase):
    def test_unreserved_heavy_steps_without_native_start_make_history_incomplete(self):
        repo = "owner/zed"
        source = {"schema": 1, "repository": repo, "upstream_sha": "a" * 40,
                  "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                  "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                  "platform": "macos/aarch64"}
        publication = {"kind": "github-release", "id": 71,
                       "published_at": "2026-10-19T10:00:00Z",
                       "metadata_sha256": "d" * 64}
        baseline_value = {"schema": 1, "repository": repo,
                          "candidate": source, "publication": publication}
        baseline_proof = {"variable": {"value": json.dumps(baseline_value)}}
        native_run = {
            "id": 42, "run_attempt": 1, "path": ".github/workflows/release.yml",
            "created_at": "2026-10-19T10:01:00Z", "updated_at": "2026-10-19T10:05:00Z",
            "status": "completed", "conclusion": "failure", "head_sha": "4" * 40,
        }
        job = {
            "id": 43, "name": release.FINAL_HEAVY_JOB, "status": "completed",
            "conclusion": "failure", "started_at": None,
            "steps": [{"name": "Run Rust regressions", "status": "completed",
                       "conclusion": "failure"}],
        }

        def native_pages(_path, field):
            if field == "workflow_runs":
                return [{**native_run, "run_attempt": 1}]
            if field == "jobs":
                return [job]
            self.fail(f"Unexpected native history field: {field}")

        with (patch.dict(os.environ, {"GITHUB_RUN_ID": "90", "GITHUB_RUN_ATTEMPT": "1"}, clear=True),
              patch.object(release, "pages", return_value=[]),
              patch.object(release, "maintenance_pages", side_effect=native_pages),
              patch.object(release, "api", return_value=native_run)):
            history, _comments = release.final_attempt_history(source, baseline_proof)
        self.assertFalse(history["complete"])
        self.assertEqual(history["attempts"], [])

    def test_reserved_heavy_steps_without_native_start_are_rejected_before_activation(self):
        repo, candidate_id, workflow_sha = "owner/zed", "f" * 64, "4" * 40
        source = {"schema": 1, "repository": repo, "upstream_sha": "1" * 40,
                  "application_sha": "2" * 40, "configuration_sha": "3" * 40,
                  "patchset_sha256": "5" * 64, "recipe_sha256": "6" * 64,
                  "platform": "macos/aarch64"}
        final = {"schema": 1, "candidate_id": candidate_id, "run_id": 99,
                 "run_attempt": 1, "workflow_sha": workflow_sha}
        comment = {
            "id": 101, "created_at": "2026-10-19T10:01:00Z", "updated_at": "2026-10-19T10:01:00Z",
            "user": {"login": "github-actions[bot]", "type": "Bot"},
            "body": ("<!-- fork-candidate:v1 "
                     + json.dumps(source, sort_keys=True, separators=(",", ":"))
                     + " -->\n<!-- fork-final:v1 "
                     + json.dumps(final, sort_keys=True, separators=(",", ":")) + " -->"),
        }
        publication = {"kind": "github-release", "id": 71,
                       "published_at": "2026-10-19T10:00:00Z",
                       "metadata_sha256": "d" * 64}
        variable = {"name": "FORK_ACCEPTED_BASELINE",
                    "value": json.dumps({"schema": 1, "repository": repo,
                                         "candidate": source, "publication": publication})}
        proof = {"endpoint": f"repos/{repo}/actions/variables/FORK_ACCEPTED_BASELINE",
                 "variable": variable}
        run = {"id": 99, "run_attempt": 1, "path": ".github/workflows/release.yml",
               "created_at": "2026-10-19T10:00:00Z", "head_sha": workflow_sha}
        job = {"id": 100, "name": release.FINAL_HEAVY_JOB, "status": "completed",
               "conclusion": "failure", "started_at": None,
               "steps": [{"name": "Run Rust regressions", "status": "completed",
                          "conclusion": "failure"}]}
        lifecycle = Mock()
        lifecycle.candidate_identity.return_value = candidate_id
        lifecycle.validate_workflow_source.return_value = True

        def native(route, **_kwargs):
            if route == proof["endpoint"]:
                return variable
            if route == f"repos/{repo}/actions/runs/99/attempts/1":
                return run
            self.fail(f"Unexpected native read: {route}")

        with (patch.dict(os.environ, {"GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1",
                                     "GITHUB_SHA": workflow_sha}, clear=True),
              patch.object(release.maintenance, "load", return_value=lifecycle),
              patch.object(release, "api", side_effect=native),
              patch.object(release, "pages", return_value=[comment]),
              patch.object(release, "maintenance_pages", return_value=[job]),
              self.assertRaisesRegex(ValueError, "no native start timestamp")):
            release.verify_shared_final_reservation(
                source, candidate_id=candidate_id, run_id="99", run_attempt="1",
                workflow_sha=workflow_sha, baseline_proof=proof)

        foreign = {"user": {"login": "other", "type": "User"},
                   "body": "<!-- fork-final:v1 {malformed -->"}
        environment = {"GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": workflow_sha}
        with (patch.dict(os.environ, environment, clear=True),
              patch.object(release.maintenance, "load", return_value=lifecycle),
              patch.object(release, "api", side_effect=native),
              patch.object(release, "pages", return_value=[foreign, comment]),
              patch.object(release, "maintenance_pages", return_value=[
                  {"name": release.FINAL_HEAVY_JOB, "conclusion": "skipped", "steps": []}])):
            self.assertEqual(release.verify_shared_final_reservation(source,
                candidate_id=candidate_id, run_id="99", run_attempt="1",
                workflow_sha=workflow_sha, baseline_proof=proof), comment)
            for bad in ({**comment, "id": True},
                        {**comment, "updated_at": "2026-10-19T10:01:01Z"}):
                with (self.subTest(comment=bad), patch.object(release, "pages", return_value=[bad]),
                      self.assertRaisesRegex(ValueError, "Native final comment identity")):
                    release.verify_shared_final_reservation(source, candidate_id=candidate_id,
                        run_id="99", run_attempt="1", workflow_sha=workflow_sha, baseline_proof=proof)


class BootstrapTrustBoundaryTests(unittest.TestCase):
    def test_private_bundle_precedes_candidate_controller_and_pin(self):
        workflow = (release.ROOT / ".github/workflows/release.yml").read_text()
        self.assertNotIn("  bootstrap-policy:", workflow)
        self.assertNotIn("needs.bootstrap-policy", workflow)
        verifier = workflow.split("\n  verify-bootstrap-bundle:\n", 1)[1]
        check = verifier.split("      - name: Verify The Complete Native Pair And Private Policy Receipt\n", 1)[1]
        script = check.split("        run: |\n", 1)[1].split("\n          PY", 1)[0]
        self.assertIn("import maintenance", script)
        self.assertNotIn("importlib.import_module('maintenance')", script)
        self.assertIn("maintenance.validated_bundle(candidate, comments, read_native)", script)
        self.assertNotIn("importlib.import_module('release')", script)
        preparation = workflow.split("\n  prepare:\n", 1)[1].split("\n  policy-check:\n", 1)[0]
        self.assertIn("needs: [verify-bootstrap-bundle]", preparation)
        self.assertIn("needs.verify-bootstrap-bundle.result == 'success'", preparation)
        self.assertIn("needs.verify-bootstrap-bundle.outputs.policy_ref", preparation)
        self.assertLess(preparation.index("Prepare The Exact Source And Review"),
                        preparation.index("Record A Successful Read-Only Bootstrap"))
        self.assertNotIn("gh variable set ZED_TRUSTED_POLICY", preparation)
        policy = workflow.split("\n  policy-check:\n", 1)[1].split("\n  test:\n", 1)[0]
        self.assertIn("needs: [prepare]", policy)
        self.assertNotIn("bootstrap-policy", policy)
        activation = workflow.split("\n  activate-policy:\n", 1)[1].split("\n  test:\n", 1)[0]
        self.assertIn("needs: [prepare, policy-check, verify-bootstrap-bundle]", activation)
        self.assertLess(activation.index("Recheck The Exact Shared Reviews"),
                        activation.index("Pin The Checked Controller For Subsequent Runs"))
        repair = workflow.split("\n  repair:\n", 1)[1]
        self.assertIn("needs.verify-bootstrap-bundle.result == 'failure'", repair)
        self.assertIn("needs.verify-bootstrap-bundle.outputs.policy_ref", repair)
        self.assertIn("BOOTSTRAP_BUNDLE_FAILED", repair)
        self.assertNotIn("bootstrap-policy", repair)


class SharedPolicyOutputBoundaryTests(unittest.TestCase):
    def test_shared_policy_sandbox_suppresses_raw_candidate_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trusted, candidate, private = root / "trusted", root / "candidate", root / "maintenance"
            (trusted / "scripts").mkdir(parents=True)
            (candidate / "scripts").mkdir(parents=True)
            private.mkdir()
            (trusted / "scripts/test_trusted.py").write_text("# frozen public test\n")
            (candidate / "scripts/test_candidate.py").write_text("# frozen public test\n")
            output = io.StringIO()
            calls = []

            def docker_run(args, **kwargs):
                calls.append((args, kwargs))
                self.assertEqual(kwargs["stdout"], subprocess.PIPE)
                self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
                self.assertFalse(kwargs["check"])
                self.assertEqual(kwargs["timeout"], 180)
                self.assertIn("--log-driver=none", args)
                launcher = args[args.index("-c") + 1]
                self.assertIn("ZED_POLICY_SUITE_COMPLETE:", launcher)
                self.assertIn("result.testsRun != expected", launcher)
                return type("Result", (), {"returncode": 1,
                    "stdout": "PRIVATE_TEST_OUTPUT", "stderr": "PRIVATE_TEST_OUTPUT"})()

            with (patch.object(release, "ROOT", trusted),
                  patch.dict(os.environ, {"FORK_MAINTENANCE_ROOT": str(private)}, clear=True),
                  patch.object(release.subprocess, "run", side_effect=docker_run),
                  patch.object(release, "urlopen", side_effect=AssertionError("Failed policy must stop before lint")),
                  patch("sys.stdout", output), self.assertRaises(subprocess.CalledProcessError) as raised):
                release.check_policy(candidate)
            self.assertEqual(len(calls), 2)
            self.assertIn("Policy suite failed", output.getvalue())
            self.assertIn("test_trusted.py", output.getvalue())
            self.assertIn("test_candidate.py", output.getvalue())
            self.assertNotIn("PRIVATE_TEST_OUTPUT", output.getvalue())
            self.assertNotIn("PRIVATE_TEST_OUTPUT", str(raised.exception))

    def test_shared_policy_rejects_zero_exit_without_a_complete_suite_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trusted, candidate, private = root / "trusted", root / "candidate", root / "maintenance"
            (trusted / "scripts").mkdir(parents=True)
            (candidate / "scripts").mkdir(parents=True)
            private.mkdir()
            (trusted / "scripts/test_trusted.py").write_text("# frozen public test\n")
            (candidate / "scripts/test_candidate.py").write_text("# frozen public test\n")
            calls = []

            def docker_run(args, **kwargs):
                calls.append((args, kwargs))
                return type("Result", (), {"returncode": 0,
                    "stdout": "ZED_POLICY_SUITE_STARTED\n", "stderr": ""})()

            with (patch.object(release, "ROOT", trusted),
                  patch.dict(os.environ, {"FORK_MAINTENANCE_ROOT": str(private)}, clear=True),
                  patch.object(release.subprocess, "run", side_effect=docker_run),
                  patch.object(release, "urlopen", side_effect=AssertionError("Failed policy must stop before lint")),
                  self.assertRaises(subprocess.CalledProcessError) as failure):
                release.check_policy(candidate)

        self.assertEqual(len(calls), 2)
        self.assertEqual(failure.exception.returncode, 1)

    def test_shared_policy_accepts_one_complete_marker_for_each_frozen_suite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trusted, candidate, private = root / "trusted", root / "candidate", root / "maintenance"
            (trusted / "scripts").mkdir(parents=True)
            (candidate / "scripts").mkdir(parents=True)
            private.mkdir()
            (trusted / "scripts/test_trusted.py").write_text("# frozen public test\n")
            (candidate / "scripts/test_candidate.py").write_text("# frozen public test\n")
            calls = []

            def docker_run(args, **kwargs):
                calls.append((args, kwargs))
                name = args[-1]
                return type("Result", (), {"returncode": 0,
                    "stdout": f"ZED_POLICY_SUITE_COMPLETE:1:{name}:"
                              + ("3" if name == "test_trusted.py" else "718263" * 25) + "\n",
                    "stderr": ""})()

            output = io.StringIO()
            with (patch.object(release, "ROOT", trusted),
                  patch.dict(os.environ, {"FORK_MAINTENANCE_ROOT": str(private)}, clear=True),
                  patch.object(release.subprocess, "run", side_effect=docker_run),
                  patch.object(release, "urlopen", side_effect=AssertionError("linter reached")),
                  patch("sys.stdout", output),
                  self.assertRaisesRegex(AssertionError, "linter reached")):
                release.check_policy(candidate)

        self.assertEqual(len(calls), 2)
        self.assertIn("Policy suite passed: test_trusted.py", output.getvalue())
        self.assertIn("Policy suite passed: test_candidate.py", output.getvalue())
        self.assertNotIn("718263" * 25, output.getvalue())


class FrozenPublicExecutionTests(unittest.TestCase):
    def candidate(self):
        return {"schema": 1, "repository": "owner/zed", "upstream_sha": "a" * 40,
                "application_sha": "b" * 40, "configuration_sha": "c" * 40,
                "patchset_sha256": "d" * 64, "recipe_sha256": "e" * 64,
                "platform": "macos/aarch64"}

    def test_exact_app_freeze_is_checked_without_private_source(self):
        candidate = self.candidate()
        identity = hashlib.sha256(json.dumps(candidate, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        environment = {"MAINTENANCE_CANDIDATE": json.dumps(candidate),
            "GITHUB_REPOSITORY": candidate["repository"],
            "REVIEW_CONFIG_SHA": candidate["configuration_sha"],
            "CANDIDATE_SHA": candidate["application_sha"], "UPSTREAM_SHA": candidate["upstream_sha"],
            "PATCHSET_SHA256": candidate["patchset_sha256"], "FORK_FINAL_CANDIDATE": identity,
            "GITHUB_RUN_ID": "12", "FORK_FINAL_RUN_ID": "12",
            "GITHUB_RUN_ATTEMPT": "1", "FORK_FINAL_ATTEMPT": "1",
            "GITHUB_SHA": "f" * 40, "FORK_FINAL_WORKFLOW_SHA": "f" * 40}
        with (patch.dict(os.environ, environment, clear=True),
              patch.object(release.maintenance, "load", side_effect=AssertionError("private access"))):
            self.assertEqual(release.check_shared_execution(), candidate)
            with patch.dict(os.environ, {"FORK_FINAL_ATTEMPT": "2"}), self.assertRaises(ValueError):
                release.check_shared_execution()
        for malformed in ({**candidate, "schema": True}, {**candidate, "extra": "unexpected"},
                          {**candidate, "application_sha": "invalid"}):
            with self.subTest(candidate=malformed), self.assertRaises(ValueError):
                release.frozen_candidate_identity(malformed)

    def test_macos_job_never_acquires_private_source_or_key(self):
        workflow = (release.ROOT / ".github/workflows/release.yml").read_text()
        product = workflow.split("\n  test:\n", 1)[1].split("\n  publish:\n", 1)[0]
        self.assertNotIn("repository: warelik/machine-maintenance", product)
        self.assertNotIn("MAINTENANCE_READ_KEY", product)
        self.assertIn('FORK_MAINTENANCE_ROOT: ""', product)
        self.assertLess(product.index("check-shared-execution"),
                        product.index("Run Every Preserved Rust Regression"))
        self.assertIn("record-artifact-receipt", product)

    def test_macos_artifact_receipt_path_is_exported_before_production(self):
        workflow = (release.ROOT / ".github/workflows/release.yml").read_text()
        product = workflow.split("\n  test:\n", 1)[1].split("\n  publish:\n", 1)[0]
        job_environment = product.split("    env:\n", 1)[1].split("    steps:\n", 1)[0]
        self.assertNotIn("ARTIFACT_RECEIPT_PATH", job_environment)

        frozen_attempt = product.split(
            "      - name: Require The Original Frozen Application Run Attempt\n", 1)[1]
        frozen_attempt = frozen_attempt.split("      - uses: actions/checkout@v4", 1)[0]
        assignment = 'echo "ARTIFACT_RECEIPT_PATH=$RUNNER_TEMP/zed-artifact-receipt.json" >> "$GITHUB_ENV"'
        self.assertIn(assignment, frozen_attempt)
        producer = product.index("      - name: Record The Exact Native Artifact Attempt\n")
        self.assertLess(product.index(assignment), producer)

        self.assertIn("path: ${{ runner.temp }}/zed-artifact-receipt.json", product)
        publish = workflow.split("\n  publish:\n", 1)[1]
        self.assertIn(
            "ARTIFACT_RECEIPT_PATH: ${{ runner.temp }}/zed-artifact-receipt/zed-artifact-receipt.json",
            publish)

    def test_bootstrap_failure_skips_private_acquisition_before_escalation(self):
        workflow = (release.ROOT / ".github/workflows/release.yml").read_text()
        repair = workflow.split("\n  repair:\n", 1)[1].split("\n  verify-bootstrap-bundle:\n", 1)[0]
        for step in repair.split("      - ")[1:]:
            if any(value in step for value in ("Read The Exact Private Maintenance Lock",
                                               "repository: warelik/machine-maintenance",
                                               "Record The Acquired Private Source Identity")):
                self.assertIn("if: needs.verify-bootstrap-bundle.result != 'failure'", step)
        self.assertIn("Repair The Actual Failure", repair)
        self.assertIn("release.escalation", repair)
        self.assertIn("73aede3a146982a9d47b195f6311cf5bca7410b7", repair)
        self.assertNotIn("dbf40ff4bd626082e0ad3d9873c5b6ad5fa7f941", repair)

    def test_source_and_test_changes_do_not_synthesize_finding_resolution(self):
        finding = {"finding_id": "inline:1:9", "path": "scripts/release.py", "line": 10}
        with patch.object(release.subprocess, "check_output") as inspect_diff:
            self.assertEqual(release.review_dispositions([finding],
                {finding["finding_id"]: {"configuration_sha": "a" * 40}}, self.candidate()), [])
        inspect_diff.assert_not_called()
        self.assertEqual(finding["finding_id"], "inline:1:9")

    def test_exact_codex_proposal_becomes_one_immutable_owner_batch_after_pair(self):
        candidate = self.candidate()
        repo, source, seed = candidate["repository"], candidate["configuration_sha"], "1" * 40
        finding = {"finding_id": "inline:91:17", "candidate_id": "2" * 64,
                   "source_sha": "3" * 40, "path": "src/state.rs", "line": 17}
        unresolved = [finding]
        pair = {"state": "findings", "complete": True}
        state = {"number": 91, "seed": seed, "head": source,
                 "changed_paths": ["scripts/release.py", "scripts/test_maintenance.py"]}
        code_excerpt = "def review_dispositions"
        regression_case = "test_exact_codex_proposal_becomes_one_immutable_owner_batch_after_pair"
        regression_excerpt = f"def {regression_case}"
        code_diff = b"+def review_dispositions(known, origins, candidate):\n+    return []\n"
        regression_diff = b"+def " + regression_case.encode() + b"(self):\n+    self.assertTrue(True)\n"
        evidence = (
            f"code location `scripts/release.py` at `{code_excerpt}`; "
            f"code diff SHA-256 `{hashlib.sha256(code_diff).hexdigest()}`; "
            f"regression path `scripts/test_maintenance.py`; "
            f"regression diff SHA-256 `{hashlib.sha256(regression_diff).hexdigest()}`; "
            f"regression case `{regression_case}`; "
            f"regression location `{regression_excerpt}`; "
            "explanation `the old empty result kept this finding open and this regression checks the owner batch`;"
        )
        proposal = {"schema": 1, "source_sha": source,
                    "findings": [{"finding_id": finding["finding_id"],
                                  "status": "fixed", "evidence": evidence}]}
        stamp = "2026-10-20T12:00:00Z"
        proposal_comment = {"id": 33, "created_at": stamp, "updated_at": stamp,
                            "user": {"login": release.BOT, "type": "Bot"},
                            "performed_via_github_app": {"slug": release.APP},
                            "body": "Machine repair evidence.\n<!-- fork-review-disposition:v1 "
                                    + json.dumps(proposal, sort_keys=True, separators=(",", ":"))
                                    + " -->"}
        fix_request = {"id": 32, "created_at": "2026-10-20T11:59:00Z",
                       "updated_at": "2026-10-20T11:59:00Z",
                       "user": {"login": "owner", "type": "User"},
                       "body": f"Repair request.\n<!-- zed-fix:{seed}:1 -->"}
        diffs = {"scripts/release.py": code_diff,
                 "scripts/test_maintenance.py": regression_diff}
        sources = {"scripts/release.py": (code_excerpt + "()\n"),
                   "scripts/test_maintenance.py": regression_excerpt + "(self):\n"}
        with (patch.object(release, "native_source_file",
                           side_effect=lambda _repo, _sha, path: sources[path].encode()),
              patch.object(release.subprocess, "check_output",
                           side_effect=lambda args, **_kwargs: diffs[args[-1]])):
            dispositions = release.review_dispositions(
                [finding], {}, candidate, fresh_ids=(), unresolved=unresolved,
                repair_state=state, repair_comments=[fix_request, proposal_comment], pair=pair)
        self.assertEqual(len(dispositions), 1)
        self.assertEqual(dispositions[0]["finding_id"], finding["finding_id"])
        self.assertEqual(dispositions[0]["proposal_comment"], proposal_comment)

        owner_id = hashlib.sha256(json.dumps(
            candidate, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        private = Mock()
        private.candidate_identity.return_value = owner_id
        created = {"id": 34, "created_at": "2026-10-20T12:01:00Z",
                   "updated_at": "2026-10-20T12:01:00Z",
                   "user": {"login": "owner", "type": "User"}}
        posted = []

        def native_api(route, *, method="GET", data=None, token=None):
            self.assertEqual(method, "POST")
            self.assertEqual(route, f"repos/{repo}/issues/91/comments")
            self.assertEqual(token, "owner-token")
            created["body"] = data["body"]
            posted.append(created.copy())
            return created.copy()

        def native_pages(_route):
            return [fix_request, proposal_comment, *posted]

        with (patch.object(release, "owner_token", return_value="owner-token"),
              patch.object(release, "pages", side_effect=native_pages),
              patch.object(release, "api", side_effect=native_api),
              patch.object(release.maintenance, "load", return_value=private)):
            result = release.post_review_dispositions(repo, 91, candidate, dispositions)
            again = release.post_review_dispositions(repo, 91, candidate, dispositions)
        self.assertEqual(len(posted), 1)
        self.assertEqual(result, again)
        owner_dispositions = release.review_dispositions_from_comments(result, candidate)
        self.assertEqual([item["finding_id"] for item in owner_dispositions], [finding["finding_id"]])
        self.assertEqual(owner_dispositions[0]["native_comment"]["id"], 34)

    def test_disposition_proposal_rejects_partial_stale_duplicate_edited_and_copied_records(self):
        candidate = self.candidate()
        repo, source = candidate["repository"], candidate["configuration_sha"]
        finding = {"finding_id": "inline:91:17", "candidate_id": "2" * 64}
        state = {"number": 91, "seed": "1" * 40, "head": source,
                 "changed_paths": ["scripts/release.py", "scripts/test_maintenance.py"]}
        pair = {"state": "findings", "complete": True}
        code_diff, regression_diff = b"changed code\n", b"changed test\n"
        evidence = (
            f"code location `scripts/release.py` at `def review_dispositions`; "
            f"code diff SHA-256 `{hashlib.sha256(code_diff).hexdigest()}`; "
            "regression path `scripts/test_maintenance.py`; "
            f"regression diff SHA-256 `{hashlib.sha256(regression_diff).hexdigest()}`; "
            "regression case `test_disposition_proposal`; "
            "regression location `def test_disposition_proposal`; "
            "explanation `the old path omitted this finding and the new regression checks resolution`;"
        )
        finding_record = {"finding_id": finding["finding_id"], "status": "disproved",
                          "evidence": evidence}
        stamp = "2026-10-20T12:00:00Z"
        request = {"id": 40, "created_at": "2026-10-20T11:00:00Z",
                   "updated_at": "2026-10-20T11:00:00Z",
                   "user": {"login": "owner", "type": "User"},
                   "body": f"Repair request.\n<!-- zed-fix:{state['seed']}:1 -->"}

        def comment(record, *, authored_by=release.BOT, updated=stamp):
            return {"id": 41, "created_at": stamp, "updated_at": updated,
                    "user": {"login": authored_by, "type": "Bot"},
                    "performed_via_github_app": {"slug": release.APP},
                    "body": "<!-- fork-review-disposition:v1 " +
                            json.dumps(record, sort_keys=True, separators=(",", ":")) + " -->"}

        base = {"schema": 1, "source_sha": source, "findings": [finding_record]}
        sources = {"scripts/release.py": "def review_dispositions():\n",
                   "scripts/test_maintenance.py": "def test_disposition_proposal():\n"}
        diffs = {"scripts/release.py": code_diff,
                 "scripts/test_maintenance.py": regression_diff}

        def evaluate(record, comments, *, fresh_ids=()):
            with (patch.object(release, "native_source_file",
                               side_effect=lambda _repo, _sha, path: sources[path].encode()),
                  patch.object(release.subprocess, "check_output",
                               side_effect=lambda args, **_kwargs: diffs[args[-1]])):
                return release.review_dispositions(
                    [finding], {}, candidate, fresh_ids=fresh_ids, unresolved=[finding],
                    repair_state=state, repair_comments=comments, pair=pair)

        invalid_cases = [
            ("partial IDs", {**base, "findings": []}, [comment({**base, "findings": []})], ()),
            ("stale source", {**base, "source_sha": "9" * 40},
             [comment({**base, "source_sha": "9" * 40})], ()),
            ("duplicate proposal", base, [comment(base), {**comment(base), "id": 42}], ()),
            ("edited proposal", base, [comment(base, updated="2026-10-20T12:01:00Z")], ()),
            ("copied marker", base, [comment(base, authored_by="someone-else")], ()),
            ("fresh finding marked fixed", {**base, "findings": [{**finding_record, "status": "fixed"}]},
             [comment({**base, "findings": [{**finding_record, "status": "fixed"}]})],
             (finding["finding_id"],)),
        ]
        for name, _record, comments, fresh_ids in invalid_cases:
            with self.subTest(case=name), self.assertRaises(ValueError):
                evaluate(_record, [request, *comments], fresh_ids=fresh_ids)
        with patch.object(release.subprocess, "check_output") as inspect_diff:
            self.assertEqual(evaluate(base, []), [])
        inspect_diff.assert_not_called()

    def test_new_fix_request_retires_old_malformed_report_and_same_second_order_uses_comment_id(self):
        candidate = self.candidate()
        source, repo = candidate["configuration_sha"], candidate["repository"]
        finding = {"finding_id": "inline:91:17", "candidate_id": "2" * 64}
        state = {"number": 91, "seed": "1" * 40, "head": source,
                 "changed_paths": ["scripts/release.py", "scripts/test_maintenance.py"]}
        pair = {"state": "findings", "complete": True}
        code_diff, regression_diff = b"", b""
        evidence = (
            f"code location `scripts/release.py` at `def review_dispositions`; "
            f"code diff SHA-256 `{hashlib.sha256(code_diff).hexdigest()}`; "
            "regression path `scripts/test_maintenance.py`; "
            f"regression diff SHA-256 `{hashlib.sha256(regression_diff).hexdigest()}`; "
            "regression case `test_existing_finding`; "
            "regression location `def test_existing_finding`; "
            "explanation `the unchanged source is inspected and this existing regression proves no defect`;"
        )
        record = {"schema": 1, "source_sha": source,
                  "findings": [{"finding_id": finding["finding_id"],
                                "status": "disproved", "evidence": evidence}]}
        same_second = "2026-10-20T11:59:00Z"
        old_report = {"id": 40, "created_at": same_second,
                      "updated_at": "2026-10-20T12:00:00Z",
                      "user": {"login": release.BOT, "type": "Bot"},
                      "performed_via_github_app": {"slug": release.APP},
                      "body": "<!-- fork-review-disposition:v1 {malformed old report -->"}
        request = {"id": 41, "created_at": same_second, "updated_at": same_second,
                   "user": {"login": "owner", "type": "User"},
                   "body": f"Repair request.\n<!-- zed-fix:{state['seed']}:2 -->"}
        current_report = {"id": 42, "created_at": same_second, "updated_at": same_second,
                          "user": {"login": release.BOT, "type": "Bot"},
                          "performed_via_github_app": {"slug": release.APP},
                          "body": "Current evidence.\n<!-- fork-review-disposition:v1 "
                                  + json.dumps(record, sort_keys=True, separators=(",", ":")) + " -->"}
        sources = {"scripts/release.py": "def review_dispositions():\n",
                   "scripts/test_maintenance.py": "def test_existing_finding():\n"}
        with (patch.object(release, "native_source_file",
                           side_effect=lambda _repo, _sha, path: sources[path].encode()),
              patch.object(release.subprocess, "check_output",
                           side_effect=lambda *_args, **_kwargs: b"")):
            result = release.review_dispositions(
                [finding], {}, candidate, unresolved=[finding], repair_state=state,
                repair_comments=[old_report, request, current_report], pair=pair)
        self.assertEqual([item["finding_id"] for item in result], [finding["finding_id"]])
        module = Mock()
        module.candidate_identity.return_value = "3" * 64
        with patch.object(release.maintenance, "load", return_value=module):
            # Historical Codex proposals are never owner authority and cannot poison rereads.
            self.assertEqual(release.review_dispositions_from_comments(
                [old_report, request, current_report], candidate), [])

        malformed_current = {**current_report, "id": 43, "body":
                             "<!-- fork-review-disposition:v1 {malformed current report -->"}
        with (patch.object(release, "native_source_file",
                           side_effect=lambda _repo, _sha, path: sources[path].encode()),
              patch.object(release.subprocess, "check_output", return_value=b""),
              self.assertRaisesRegex(ValueError, "malformed")):
            release.review_dispositions(
                [finding], {}, candidate, unresolved=[finding], repair_state=state,
                repair_comments=[old_report, request, malformed_current], pair=pair)

    def test_patch_regression_lookup_uses_only_the_resulting_hunk_postimage(self):
        patch_source = "\n".join((
            "diff --git a/crates/state.rs b/crates/state.rs",
            "--- a/crates/state.rs", "+++ b/crates/state.rs", "@@ -1,7 +1,3 @@",
            "-#[test]", "-fn test_removed_regression() {", "-    assert!(false);", "-}",
            " #[cfg(test)]", "+#[test]", "+fn test_current_regression() {",
            "+    assert!(true);", "+}", "",
        ))
        self.assertFalse(release._source_rust_test(patch_source, "test_removed_regression"))
        self.assertTrue(release._source_rust_test(patch_source, "test_current_regression"))

    def test_patch_regression_attribute_belongs_to_the_named_function(self):
        patch_source = "\n".join((
            "diff --git a/crates/state.rs b/crates/state.rs",
            "--- a/crates/state.rs", "+++ b/crates/state.rs", "@@ -1,3 +1,4 @@",
            " #[test]", " fn test_retained() {}", "+fn test_helper_without_attribute() {}",
            "+#[gpui::test]", "+async fn test_added() {}", "",
        ))
        self.assertTrue(release._source_rust_test(patch_source, "test_retained"))
        self.assertTrue(release._source_rust_test(patch_source, "test_added"))
        self.assertFalse(release._source_rust_test(patch_source, "test_helper_without_attribute"))
        separated = "\n".join((
            "diff --git a/crates/state.rs b/crates/state.rs", "@@ -1 +1 @@",
            "+#[test]", "@@ -20 +20 @@", "+fn test_not_in_the_attribute_hunk() {}", "",
        ))
        self.assertFalse(release._source_rust_test(separated, "test_not_in_the_attribute_hunk"))

    def test_reservation_reader_ignores_foreign_markers_and_rejects_edited_native_records(self):
        stamp = "2026-10-19T11:00:00Z"
        owner = {"id": 7, "user": {"login": "owner", "type": "User"},
                 "created_at": stamp, "updated_at": stamp,
                 "body": '<!-- fork-final:v1 {"run_id":12,"run_attempt":1} -->'}
        foreign = {"id": 8, "user": {"login": "other", "type": "User"},
                   "body": '<!-- fork-final:v1 {malformed -->'}
        self.assertEqual(release.reservations_from_history_comments([foreign, owner], owner="owner"),
                         [{"run_id": 12, "run_attempt": 1}])
        for invalid in ({**owner, "id": True},
                        {**owner, "updated_at": "2026-10-19T11:00:01Z"}):
            with self.subTest(comment=invalid), self.assertRaises(ValueError):
                release.reservations_from_history_comments([invalid], owner="owner")


class UnfinishedSourceSnapshotTests(unittest.TestCase):
    def fixture(self):
        repo, config, application, upstream, context = (
            "owner/zed", "a" * 40, "b" * 40, "c" * 40, "d" * 40)
        candidate = {"schema": 1, "repository": repo, "configuration_sha": config,
                     "application_sha": application, "upstream_sha": upstream,
                     "patchset_sha256": "e" * 64, "recipe_sha256": "9" * 64,
                     "platform": "macos/aarch64"}
        refs = [{"ref": f"refs/heads/review/{context}",
                 "object": {"type": "commit", "sha": context}}]
        commits = {
            f"repos/{repo}/git/commits/{context}": {
                "sha": context, "message": "fork-review-context:v1 " + "f" * 64,
                "parents": [{"sha": application}], "tree": {"sha": "8" * 40}},
            f"repos/{repo}/git/commits/{application}": {
                "sha": application, "tree": {"sha": "8" * 40}},
            f"repos/{release.UPSTREAM}/commits/v1.2.3": {"sha": upstream},
            f"repos/{repo}/git/matching-refs/heads/review/": refs,
        }
        return repo, candidate, context, refs, commits

    def selector(self, *, claimed=(), mutate=None, identity=None):
        repo, candidate, context, refs, commits = self.fixture()
        if mutate is not None:
            mutate(refs, commits)
        module = Mock()
        module.candidate_identity.return_value = identity or "f" * 64

        def native_pages(route):
            if route == f"repos/{repo}/pulls?state=all":
                return list(claimed)
            self.fail(f"Unexpected native inventory: {route}")

        with (patch.object(release, "pages", side_effect=native_pages),
              patch.object(release, "api", side_effect=lambda route: commits[route]) as native,
              patch.object(release.maintenance, "load", return_value=module),
              patch.object(release, "maintenance_candidate", return_value=candidate) as build,
              patch.object(release, "native_source_file",
                           return_value=b'[package]\nversion = "1.2.3"\n') as cargo,
              patch.object(release, "run") as command):
            result = release.pending_source_snapshot(
                repo, candidate["configuration_sha"], candidate["patchset_sha256"])
            command.assert_not_called()
        return result, candidate, context, native, build, cargo

    def test_unposted_context_pins_exact_official_source_without_writing_refs(self):
        result, candidate, context, _, build, cargo = self.selector()
        self.assertEqual(result, {"candidate": candidate, "review_sha": context})
        build.assert_called_once_with(
            candidate["repository"], candidate["configuration_sha"],
            candidate["application_sha"], candidate["upstream_sha"],
            candidate["patchset_sha256"])
        cargo.assert_called_once_with(candidate["repository"], candidate["application_sha"],
                                      "crates/zed/Cargo.toml")

    def test_archived_pr_and_different_configuration_do_not_become_pending_work(self):
        repo, _, context, _, _ = self.fixture()
        archived = {"state": "closed", "head": {
            "ref": f"review/{context}", "repo": {"full_name": repo}}}
        result, _, _, native, _, _ = self.selector(claimed=[archived])
        self.assertIsNone(result)
        native.assert_called_once_with(f"repos/{repo}/git/matching-refs/heads/review/")
        self.assertIsNone(self.selector(identity="0" * 64)[0])

    def test_inconsistent_and_ambiguous_contexts_fail_before_any_mutation(self):
        repo, _, context, _, _ = self.fixture()
        route = f"repos/{repo}/git/commits/{context}"
        for change in ({"parents": []}, {"tree": {"sha": "0" * 40}}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "context"):
                self.selector(mutate=lambda refs, commits: commits[route].update(change))

        def duplicate(refs, commits):
            other = "7" * 40
            refs.append({"ref": f"refs/heads/review/{other}",
                         "object": {"type": "commit", "sha": other}})
            commits[f"repos/{repo}/git/commits/{other}"] = {
                **commits[route], "sha": other}

        with self.assertRaisesRegex(ValueError, "Multiple unfinished"):
            self.selector(mutate=duplicate)

    def test_prepare_requires_actual_patch_reproduction_before_requesting_a_pr(self):
        repo, candidate, context, _, _ = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            with (patch.dict(os.environ, {"GITHUB_REPOSITORY": repo,
                                         "GITHUB_SHA": candidate["configuration_sha"]}),
                  patch.object(release, "output"),
                  patch.object(release, "mirror_main"),
                  patch.object(release, "api", return_value={"sha": candidate["configuration_sha"]}) as native,
                  patch.object(release, "proposal", return_value=None),
                  patch.object(release, "patch_identity",
                               return_value=([Path(temporary) / "patch"], candidate["patchset_sha256"])),
                  patch.object(release, "active_snapshot", return_value=None),
                  patch.object(release, "pending_source_snapshot",
                               return_value={"candidate": candidate, "review_sha": context}),
                  patch.object(release, "select_upstream") as latest,
                  patch.object(release, "policy_snapshot", return_value={}),
                  patch.object(release, "run", side_effect=lambda *args, **kwargs:
                               "0" * 40 if args == ("git", "rev-parse", "HEAD") else ""),
                  self.assertRaisesRegex(ValueError, "did not reproduce")):
                release.prepare(Path(temporary) / "application", shared=True)
            latest.assert_not_called()
            native.assert_called_once_with(f"repos/{repo}/commits/downstream")

    def test_recovery_reprepares_the_exact_original_candidate_and_run(self):
        repo, old, new = "owner/zed", "c" * 40, "0" * 40
        application, upstream, patchset = "a" * 40, "b" * 40, "d" * 64
        candidate = {"schema": 1, "repository": repo, "upstream_sha": upstream,
                     "application_sha": application, "configuration_sha": old,
                     "patchset_sha256": patchset, "recipe_sha256": "e" * 64,
                     "platform": "macos/aarch64"}
        history = {"complete": True, "attempts": [
            {"candidate": candidate, "run": {"id": 41, "run_attempt": 2}}]}
        baseline = {"endpoint": "e", "variable": {"value": json.dumps(
            {"publication": {"published_at": "2026-10-19T10:00:00Z"}})}}
        context, review_pr, delta_pr = "f" * 40, 11, 12
        tag = release.release_tag("1.24.2", upstream, patchset, application)
        draft = {"id": 9, "tag_name": tag, "draft": True}
        pull = {"number": review_pr, "state": "open",
                "head": {"sha": context}, "base": {"ref": f"review-base/{upstream}"}}

        def run(*args, **kwargs):
            if args == ("git", "init", str(app)):
                (app / "crates" / "zed").mkdir(parents=True)
                (app / "crates" / "zed" / "Cargo.toml").write_text(
                    '[package]\nversion = "1.24.2"\n')
                return ""
            if args == ("git", "rev-parse", "HEAD"):
                return application
            if args[:2] == ("git", "show") and args[2].endswith("Cargo.toml"):
                return '[package]\nversion = "1.24.2"\n'
            if args[:2] == ("git", "diff"):
                return ""
            return ""

        def native(path, **kwargs):
            if path == f"repos/{repo}/commits/downstream":
                return {"sha": new}
            if path == f"repos/{release.UPSTREAM}/commits/v1.24.2":
                return {"sha": upstream}
            return {}

        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "application"
            module = Mock()
            module.candidate_identity.return_value = "1" * 64
            outputs = {}
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {
                      "GITHUB_REPOSITORY": repo, "GITHUB_SHA": new,
                      "GITHUB_RUN_ID": "90", "GITHUB_RUN_ATTEMPT": "1",
                      "ZED_TRUSTED_POLICY_SHA": new,
                      "ARTIFACT_RECOVERY": "41/2"}, clear=True))
                stack.enter_context(patch.object(release, "output", side_effect=lambda **kw: outputs.update(kw)))
                native_api = stack.enter_context(patch.object(release, "api", side_effect=native))
                stack.enter_context(patch.object(release, "pages", side_effect=lambda path, **kw: (
                      [draft] if path == f"repos/{repo}/releases"
                      else [pull] if "pulls?" in path else [])))
                proposal = stack.enter_context(patch.object(release, "proposal",
                               side_effect=AssertionError("no proposal in recovery")))
                stack.enter_context(patch.object(release, "patch_identity",
                               return_value=([Path(temporary)], patchset)))
                stack.enter_context(patch.object(release, "active_snapshot", return_value=None))
                stack.enter_context(patch.object(release, "select_upstream",
                               return_value=("v1.24.2", upstream)))
                stack.enter_context(patch.object(release, "minor_findings", return_value=[]))
                pending = stack.enter_context(patch.object(release, "pending_source_snapshot",
                               side_effect=AssertionError("no pending lookup")))
                adopted = stack.enter_context(patch.object(release, "adopted_review_configuration",
                               side_effect=AssertionError("no adoption")))
                stack.enter_context(patch.object(release, "missing_patch_regressions", return_value=set()))
                stack.enter_context(patch.object(release, "validate_paths"))
                stack.enter_context(patch.object(release, "policy_snapshot", return_value={}))
                stack.enter_context(patch.object(release, "verify_upstream_recipes"))
                stack.enter_context(patch.object(release, "maintenance_candidate", return_value={
                      **candidate, "configuration_sha": new}))
                stack.enter_context(patch.object(release, "accepted_baseline",
                               return_value=(baseline, candidate, {})))
                collect = stack.enter_context(patch.object(release, "final_attempt_history",
                               return_value=(history, [])))
                stack.enter_context(patch.object(release, "review_context_commit", return_value=context))
                stack.enter_context(patch.object(release, "push_review_refs"))
                stack.enter_context(patch.object(release, "check_review", return_value=(True, "ready")))
                stack.enter_context(patch.object(release, "verified_policy_bundle", return_value=False))
                stack.enter_context(patch.object(release.maintenance, "load", return_value=module))
                delta = stack.enter_context(patch.object(release, "delta_review",
                               return_value=(delta_pr, "1" * 40, old, True, "ready")))
                stack.enter_context(patch.object(release, "current_review_comments", return_value=[]))
                stack.enter_context(patch.object(release, "review_history_from_comments", return_value=({}, {})))
                stack.enter_context(patch.object(release, "review_dispositions_from_comments", return_value=[]))
                stack.enter_context(patch.object(release, "maintenance_pair", return_value={
                      "state": "ready", "complete": False, "findings": [],
                      "known_findings": [], "review_dispositions": []}))
                recipe = stack.enter_context(patch.object(release, "verify_executing_product_recipe"))
                authorize = stack.enter_context(patch.object(release, "authorize_shared_final", return_value={
                      "decision": "already-validated",
                      "validated_run": {"id": 41, "run_attempt": 2},
                      "artifact_workflow_sha": old}))
                stack.enter_context(patch.object(release, "ensure_forward_release"))
                reserve = stack.enter_context(patch.object(release, "reserve_release", return_value=9))
                stack.enter_context(patch.object(release, "owner_token", return_value="owner"))
                stack.enter_context(patch.object(release, "run", side_effect=run))
                release.prepare(app, shared=True)
        proposal.assert_not_called()
        pending.assert_not_called()
        adopted.assert_not_called()
        collect.assert_called_once()
        recipe.assert_called_once_with(candidate, new)
        self.assertEqual(delta.call_args.args[:3], (repo, old, application))
        self.assertEqual(delta.call_args.kwargs["candidate"], candidate)
        self.assertEqual(delta.call_args.kwargs["accepted"], baseline)
        self.assertEqual(authorize.call_args.kwargs["history_snapshot"], (history, []))
        self.assertEqual(authorize.call_args.kwargs["required_validated_run"], (41, 2))
        self.assertEqual(authorize.call_args.args[0], candidate)
        request_bodies = [
            call.kwargs["data"]["body"] for call in native_api.call_args_list
            if call.args[0] == f"repos/{repo}/issues/{review_pr}/comments"
            and call.kwargs.get("method") == "POST"
        ]
        self.assertEqual(len(request_bodies), 1)
        self.assertIn(f"<!-- zed-full:{context}:{old} -->", request_bodies[0])
        self.assertEqual(reserve.call_args.args[6], old)
        self.assertTrue(outputs["ready"])
        self.assertEqual(outputs["candidate_config"], old)
        self.assertEqual(outputs["artifact_config"], old)
        self.assertEqual(outputs["checked_policy"], new)
        self.assertEqual(outputs["publication_config"], new)
        self.assertEqual(json.loads(outputs["maintenance_candidate"]), candidate)
        self.assertEqual((outputs["artifact_run_id"], outputs["artifact_run_attempt"]), (41, 2))
        self.assertEqual(outputs["artifact_workflow_sha"], old)
        self.assertTrue(outputs["artifact_reuse"])

    def test_recovery_input_requires_checked_shared_trusted_controller(self):
        repo, candidate = "owner/zed", {"repository": "owner/zed"}
        for changes in (
            {"ARTIFACT_RECOVERY": "41/x"},
            {"ARTIFACT_RECOVERY": "0/1"},
            {"ARTIFACT_RECOVERY": "1 2/3"},
            {"ARTIFACT_RECOVERY": "41/0"},
            {"ARTIFACT_RECOVERY": "41/2", "ZED_TRUSTED_POLICY_SHA": "f" * 40},
            {"ARTIFACT_RECOVERY": "41/2", "BOOTSTRAP_MAINTENANCE_CANDIDATE": "{}"},
        ):
            with (self.subTest(changes=changes), tempfile.TemporaryDirectory() as temporary,
                  patch.dict(os.environ, {
                      "GITHUB_REPOSITORY": repo, "GITHUB_SHA": "0" * 40,
                      "ZED_TRUSTED_POLICY_SHA": "0" * 40, **changes}, clear=True),
                  patch.object(release, "output"),
                  patch.object(release, "api", return_value={"sha": "0" * 40}),
                  patch.object(release, "proposal") as proposal,
                  self.assertRaisesRegex(ValueError, "checked trusted controller")):
                release.prepare(Path(temporary) / "application", shared=True)
            proposal.assert_not_called()
        with (tempfile.TemporaryDirectory() as temporary,
              patch.dict(os.environ, {
                  "GITHUB_REPOSITORY": repo, "GITHUB_SHA": "0" * 40,
                  "ZED_TRUSTED_POLICY_SHA": "0" * 40, "ARTIFACT_RECOVERY": "41/2"},
                  clear=True),
              patch.object(release, "output"),
              patch.object(release, "api", return_value={"sha": "0" * 40}),
              patch.object(release, "proposal") as proposal,
              self.assertRaisesRegex(ValueError, "checked trusted controller")):
            release.prepare(Path(temporary) / "application")
        proposal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
class NativePagingSnapshotTests(unittest.TestCase):
    def test_new_callback_restarts_reads_without_returning_partial_history(self):
        first = [{"id": i} for i in range(100)]
        with patch.object(release, "api", side_effect=[
            {"total_count": 101, "workflow_runs": first},
            {"total_count": 102, "workflow_runs": [{"id": 100}, {"id": 101}]},
            {"total_count": 102, "workflow_runs": first},
            {"total_count": 102, "workflow_runs": [{"id": 100}, {"id": 101}]},
        ]) as api, patch.object(release.time, "sleep") as sleep:
            result = release.maintenance_pages("repos/fixture/actions/runs", "workflow_runs")
        self.assertEqual([v["id"] for v in result], list(range(102)))
        self.assertEqual([c.args[0].rsplit("=", 1)[1] for c in api.call_args_list], ["1", "2", "1", "2"])
        sleep.assert_called_once_with(1)

    def test_persistent_drift_and_malformed_records_still_block(self):
        with patch.object(release, "api", side_effect=[
            {"total_count": 101, "workflow_runs": [{}] * 100},
            {"total_count": 102, "workflow_runs": [{}] * 2},
        ] * 3) as api, patch.object(release.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "history changed while paging"):
                release.maintenance_pages("repos/fixture/actions/runs", "workflow_runs")
        self.assertEqual(api.call_count, 6)
        self.assertEqual(sleep.call_count, 2)
        with patch.object(release, "api", return_value={"total_count": "bad", "workflow_runs": []}) as api:
            with self.assertRaisesRegex(ValueError, "Incomplete native lifecycle history"):
                release.maintenance_pages("repos/fixture/actions/runs", "workflow_runs")
        self.assertEqual(api.call_count, 1)
class SharedPublicationBoundaryTests(unittest.TestCase):
    def test_shared_publication_uses_disposition_aware_pair_and_exact_scope(self):
        candidate = {"repository": "owner/zed", "upstream_sha": "a" * 40}
        env = {"MAINTENANCE_CANDIDATE": json.dumps(candidate), "MAINTENANCE_BASELINE": "{}",
               "CANDIDATE_PR": "1", "FULL_REVIEW_SHA": "b" * 40,
               "DELTA_PR": "2", "DELTA_SHA": "c" * 40, "DELTA_BASE": "d" * 40}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(release, "_shared_pair_from_environment", return_value={"state": "ready"}), \
                patch.object(release, "check_review", side_effect=AssertionError("legacy gate must not re-reject a shared disposition")), \
                patch.object(release, "minor_findings", side_effect=AssertionError("legacy minor gate must not run")):
            self.assertTrue(release.publication_review_status("owner/zed", 1, "b" * 40, "a" * 40)[0])
            self.assertEqual(release.publication_minor_findings("owner/zed", 1, "b" * 40), [])
            self.assertFalse(release.publication_review_status("owner/zed", 1, "e" * 40, "a" * 40)[0])
            with patch.object(release, "_shared_pair_from_environment", side_effect=ValueError("unresolved")):
                with self.assertRaisesRegex(ValueError, "unresolved"):
                    release.publication_minor_findings("owner/zed", 1, "b" * 40)

    def test_executing_recipe_is_checked_instead_of_the_proposed_recipe(self):
        candidate = {"repository": "owner/zed", "upstream_sha": "a" * 40, "recipe_sha256": "b" * 64}
        with patch.object(release, "product_recipe_sha256", return_value="b" * 64) as recipe:
            release.verify_executing_product_recipe(candidate, "c" * 40)
            recipe.assert_called_once_with("owner/zed", "c" * 40, "a" * 40)
        with patch.object(release, "product_recipe_sha256", return_value="d" * 64):
            with self.assertRaisesRegex(ValueError, "Executing workflow recipe differs"):
                release.verify_executing_product_recipe(candidate, "c" * 40)

    def test_bootstrap_loader_and_lock_are_from_the_independently_pinned_gate(self):
        workflow = (release.ROOT / ".github/workflows/release.yml").read_text()
        verifier = workflow.split("\n  verify-bootstrap-bundle:\n", 1)[1]
        self.assertIn("ref: c621547cc93d751d7110791049ee748befdd8831", verifier)
        self.assertIn('"gate/scripts/maintenance.lock.json"', verifier)
        self.assertIn('"gate/scripts/maintenance.py"', verifier)
        self.assertIn("workflow_scripts = pathlib.Path('gate/scripts').resolve()", verifier)
        self.assertNotIn("workflow_scripts = workflow_source / 'scripts'", verifier)
