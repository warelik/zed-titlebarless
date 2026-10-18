import hashlib
import io
import json
import os
import plistlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import release


def metadata_fixture(directory):
    repository = "maintainer/zed-fork"
    version = "1.24.2"
    candidate_sha = "a" * 40
    upstream_sha = "b" * 40
    config_sha = "c" * 40
    patchset_sha256 = "d" * 64
    release_id = 1234
    tag = f"build-{version}-{upstream_sha[:12]}-{patchset_sha256[:12]}-{candidate_sha[:12]}"
    symbols_name = f"zed-{version}-titlebarless-debug-symbols-macos-aarch64.tar.gz"
    payloads = {
        "Zed.dmg": b"tiny fake DMG metadata fixture",
        symbols_name: b"tiny fake debug-symbol metadata fixture",
    }
    for name, contents in payloads.items():
        (directory / name).write_bytes(contents)
    archive_sha256 = hashlib.sha256(payloads["Zed.dmg"]).hexdigest()
    symbols_sha256 = hashlib.sha256(payloads[symbols_name]).hexdigest()
    identity = {
        "schema": 1,
        "repository": repository,
        "upstream_repository": "zed-industries/zed",
        "platform": "macos/aarch64",
        "candidate_sha": candidate_sha,
        "upstream_sha": upstream_sha,
        "patchset_sha256": patchset_sha256,
        "config_sha": config_sha,
        "version": version,
        "release_id": release_id,
    }
    provenance = {
        **identity,
        "archive": "Zed.dmg",
        "archive_sha256": archive_sha256,
        "archive_size": len(payloads["Zed.dmg"]),
        "symbols_archive": symbols_name,
        "symbols_sha256": symbols_sha256,
        "symbols_size": len(payloads[symbols_name]),
        "binary_uuid": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        "remote_server_uuid": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB",
    }
    provenance_bytes = (json.dumps(provenance, indent=2) + "\n").encode()
    (directory / "provenance.json").write_bytes(provenance_bytes)
    manifest = {
        key: identity[key]
        for key in (
            "schema",
            "version",
            "release_id",
            "platform",
            "candidate_sha",
            "upstream_sha",
            "patchset_sha256",
        )
    }
    manifest.update(
        url=f"https://github.com/{repository}/releases/download/{tag}/Zed.dmg",
        sha256=archive_sha256,
        size=len(payloads["Zed.dmg"]),
    )
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
    (directory / "latest.json").write_bytes(manifest_bytes)
    checksums = {
        "Zed.dmg": archive_sha256,
        symbols_name: symbols_sha256,
        "provenance.json": hashlib.sha256(provenance_bytes).hexdigest(),
        "latest.json": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    sums = "".join(f"{checksums[name]}  {name}\n" for name in sorted(checksums))
    (directory / "SHA256SUMS").write_text(sums)
    assets = {
        path.name: {
            "name": path.name,
            "size": path.stat().st_size,
            "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in directory.iterdir()
    }
    for index, asset in enumerate(assets.values(), 1):
        asset["id"] = index
    return identity, provenance, manifest, sums, assets, tag


class UpstreamSelectionTests(unittest.TestCase):
    def test_selects_canonical_stable_release_and_full_commit(self):
        stable = {"tag_name": "v1.24.2", "draft": False, "prerelease": False}
        with patch.object(release, "api", side_effect=[stable, {"sha": "a" * 40}]) as api:
            self.assertEqual(release.select_upstream(), ("v1.24.2", "a" * 40))
        self.assertEqual(api.call_count, 2)

    def test_rejects_draft_prerelease_noncanonical_and_unsafe_tags(self):
        unsupported = [
            {"tag_name": "v1.24.2", "draft": True, "prerelease": False},
            {"tag_name": "v1.24.2", "draft": False, "prerelease": True},
            {"tag_name": "v01.24.2", "draft": False, "prerelease": False},
            {"tag_name": "v1.24.2-rc.1", "draft": False, "prerelease": False},
            {"tag_name": "v1.24.2/../../main", "draft": False, "prerelease": False},
        ]
        for record in unsupported:
            with self.subTest(record=record), patch.object(release, "api", return_value=record):
                with self.assertRaises(ValueError):
                    release.select_upstream()

    def test_rejects_a_nonfull_release_commit(self):
        stable = {"tag_name": "v1.24.2", "draft": False, "prerelease": False}
        with patch.object(release, "api", side_effect=[stable, {"sha": "a" * 12}]):
            with self.assertRaises(ValueError):
                release.select_upstream()

    def test_main_mirror_only_advances_without_force(self):
        upstream = "b" * 40
        with patch.object(
            release,
            "api",
            side_effect=[
                {"object": {"sha": "a" * 40}},
                {"object": {"sha": upstream}},
                {"status": "ahead", "merge_base_commit": {"sha": "a" * 40}},
                {"object": {"sha": upstream}},
            ],
        ) as api:
            release.mirror_main("maintainer/zed-fork")
        self.assertEqual(api.call_args.kwargs["method"], "PATCH")
        self.assertEqual(api.call_args.kwargs["data"], {"sha": upstream, "force": False})

    def test_main_mirror_rejects_divergence_without_writing(self):
        with patch.object(
            release,
            "api",
            side_effect=[
                {"object": {"sha": "a" * 40}},
                {"object": {"sha": "b" * 40}},
                {"status": "diverged", "merge_base_commit": {"sha": "c" * 40}},
            ],
        ) as api:
            with self.assertRaises(ValueError):
                release.mirror_main("maintainer/zed-fork")
        self.assertEqual(api.call_count, 3)


class StableReleaseProgressionTests(unittest.TestCase):
    def check_previous(self, candidate_version, candidate_upstream, *, release_id=None):
        previous = {
            "id": 1234,
            "tag_name": "v1.24.2-titlebarless",
            "draft": False,
            "prerelease": False,
        }
        with (
            patch.object(release, "pages", return_value=[previous]),
            patch.object(release, "api", return_value={"sha": "b" * 40}),
        ):
            return release.ensure_forward_release(
                "maintainer/zed-fork",
                candidate_version,
                candidate_upstream,
                release_id=release_id,
            )

    def test_stable_version_cannot_downgrade(self):
        with self.assertRaises(ValueError):
            self.check_previous("1.24.1", "c" * 40)

    def test_same_version_source_cannot_be_retagged(self):
        with self.assertRaises(ValueError):
            self.check_previous("1.24.2", "c" * 40)

    def test_higher_stable_release_on_another_upstream_branch_is_allowed(self):
        self.assertIsNone(self.check_previous("1.24.3", "c" * 40, release_id=1235))

    def test_native_release_id_must_be_newer_than_published(self):
        with self.assertRaises(ValueError):
            self.check_previous("1.24.3", "c" * 40, release_id=1234)


class ReleaseReservationTests(unittest.TestCase):
    def setUp(self):
        self.repository = "maintainer/zed-fork"
        self.sha = "a" * 40
        self.upstream = "b" * 40
        self.patchset = "d" * 64
        self.version = "1.24.2"
        self.config = "c" * 40
        self.tag = f"build-{self.version}-{self.upstream[:12]}-{self.patchset[:12]}-{self.sha[:12]}"

    def identity(self, config=None):
        return {
            "schema": 1,
            "candidate_sha": self.sha,
            "upstream_sha": self.upstream,
            "patchset_sha256": self.patchset,
            "version": self.version,
            "config_sha": config or self.config,
        }

    def draft(self, identity=None, **changes):
        body = "<!-- zed-candidate:v1 " + json.dumps(
            identity or self.identity(), sort_keys=True
        ) + " -->"
        return {
            "id": 1234,
            "tag_name": self.tag,
            "draft": True,
            "prerelease": False,
            "target_commitish": self.sha,
            "author": {"login": "maintainer"},
            "body": body,
            **changes,
        }

    def reserve(self, existing):
        return release.reserve_release(
            self.repository,
            self.tag,
            self.sha,
            self.upstream,
            self.patchset,
            self.version,
            self.config,
        )

    def test_matching_owned_draft_is_reused_and_config_identity_refreshed(self):
        existing = self.draft(self.identity(config="e" * 40))
        with (
            patch.object(release, "pages", return_value=[existing]),
            patch.object(release, "api", return_value=self.draft()) as api,
            patch.object(release, "ensure_forward_release") as ensure_forward,
        ):
            self.assertEqual(self.reserve(existing), 1234)
        self.assertEqual(api.call_args.kwargs["method"], "PATCH")
        self.assertIn(self.config, api.call_args.kwargs["data"]["body"])
        ensure_forward.assert_called_once_with(
            self.repository, self.version, self.upstream, release_id=1234
        )

    def test_published_or_unrelated_drafts_are_never_edited(self):
        unrelated = self.identity()
        unrelated["candidate_sha"] = "e" * 40
        for record in (
            self.draft(draft=False),
            self.draft(author={"login": "somebody"}),
            self.draft(identity=unrelated),
            self.draft(target_commitish="e" * 40),
        ):
            with (
                self.subTest(record=record),
                patch.object(release, "pages", return_value=[record]),
                patch.object(release, "api") as api,
                self.assertRaises(ValueError),
            ):
                self.reserve(record)
            api.assert_not_called()

    def test_new_reservation_returns_the_native_release_id(self):
        with (
            patch.object(release, "pages", return_value=[]),
            patch.object(release, "api", return_value=self.draft()) as api,
            patch.object(release, "ensure_forward_release") as ensure_forward,
        ):
            self.assertEqual(self.reserve(None), 1234)
        self.assertEqual(api.call_args.kwargs["method"], "POST")
        self.assertTrue(api.call_args.kwargs["data"]["draft"])
        ensure_forward.assert_called_once_with(
            self.repository, self.version, self.upstream, release_id=1234
        )

    def test_zero_native_release_id_is_rejected(self):
        with (
            patch.object(release, "pages", return_value=[]),
            patch.object(release, "api", return_value=self.draft(id=0)),
            self.assertRaises(ValueError),
        ):
            self.reserve(None)


class BuildIdentityTests(unittest.TestCase):
    def environment(self, **changes):
        return {
            "GITHUB_REPOSITORY": "maintainer/zed-fork",
            "GITHUB_SHA": "c" * 40,
            "CANDIDATE_SHA": "a" * 40,
            "UPSTREAM_SHA": "b" * 40,
            "PATCHSET_SHA256": "d" * 64,
            "APP_VERSION": "1.24.2",
            "RELEASE_TAG": f"build-1.24.2-{'b' * 12}-{'d' * 12}-{'a' * 12}",
            "RELEASE_ID": "1234",
            "CONFIG_SHA": "c" * 40,
        } | changes

    def test_native_release_id_is_the_compile_identity_without_version_bumping(self):
        with patch.dict(os.environ, self.environment(), clear=True):
            identity = release.build_identity()
        self.assertEqual(identity["release_id"], 1234)
        self.assertEqual(identity["version"], "1.24.2")
        self.assertEqual(identity["candidate_sha"], "a" * 40)

    def test_zero_overflow_and_wrong_release_tag_are_rejected(self):
        for changes in (
            {"RELEASE_ID": "0"},
            {"RELEASE_ID": str(2**64)},
            {"RELEASE_TAG": "build-1.24.2-wrong"},
        ):
            with (
                self.subTest(changes=changes),
                patch.dict(os.environ, self.environment(**changes), clear=True),
                self.assertRaises(ValueError),
            ):
                release.build_identity()


class ReleaseMetadataTests(unittest.TestCase):
    def fixture(self, directory):
        identity, provenance, manifest, sums, assets, tag = metadata_fixture(directory)
        self.assertEqual(len(assets), 5)
        self.assertTrue(all((directory / name).is_file() for name in assets))
        self.assertTrue(all((directory / name).stat().st_size < 4096 for name in assets))
        return identity, provenance, manifest, sums, assets, tag

    def validate(self, fixture, *, provenance=None, manifest=None, sums=None, expected=None, assets=None, tag=None):
        identity, original_provenance, original_manifest, original_sums, original_assets, original_tag = fixture
        release.validate_metadata(
            provenance or original_provenance,
            manifest or original_manifest,
            original_sums if sums is None else sums,
            expected or identity,
            original_assets if assets is None else assets,
            original_tag if tag is None else tag,
        )

    def test_five_tiny_regular_fake_assets_with_real_sha256_values_validate(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.validate(self.fixture(Path(temporary)))

    def test_source_config_version_platform_release_id_and_tag_are_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(Path(temporary))
            identity, provenance, _, _, _, tag = fixture
            for key, value in (
                ("candidate_sha", "e" * 40),
                ("config_sha", "f" * 40),
                ("version", "1.24.3"),
                ("release_id", 1235),
                ("platform", "macos/x86_64"),
            ):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    self.validate(fixture, expected={**identity, key: value})
            with self.assertRaises(ValueError):
                self.validate(fixture, tag=tag + "-stale")
            with self.assertRaises(ValueError):
                self.validate(fixture, provenance={**provenance, "release_id": 1235})

    def test_sizes_digests_urls_symbols_and_checksum_sets_are_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.fixture(Path(temporary))
            identity, provenance, manifest, sums, assets, _ = fixture
            symbols_name = provenance["symbols_archive"]
            invalid_assets = [
                {**assets, "Zed.dmg": {**assets["Zed.dmg"], "digest": "sha256:" + "0" * 64}},
                {**assets, "Zed.dmg": {**assets["Zed.dmg"], "size": 0}},
                {name: asset for name, asset in assets.items() if name != "latest.json"},
                {**assets, "unexpected.bin": {"name": "unexpected.bin", "size": 1,
                                               "digest": "sha256:" + "0" * 64}},
                {**assets, symbols_name: {**assets[symbols_name], "digest": "sha256:" + "0" * 64}},
            ]
            invalid_sums = [
                sums + sums.splitlines()[0] + "\n",
                sums + "0" * 64 + "  unexpected.bin\n",
                "\n".join(sums.splitlines()[1:]) + "\n",
            ]
            for changed_assets in invalid_assets:
                with self.subTest(assets=sorted(changed_assets)), self.assertRaises(ValueError):
                    self.validate(fixture, assets=changed_assets)
            for changed_sums in invalid_sums:
                with self.subTest(sums=changed_sums), self.assertRaises(ValueError):
                    self.validate(fixture, sums=changed_sums)
            for changed_manifest in (
                {**manifest, "url": "https://example.invalid/Zed.dmg"},
                {**manifest, "url": f"https://github.com/{identity['repository']}/releases/download/../Zed.dmg"},
                {**manifest, "extra": "unexpected"},
            ):
                with self.subTest(manifest=changed_manifest), self.assertRaises(ValueError):
                    self.validate(fixture, manifest=changed_manifest)
            with self.assertRaises(ValueError):
                self.validate(fixture, provenance={**provenance, "symbols_sha256": "0" * 64})


class PublishedReleaseTests(unittest.TestCase):
    def setUp(self):
        # Delta-review rejection has its own independent contract tests. These
        # tests isolate immutable artifact publication from GitHub review I/O.
        delta = patch.object(release, "check_delta_for_publication")
        delta.start()
        self.addCleanup(delta.stop)

    def published_record(self, identity, assets):
        tag = (
            f"build-{identity['version']}-{identity['upstream_sha'][:12]}-"
            f"{identity['patchset_sha256'][:12]}-{identity['candidate_sha'][:12]}"
        )
        return {
            "id": identity["release_id"],
            "draft": False,
            "prerelease": False,
            "tag_name": tag,
            "target_commitish": identity["candidate_sha"],
            "assets": list(assets.values()),
        }

    def test_existing_published_release_is_fully_rechecked_without_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            identity, _, _, _, assets, _ = metadata_fixture(Path(temporary))
            record = self.published_record(identity, assets)
            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_REPOSITORY": identity["repository"],
                        "GITHUB_SHA": identity["config_sha"],
                        "CANDIDATE_PR": "7",
                        "DELTA_PR": "8",
                        "RELEASE_TAG": record["tag_name"],
                        "REPAIR_HEAD": "e" * 40,
                    },
                    clear=True,
                ),
                patch.object(release, "build_identity", return_value=identity),
                patch.object(release, "api", return_value=record),
                patch.object(release, "verify_published_release") as verify,
                patch.object(release, "check_review", return_value=(True, "reviewed")) as review,
                patch.object(release, "minor_findings", return_value=[]),
                patch.object(release, "close_published_reviews") as close_reviews,
                patch.object(release, "close_recovered_repair") as close_repair,
                patch.object(release, "run") as run,
            ):
                release.publish(Path("/unused"))
            verify.assert_called_once_with(
                identity["repository"],
                record,
                identity["candidate_sha"],
                identity["upstream_sha"],
                identity["patchset_sha256"],
                identity["version"],
            )
            self.assertTrue(review.call_args.kwargs["allow_closed"])
            close_reviews.assert_called_once()
            self.assertEqual(close_repair.call_args.kwargs["expected_head"], "e" * 40)
            run.assert_not_called()

    def test_duplicate_asset_names_in_a_published_release_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            identity, _, _, _, assets, tag = metadata_fixture(Path(temporary))
            record = self.published_record(identity, assets)
            record["assets"].append(record["assets"][0])
            with patch.object(
                release, "api", return_value={"object": {"type": "commit", "sha": identity["candidate_sha"]}}
            ):
                with self.assertRaisesRegex(ValueError, "duplicated"):
                    release.verify_published_release(
                        identity["repository"],
                        record,
                        identity["candidate_sha"],
                        identity["upstream_sha"],
                        identity["patchset_sha256"],
                        identity["version"],
                    )

    def test_draft_publication_uploads_only_verified_assets_and_captures_repair_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            identity, _, _, _, assets, tag = metadata_fixture(directory)
            marker_identity = {
                key: identity[key]
                for key in (
                    "schema",
                    "candidate_sha",
                    "upstream_sha",
                    "patchset_sha256",
                    "version",
                    "config_sha",
                )
            }
            draft = {
                "id": identity["release_id"],
                "draft": True,
                "prerelease": False,
                "tag_name": tag,
                "target_commitish": identity["candidate_sha"],
                "author": {"login": "maintainer"},
                "body": "<!-- zed-candidate:v1 "
                + json.dumps(marker_identity, sort_keys=True)
                + " -->",
                "assets": [assets["Zed.dmg"]],
            }
            uploaded = {**draft, "assets": list(assets.values())}
            published = {**uploaded, "draft": False}
            release_reads = 0
            api_calls = []

            def api(path, *, method="GET", data=None, token=None):
                nonlocal release_reads
                api_calls.append((path, method, data, token))
                release_path = f"repos/{identity['repository']}/releases/{identity['release_id']}"
                if path == release_path and method == "GET":
                    release_reads += 1
                    return (draft, uploaded, published)[release_reads - 1]
                if path == f"repos/{identity['repository']}/commits/downstream":
                    return {"sha": identity["config_sha"]}
                if path == f"repos/{identity['repository']}/git/refs" and method == "POST":
                    return {"ref": f"refs/tags/{tag}"}
                if path == f"repos/{identity['repository']}/git/ref/tags/{tag}":
                    return {"object": {"type": "commit", "sha": identity["candidate_sha"]}}
                return {}

            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_REPOSITORY": identity["repository"],
                        "GITHUB_SHA": identity["config_sha"],
                        "RELEASE_TAG": tag,
                        "CANDIDATE_PR": "7",
                        "DELTA_PR": "8",
                        "REPAIR_HEAD": "f" * 40,
                    },
                    clear=True,
                ),
                patch.object(release, "build_identity", return_value=identity),
                patch.object(release, "api", side_effect=api),
                patch.object(release, "pages", return_value=[]),
                patch.object(release, "check_review", return_value=(True, "reviewed")),
                patch.object(release, "ensure_forward_release") as forward,
                patch.object(release, "verify_published_release") as verify,
                patch.object(release, "minor_findings", return_value=[]),
                patch.object(release, "close_published_reviews") as close_reviews,
                patch.object(release, "close_recovered_repair") as close_repair,
                patch.object(release, "run") as run,
            ):
                release.publish(directory)

            upload = next(call for call in run.call_args_list if call.args[:3] == (
                "gh", "release", "upload"
            ))
            self.assertIn("--clobber", upload.args)
            self.assertEqual(len(upload.args[4:9]), 5)
            self.assertEqual(
                {Path(path).name for path in upload.args[4:9]},
                release.release_names(identity["version"]),
            )
            forward.assert_called_once_with(
                identity["repository"],
                identity["version"],
                identity["upstream_sha"],
                release_id=identity["release_id"],
            )
            self.assertTrue(
                any(
                    path == f"repos/{identity['repository']}/git/refs"
                    and method == "POST"
                    and data == {"ref": f"refs/tags/{tag}", "sha": identity["candidate_sha"]}
                    for path, method, data, _ in api_calls
                )
            )
            verify.assert_called_once()
            close_reviews.assert_called_once()
            self.assertEqual(close_repair.call_args.kwargs["expected_head"], "f" * 40)

    def test_corrupt_local_asset_cannot_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            identity, _, _, _, _, tag = metadata_fixture(directory)
            (directory / "Zed.dmg").write_bytes(b"corrupt")
            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_REPOSITORY": identity["repository"],
                        "GITHUB_SHA": identity["config_sha"],
                        "RELEASE_TAG": tag,
                        "CANDIDATE_PR": "7",
                        "DELTA_PR": "8",
                    },
                    clear=True,
                ),
                patch.object(release, "build_identity", return_value=identity),
                patch.object(
                    release,
                    "api",
                    side_effect=[
                        {
                            "id": identity["release_id"],
                            "draft": True,
                            "prerelease": False,
                            "tag_name": tag,
                            "target_commitish": identity["candidate_sha"],
                            "author": {"login": "maintainer"},
                            "body": "<!-- zed-candidate:v1 "
                            + json.dumps(
                                {key: identity[key] for key in (
                                    "schema", "candidate_sha", "upstream_sha",
                                    "patchset_sha256", "version", "config_sha"
                                )},
                                sort_keys=True,
                            )
                            + " -->",
                        },
                        {"sha": identity["config_sha"]},
                    ],
                ),
                patch.object(release, "check_review", return_value=(True, "reviewed")),
                patch.object(release, "run") as run,
                self.assertRaises(ValueError),
            ):
                release.publish(directory)
            run.assert_not_called()


class WorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (release.ROOT / ".github/workflows/release.yml").read_text()
        self.readme = (release.ROOT / "README.md").read_text()

    def test_exact_candidate_and_trusted_default_branch_are_required(self):
        self.assertIn("test \"$GITHUB_REF\" = \"refs/heads/$DEFAULT_BRANCH\"", self.workflow)
        self.assertIn('test \"$DEFAULT_BRANCH\" = downstream', self.workflow)
        self.assertIn("ref: ${{ needs.prepare.outputs.sha }}", self.workflow)
        self.assertIn("fetch-depth: ${{ needs.prepare.outputs.source_depth }}", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
    def test_macos_job_is_read_only_and_has_no_write_or_codex_credentials(self):
        application_job = self.workflow.split("\n  test:\n", 1)[1].split("\n  publish:\n", 1)[0]
        self.assertIn("timeout-minutes: 355", application_job)
        self.assertNotIn("GH_TOKEN:", application_job)
        self.assertNotIn("CODEX_REQUEST_TOKEN:", application_job)
        self.assertIn("CANDIDATE_SHA:", application_job)
        self.assertIn("PATCHSET_SHA256:", application_job)
        self.assertIn("RELEASE_ID:", application_job)
        self.assertIn("python3 trusted/scripts/release.py test", application_job)

    def test_policy_is_checked_without_write_credentials_and_repair_is_bounded(self):
        policy_job = self.workflow.split("\n  policy-check:\n", 1)[1].split("\n  test:\n", 1)[0]
        repair_job = self.workflow.split("\n  repair:\n", 1)[1]
        self.assertIn("python3 trusted/scripts/release.py check-policy", policy_job)
        self.assertIn("needs.prepare.outputs.ready == 'true'", policy_job)
        land_job = self.workflow.split("\n  land:\n", 1)[1].split("\n  repair:\n", 1)[0]
        self.assertIn("needs: [prepare, activate-policy]", land_job)
        activation = self.workflow.split("\n  activate-policy:\n", 1)[1].split("\n  repair:\n", 1)[0]
        self.assertIn("needs.policy-check.result == 'success'", activation)
        self.assertIn("needs.activate-policy.result == 'success'", land_job)
        self.assertNotIn("GH_TOKEN:", policy_job)
        self.assertIn("contains(needs.*.result, 'failure')", repair_job)
        self.assertIn("REPAIR_HEAD: ${{ needs.prepare.outputs.repair_head }}", self.workflow)
        self.assertNotIn("--force", self.workflow)
        self.assertNotIn("gh release delete", self.workflow)
        self.assertNotIn("force: true", self.workflow)

    def test_docs_distinguish_publication_from_delivery_and_state_updater_limit(self):
        self.assertIn("two permanent branches", self.readme)
        self.assertIn("Native Codex repair PRs use temporary branches", self.readme)
        self.assertIn("Publication is not live installation", self.readme)
        self.assertIn("one-time install or their next ordinary higher-upstream-version update", self.readme)
        self.assertIn("immutable five-asset release", self.readme)

    def test_review_wait_and_total_repair_budget_are_distinct_and_bounded(self):
        self.assertEqual(release.REVIEW_WAIT_SECONDS, 7200)
        self.assertEqual(release.MACOS_JOB_TIMEOUT_MINUTES, 355)
        self.assertEqual(
            release.MAX_REPAIR_SECONDS,
            23 * 60 * 60 + 45 * 60,
        )


class IsolatedEnvironmentTests(unittest.TestCase):
    def test_both_explicit_toolchain_paths_avoid_home_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sandbox"
            root.mkdir()
            environment = {
                "HOME": "/host/home",
                "CARGO_HOME": "/toolchain/cargo",
                "RUSTUP_HOME": "/toolchain/rustup",
                "GH_TOKEN": "secret",
                "GITHUB_TOKEN": "secret",
                "CODEX_REQUEST_TOKEN": "secret",
                "GITHUB_ENV": "/runner/env",
                "GITHUB_PATH": "/runner/path",
                "ZED_UPDATE_MANIFEST_URL": "secret",
                "ZED_DOWNSTREAM_RELEASE_ID": "secret",
                "ZED_COMMIT_SHA": "secret",
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch.object(Path, "home", side_effect=AssertionError("Path.home must not run")),
            ):
                isolated = release.isolated_environment(root)
            self.assertEqual(isolated["CARGO_HOME"], "/toolchain/cargo")
            self.assertEqual(isolated["RUSTUP_HOME"], "/toolchain/rustup")
            self.assertNotEqual(isolated["HOME"], "/host/home")
            self.assertFalse(set(environment) & isolated.keys() - {
                "HOME", "CARGO_HOME", "RUSTUP_HOME"
            })

    def test_missing_home_keeps_isolated_application_home_without_toolchain_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sandbox"
            root.mkdir()
            with (
                patch.dict(
                    os.environ,
                    {
                        "HOME": "/host/home",
                        "GH_TOKEN": "secret",
                        "CODEX_REQUEST_TOKEN": "secret",
                    },
                    clear=True,
                ),
                patch.object(Path, "home", side_effect=RuntimeError("no home database entry")),
            ):
                isolated = release.isolated_environment(root)
            self.assertEqual(isolated["HOME"], str(root / "home"))
            self.assertNotIn("CARGO_HOME", isolated)
            self.assertNotIn("RUSTUP_HOME", isolated)
            self.assertNotIn("GH_TOKEN", isolated)
            self.assertNotIn("CODEX_REQUEST_TOKEN", isolated)

    def test_one_explicit_toolchain_path_survives_missing_host_home(self):
        for supplied, value, absent in (
            ("CARGO_HOME", "/toolchain/cargo", "RUSTUP_HOME"),
            ("RUSTUP_HOME", "/toolchain/rustup", "CARGO_HOME"),
        ):
            with self.subTest(supplied=supplied), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "sandbox"
                root.mkdir()
                with (
                    patch.dict(
                        os.environ,
                        {"HOME": "/host/home", supplied: value, "CODEX_REQUEST_TOKEN": "secret"},
                        clear=True,
                    ),
                    patch.object(Path, "home", side_effect=RuntimeError("no home database entry")),
                ):
                    isolated = release.isolated_environment(root)
                self.assertEqual(isolated[supplied], value)
                self.assertNotIn(absent, isolated)
                self.assertEqual(isolated["HOME"], str(root / "home"))
                self.assertNotIn("CODEX_REQUEST_TOKEN", isolated)

    def test_host_defaults_are_toolchain_inputs_not_application_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sandbox"
            root.mkdir()
            host_home = Path(temporary) / "host-home"
            with (
                patch.dict(os.environ, {"HOME": str(host_home)}, clear=True),
                patch.object(Path, "home", return_value=host_home),
            ):
                isolated = release.isolated_environment(root)
            self.assertEqual(isolated["CARGO_HOME"], str(host_home / ".cargo"))
            self.assertEqual(isolated["RUSTUP_HOME"], str(host_home / ".rustup"))
            self.assertEqual(isolated["HOME"], str(root / "home"))
            self.assertNotEqual(isolated["HOME"], str(host_home))


class ReviewRefPublicationTests(unittest.TestCase):
    def test_exact_base_and_patched_head_are_published_atomically(self):
        calls = []
        repo, upstream, head = "maintainer/zed-fork", "b" * 40, "a" * 40
        parent_token = "parent-gh-token"
        owner_token = "fixture-owner-token"

        def run(*args, cwd=None, env=None):
            calls.append((args, env))
            if args[:3] == ("git", "ls-remote", "--refs"):
                return ""
            return ""

        with (
            patch.dict(os.environ, {"GH_TOKEN": parent_token}),
            patch.object(release, "owner_token", return_value=owner_token) as get_owner_token,
            patch.object(release, "run", side_effect=run),
        ):
            release.push_review_refs(
                repo, Path("app"), "https://example.invalid/repo.git",
                f"review-base/{upstream}", upstream, "review/candidate", head)
            self.assertEqual(os.environ["GH_TOKEN"], parent_token)

        push_args, push_env = next(call for call in calls if call[0][:2] == ("git", "push"))
        self.assertEqual(push_args, (
            "git", "push", "--atomic", "https://example.invalid/repo.git",
            f"{upstream}:refs/heads/review-base/{upstream}",
            f"{head}:refs/heads/review/candidate",
        ))
        self.assertEqual(push_env["GH_TOKEN"], owner_token)
        self.assertNotIn(owner_token, " ".join(push_args))
        get_owner_token.assert_called_once_with(repo)

    def test_mismatched_existing_ref_fails_before_owner_token_or_push(self):
        calls = []
        repo, upstream, head = "maintainer/zed-fork", "b" * 40, "a" * 40
        base = f"review-base/{upstream}"

        def run(*args, cwd=None, env=None):
            calls.append((args, env))
            if args[:3] == ("git", "ls-remote", "--refs"):
                return f"{'f' * 40}\trefs/heads/{base}\n"
            raise AssertionError(args)

        with (
            patch.object(release, "run", side_effect=run),
            patch.object(release, "owner_token") as get_owner_token,
            self.assertRaisesRegex(ValueError, "Review branch changed"),
        ):
            release.push_review_refs(
                repo, Path("app"), "https://example.invalid/repo.git",
                base, upstream, "review/candidate", head)

        get_owner_token.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][:3], ("git", "ls-remote", "--refs"))


class RepairPromptContractTests(unittest.TestCase):
    def test_repair_prompt_reconstructs_exact_json_context_without_archive_or_new_gate(self):
        state = {
            "config": "a" * 40,
            "upstream": "b" * 40,
            "head": "c" * 40,
        }
        prompt = release.repair_text(state, 1, "fixture failure")
        self.assertIn(".repair/context.json", prompt)
        self.assertIn("https://github.com/zed-industries/zed.git", prompt)
        self.assertIn("filename order", prompt)
        self.assertIn("There is no giant source archive", prompt)
        self.assertIn("<!-- zed-fix:" + state["head"] + ":1 -->", prompt)
        self.assertNotIn("9router", prompt)

    def test_source_review_is_internal_to_the_downstream_and_keeps_all_safety_checks(self):
        sha = "c" * 40
        upstream = "b" * 40
        config = "maintainer/zed-fork@" + "a" * 40
        prompt = release.request_text(sha, upstream, config)

        self.assertIn(f"Apply the existing downstream maintenance contract in `AGENTS.md` at `{config}`", prompt)
        self.assertIn("already requires independent exact-SHA Codex review", prompt)
        self.assertIn("excludes upstream README banners and manual author confirmation", prompt)
        self.assertIn("This internal comparison is not an upstream PR submission.", prompt)
        self.assertIn("Patch configuration: `" + config + "`", prompt)
        self.assertIn("regression coverage", prompt)
        self.assertIn("official SSH remote-server protocol and download source", prompt)
        self.assertIn("downgrade refusal", prompt)
        self.assertIn("DMG integrity before installation", prompt)
        self.assertIn(f"<!-- zed-codex:{sha} -->", prompt)

    def test_repair_prompt_reuses_applicable_unadopted_candidates_without_old_context(self):
        state = {
            "config": "a" * 40,
            "upstream": "b" * 40,
            "head": "c" * 40,
        }
        prepared = {
            "repository": "maintainer/zed-fork",
            "pull_request": 7,
            "head": "d" * 40,
            "seed": "e" * 40,
            "config": "f" * 40,
            "current_config": state["config"],
            "upstream": state["upstream"],
            "baseline_patches_tree": "1" * 40,
            "changed_paths": ["patches/0014-Fix-terminal-cleanup.patch"],
        }
        prompt = release.repair_text(state, 1, "fixture failure", prepared=prepared)
        default_prompt = release.repair_text(state, 1, "fixture failure")

        self.assertNotIn("A prepared application candidate is pinned below", default_prompt)
        self.assertNotIn("inspect earlier repair PRs", default_prompt)
        self.assertIn("A prepared application candidate is pinned below", prompt)
        self.assertIn("same-repository repair identity", prompt)
        self.assertIn("application-only delta", prompt)
        self.assertIn("identical baseline patches tree", prompt)
        self.assertIn("Reuse only this exact candidate's applicable patch changes", prompt)
        self.assertIn("inspect its linked review findings", prompt)
        self.assertIn("Do not replay old policy or repair context", prompt)
        self.assertIn("fresh exact-SHA review and every mandatory gate still apply", prompt)
        self.assertIn(json.dumps(prepared, sort_keys=True), prompt)
        self.assertNotIn("inspect earlier repair PRs", prompt)
        self.assertIn(".repair/context.json", prompt)
        self.assertIn(f"`{release.UPSTREAM}@{state['upstream']}`", prompt)
        self.assertIn("Do not mix policy and application repairs", prompt)
        self.assertRegex(prompt, r"Attempt 1(?:/3)?\.")
        self.assertIn(f"<!-- zed-fix:{state['head']}:1 -->", prompt)


class RepairContextTests(unittest.TestCase):
    def test_native_repair_context_is_exact_json_and_has_no_source_archive(self):
        repository = "maintainer/zed-fork"
        config = "a" * 40
        upstream = "b" * 40
        seed = "c" * 40
        environment = {
            "GITHUB_REPOSITORY": repository,
            "GITHUB_SHA": config,
            "GITHUB_RUN_ID": "42",
            "UPSTREAM_SHA": upstream,
        }

        def run(*args, cwd=None, env=None):
            if args[:3] == ("git", "worktree", "add"):
                Path(args[4]).mkdir(parents=True)
                return ""
            if args[:3] == ("git", "rev-parse", "HEAD"):
                return seed
            return ""

        def api(path, *, method="GET", data=None, token=None):
            if path == f"repos/{repository}/commits/downstream":
                return {"sha": config}
            if path == f"repos/{repository}/pulls":
                return {"number": 2, "created_at": "2026-10-16T10:00:00Z"}
            return {}

        def subprocess_run(args, **kwargs):
            return SimpleNamespace(stdout="", returncode=1)

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, environment, clear=True),
            patch.object(release, "repair_pull", return_value=None),
            patch.object(release, "failure_evidence", return_value="failure fixture"),
            patch.object(release, "owner_token", return_value="fake-token"),
            patch.object(release, "pages", return_value=[]),
            patch.object(release, "patch_identity", return_value=([], "d" * 64)),
            patch.object(release, "repair_requests", return_value=[]),
            patch.object(release, "run", side_effect=run),
            patch.object(release, "api", side_effect=api),
            patch.object(release.subprocess, "run", side_effect=subprocess_run),
        ):
            repair_directory = Path(temporary) / "repair"
            release.request_repair(repair_directory)
            worktree = repair_directory / "configuration"
            context_directory = worktree / ".repair"
            context = json.loads((context_directory / "context.json").read_text())
            self.assertEqual(
                set(context),
                {"schema", "config", "upstream", "cycle", "nonblocking"},
            )
            self.assertEqual(context["schema"], 1)
            self.assertEqual(context["config"], config)
            self.assertEqual(context["upstream"], upstream)
            self.assertRegex(context["cycle"], r"^[0-9a-f]{64}$")
            self.assertFalse((context_directory / "upstream.tar.gz").exists())
            self.assertEqual({path.name for path in context_directory.iterdir()}, {"context.json"})


class DraftPublicationFailureTests(unittest.TestCase):
    def setUp(self):
        delta = patch.object(release, "check_delta_for_publication")
        delta.start()
        self.addCleanup(delta.stop)

    def test_unreviewed_draft_cannot_upload_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            identity, _, _, _, _, tag = metadata_fixture(Path(temporary))
            draft = {
                "id": identity["release_id"],
                "draft": True,
                "prerelease": False,
                "tag_name": tag,
                "target_commitish": identity["candidate_sha"],
                "author": {"login": "maintainer"},
                "body": "candidate",
                "assets": [],
            }
            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_REPOSITORY": identity["repository"],
                        "GITHUB_SHA": identity["config_sha"],
                        "RELEASE_TAG": tag,
                        "CANDIDATE_PR": "7",
                        "DELTA_PR": "8",
                    },
                    clear=True,
                ),
                patch.object(release, "build_identity", return_value=identity),
                patch.object(
                    release,
                    "api",
                    side_effect=[draft, {"sha": identity["config_sha"]}],
                ),
                patch.object(release, "check_review", return_value=(False, "review missing")),
                patch.object(release, "run") as run,
                self.assertRaisesRegex(ValueError, "review missing"),
            ):
                release.publish(Path(temporary))
            run.assert_not_called()

    def test_stale_configuration_prevents_publication_before_review_or_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            identity, _, _, _, _, tag = metadata_fixture(Path(temporary))
            draft = {
                "id": identity["release_id"],
                "draft": True,
                "prerelease": False,
                "tag_name": tag,
                "target_commitish": identity["candidate_sha"],
            }
            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_REPOSITORY": identity["repository"],
                        "GITHUB_SHA": identity["config_sha"],
                        "RELEASE_TAG": tag,
                        "CANDIDATE_PR": "7",
                        "DELTA_PR": "8",
                    },
                    clear=True,
                ),
                patch.object(release, "build_identity", return_value=identity),
                patch.object(release, "api", side_effect=[draft, {"sha": "f" * 40}]),
                patch.object(release, "check_review") as review,
                patch.object(release, "run") as run,
                self.assertRaisesRegex(ValueError, "stale candidate"),
            ):
                release.publish(Path(temporary))
            review.assert_not_called()
            run.assert_not_called()

    def _assert_uploaded_mismatch_keeps_draft_unpublished(self, mutate_asset):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            identity, _, _, _, assets, tag = metadata_fixture(directory)
            marker_identity = {
                key: identity[key]
                for key in (
                    "schema",
                    "candidate_sha",
                    "upstream_sha",
                    "patchset_sha256",
                    "version",
                    "config_sha",
                )
            }
            draft = {
                "id": identity["release_id"],
                "draft": True,
                "prerelease": False,
                "tag_name": tag,
                "target_commitish": identity["candidate_sha"],
                "author": {"login": "maintainer"},
                "body": "<!-- zed-candidate:v1 "
                + json.dumps(marker_identity, sort_keys=True)
                + " -->",
                "assets": [assets["Zed.dmg"]],
            }
            uploaded_assets = [dict(asset) for asset in assets.values()]
            mutate_asset(uploaded_assets[0])
            uploaded = {**draft, "assets": uploaded_assets}
            release_reads = 0
            api_calls = []

            def api(path, *, method="GET", data=None, token=None):
                nonlocal release_reads
                api_calls.append((path, method, data))
                if path == f"repos/{identity['repository']}/releases/{identity['release_id']}":
                    release_reads += 1
                    return (draft, uploaded)[release_reads - 1]
                if path == f"repos/{identity['repository']}/commits/downstream":
                    return {"sha": identity["config_sha"]}
                return {}

            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_REPOSITORY": identity["repository"],
                        "GITHUB_SHA": identity["config_sha"],
                        "RELEASE_TAG": tag,
                        "CANDIDATE_PR": "7",
                        "DELTA_PR": "8",
                        "REPAIR_HEAD": "e" * 40,
                    },
                    clear=True,
                ),
                patch.object(release, "build_identity", return_value=identity),
                patch.object(release, "api", side_effect=api),
                patch.object(release, "check_review", return_value=(True, "reviewed")),
                patch.object(release, "ensure_forward_release"),
                patch.object(release, "run") as run,
                self.assertRaisesRegex(ValueError, "Uploaded assets differ"),
            ):
                release.publish(directory)
            self.assertTrue(any(call.args[:3] == ("gh", "release", "upload") for call in run.call_args_list))
            self.assertFalse(any(method == "PATCH" for _, method, _ in api_calls))
            self.assertFalse(any(path.endswith("/git/refs") for path, _, _ in api_calls))

    def test_uploaded_native_size_mismatch_keeps_draft_unpublished(self):
        self._assert_uploaded_mismatch_keeps_draft_unpublished(
            lambda asset: asset.update(size=asset["size"] + 1)
        )

    def test_uploaded_native_digest_mismatch_keeps_draft_unpublished(self):
        self._assert_uploaded_mismatch_keeps_draft_unpublished(
            lambda asset: asset.update(digest="sha256:" + "0" * 64)
        )


class RustRegressionRunnerTests(unittest.TestCase):
    def assert_isolated(self, env):
        self.assertFalse(
            {
                "GH_TOKEN",
                "GITHUB_TOKEN",
                "CODEX_REQUEST_TOKEN",
                "ACTIONS_RUNTIME_TOKEN",
                "ACTIONS_ID_TOKEN_REQUEST_URL",
                "ZED_UPDATE_MANIFEST_URL",
                "ZED_DOWNSTREAM_RELEASE_ID",
                "ZED_COMMIT_SHA",
            }
            & env.keys()
        )
        self.assertEqual(env["CARGO_HOME"], "/toolchain-cache/cargo")
        self.assertEqual(env["RUSTUP_HOME"], "/toolchain-cache/rustup")
        self.assertNotIn("RUSTUP_TOOLCHAIN", env)
        self.assertNotIn("MACOSX_DEPLOYMENT_TARGET", env)
        for key in (
            "HOME",
            "TMPDIR",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "DATA_DIR",
        ):
            path = Path(env[key])
            self.assertTrue(path.is_dir())
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_debug_assertions_only_enabled_for_selector_regressions(self):
        """Legacy identity retained; owner requires upstream's one default test profile."""
        key = "CARGO_PROFILE_RELEASE_DEBUG_ASSERTIONS"
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {key: "true"}),
        ):
            self.assertNotIn(key, release.isolated_environment(Path(temporary)))
        for selector in ("test_tab_width_bounds",
                         "test_title_bar_to_status_bar_keeps_title_when_status_bar_hidden"):
            commands, test_runs = self.exercise(selector_case=selector)
            cargo = [(command, env) for command, env in commands
                     if command[:2] == ["cargo", "test"]]
            self.assertEqual(len(cargo), 1)
            self.assertNotIn("--release", cargo[0][0])
            self.assertNotIn("--profile", cargo[0][0])
            self.assertNotIn(key, cargo[0][1])
            self.assertTrue(all(key not in env for _, env in test_runs))
            self.assertIn("tests::" + selector, {command[2] for command, _ in test_runs})
            self.assertIn("tests::test_historical", {command[2] for command, _ in test_runs})

    def exercise(
        self,
        *,
        missing_package=False,
        listing=None,
        binary_outside=False,
        selector_case=False,
        instrumentation=True,
        real_shpool_case=False,
        result_output="test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out",
        result_code=0,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app"
            app.mkdir()
            binary = root / "outside-test-binary" if binary_outside else (
                app / "target/test-binary"
            )
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("fake test executable")
            candidate = "c" * 40
            upstream = "b" * 40
            manifest = {
                "test_cases": [["auto_update", "test_historical"]],
                "regression_cases": [["auto_update", "test_preserved"]],
            }
            if real_shpool_case:
                manifest["regression_cases"].append(["project", "test_isolated_shpool_daemon_probe_and_kill"])
            if selector_case:
                selector = "test_tab_width_bounds" if selector_case is True else selector_case
                manifest["test_cases"].append(["workspace", selector])
            diff = """diff --git a/crates/auto_update/src/auto_update.rs b/crates/auto_update/src/auto_update.rs
--- a/crates/auto_update/src/auto_update.rs
+++ b/crates/auto_update/src/auto_update.rs
@@ -0,0 +1,4 @@
+#[test]
+fn test_added() {}
+#[gpui::test]
+async fn added_without_prefix() {}
"""
            listing = listing or (
                "tests::test_isolated_shpool_daemon_probe_and_kill: test\n"
                "tests::test_historical: test\n"
                "tests::test_preserved: test\n"
                "tests::test_added: test\n"
                "tests::added_without_prefix: test\n"
                "tests::test_tab_width_bounds: test\n"
                "tests::test_title_bar_to_status_bar_keeps_title_when_status_bar_hidden: test\n"
            )
            injected = {
                "CARGO_HOME": "/toolchain-cache/cargo",
                "RUSTUP_HOME": "/toolchain-cache/rustup",
                "RUSTUP_TOOLCHAIN": "pinned-toolchain",
                "CARGO_PROFILE_RELEASE_DEBUG_ASSERTIONS": "false",
                "GH_TOKEN": "not-forwarded",
                "GITHUB_TOKEN": "not-forwarded",
                "CODEX_REQUEST_TOKEN": "not-forwarded",
                "ACTIONS_RUNTIME_TOKEN": "not-forwarded",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "not-forwarded",
                "ZED_UPDATE_MANIFEST_URL": "not-forwarded",
                "ZED_DOWNSTREAM_RELEASE_ID": "not-forwarded",
                "ZED_COMMIT_SHA": "not-forwarded",
            }
            commands = []
            test_runs = []
            self.last_test_runs = test_runs

            def capture(args, *, cwd, env):
                self.assert_isolated(env)
                commands.append((args, env.copy()))
                if args[:3] == ["git", "rev-parse", "HEAD"]:
                    return candidate
                if args[:2] == ["git", "diff"]:
                    return diff
                if args[:3] == ["cargo", "metadata", "--locked"]:
                    packages = [] if missing_package else [
                        {"id": "auto-update-id", "name": "auto_update"},
                        {"id": "workspace-id", "name": "workspace"},
                        {"id": "project-id", "name": "project"},
                    ]
                    return json.dumps({"packages": packages})
                if args[:2] == ["cargo", "test"] and "--no-run" in args:
                    return "".join(json.dumps({
                        "reason": "compiler-artifact",
                        "package_id": package_id,
                        "profile": {"test": True, "debug_assertions": instrumentation},
                        "executable": str(binary),
                    }) + "\n" for package, package_id in (
                        ("workspace", "workspace-id"), ("auto_update", "auto-update-id"),
                        ("project", "project-id"))
                        if package in args)
                if args[0] == str(binary) and args[1:3] == ["--list", "--format"]:
                    return listing
                raise AssertionError(f"unexpected captured command: {args}")

            def run(args, *, cwd, env, check=False, text=False, stdout=None, stderr=None):
                self.assert_isolated(env)
                commands.append((args, env.copy()))
                if args[0] == "git":
                    return SimpleNamespace(returncode=0, stdout="")
                test_runs.append((args, env.copy()))
                if result_code and check:
                    raise subprocess.CalledProcessError(result_code, args, output=result_output)
                return SimpleNamespace(returncode=result_code, stdout=result_output)

            with (
                patch.dict(os.environ, injected, clear=False),
                patch.object(release, "capture", side_effect=capture),
                patch.object(release.subprocess, "run", side_effect=run),
                patch.object(release, "run_rust_case", side_effect=run),
            ):
                release.run_regressions(app, manifest, upstream, candidate)
            return commands, test_runs

    def test_real_shpool_case_is_enabled_without_leaking_activation_to_other_cases(self):
        commands, test_runs = self.exercise(real_shpool_case=True)
        real = [(command, env) for command, env in test_runs
                if command[2] == "tests::test_isolated_shpool_daemon_probe_and_kill"]
        self.assertEqual(len(real), 1)
        self.assertEqual(real[0][1]["ZED_TEST_REAL_SHPOOL"], "1")
        self.assertTrue(all("ZED_TEST_REAL_SHPOOL" not in env for command, env in test_runs
                            if command[2] != real[0][0][2]))
        self.assertTrue(all("ZED_TEST_REAL_SHPOOL" not in env for command, env in commands
                            if command[0] == "cargo"))

    def test_missing_selector_instrumentation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "selector instrumentation"):
            self.exercise(selector_case=True, instrumentation=False)

    def test_unions_preserved_and_added_cases_with_isolated_credentials(self):
        commands, test_runs = self.exercise()
        command_args = [command for command, _ in commands]
        test_args = [command for command, _ in test_runs]
        self.assertTrue(any(command[:2] == ["cargo", "metadata"] for command in command_args))
        cargo_command = next(command for command in command_args if command[:2] == ["cargo", "test"])
        self.assertIn("-p", cargo_command)
        self.assertIn("auto_update", cargo_command)
        self.assertIn("--message-format=json-render-diagnostics", cargo_command)
        self.assertNotIn("--message-format=json", cargo_command)
        self.assertEqual(
            {command[2] for command in test_args},
            {"tests::test_historical", "tests::test_preserved", "tests::test_added",
             "tests::added_without_prefix"},
        )
        nonprefixed = next(command for command in test_args
                           if command[2] == "tests::added_without_prefix")
        self.assertEqual(nonprefixed[1:], ["--exact", "tests::added_without_prefix", "--nocapture"])
        self.assertTrue(all("--exact" in command for command in test_args))

    def test_failed_binary_output_is_visible_and_failure_propagates(self):
        for output in (
            "panic: preserved terminal cleanup failure\ntest result: FAILED",
            None,
        ):
            with self.subTest(output=output):
                captured = io.StringIO()
                with patch("sys.stdout", captured):
                    with self.assertRaises(subprocess.CalledProcessError) as raised:
                        self.exercise(result_code=1, result_output=output)
                self.assertEqual(raised.exception.returncode, 1)
                self.assertEqual(raised.exception.stdout, output)
                self.assertNotIn("Passed all", captured.getvalue())
                if output is not None:
                    self.assertIn("panic: preserved terminal cleanup failure", captured.getvalue())

    def test_all_runtime_failures_are_reported_in_one_final_attempt(self):
        captured = io.StringIO()
        with patch("sys.stdout", captured), self.assertRaises(subprocess.CalledProcessError):
            self.exercise(result_code=1, result_output="panic: regression failed\n")
        expected = {"tests::test_historical", "tests::test_preserved", "tests::test_added",
                    "tests::added_without_prefix"}
        self.assertEqual({command[2] for command, _ in self.last_test_runs}, expected)
        summary = captured.getvalue().split("Complete required Rust regression failure list:")[1]
        self.assertTrue(all(name in summary for name in expected))
        self.assertNotIn("Passed all", captured.getvalue())

    def test_missing_package_case_and_out_of_source_binary_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "package is missing"):
            self.exercise(missing_package=True)
        with self.assertRaisesRegex(ValueError, "regression missing"):
            self.exercise(listing="tests::test_historical: test\n")
        with self.assertRaisesRegex(ValueError, "outside the source checkout"):
            self.exercise(binary_outside=True)

    def test_zero_pass_ignored_and_failed_results_do_not_pass(self):
        with self.assertRaisesRegex(ValueError, "did not actually pass"):
            self.exercise(result_output="test result: ok. 0 passed; 0 failed; 1 ignored;")
        with self.assertRaises(subprocess.CalledProcessError):
            self.exercise(result_code=1, result_output="test result: FAILED")


class MacBundleInspectionTests(unittest.TestCase):
    def build_attempt(self, scenario):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app"
            app.mkdir()
            workspace = root / "workspace"
            workspace.mkdir()
            target = app / "target/aarch64-apple-darwin/release"
            identity = {
                "schema": 1,
                "repository": "maintainer/zed-fork",
                "upstream_repository": "zed-industries/zed",
                "platform": "macos/aarch64",
                "candidate_sha": "a" * 40,
                "upstream_sha": "b" * 40,
                "patchset_sha256": "d" * 64,
                "config_sha": "c" * 40,
                "version": "1.24.2",
                "release_id": 1234,
            }
            tag = f"build-1.24.2-{'b' * 12}-{'d' * 12}-{'a' * 12}"
            binary_uuid = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
            remote_uuid = "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"
            bundle_environments = []

            def run(*args, cwd=None, env=None):
                if args[:3] == ("git", "rev-parse", "HEAD"):
                    return "f" * 40 if scenario == "source" else identity["candidate_sha"]
                if args[:2] == ("git", "diff"):
                    return ""
                if args == ("uname", "-m"):
                    return "arm64"
                if args[:2] == ("hdiutil", "attach"):
                    mountpoint = Path(args[args.index("-mountpoint") + 1])
                    bundle = mountpoint / "Zed.app"
                    executable = bundle / "Contents/MacOS/zed"
                    executable.parent.mkdir(parents=True)
                    executable.write_bytes(b"fake app binary")
                    version = "1.24.3" if scenario == "version" else identity["version"]
                    info = bundle / "Contents/Info.plist"
                    info.parent.mkdir(parents=True, exist_ok=True)
                    info.write_bytes(plistlib.dumps({"CFBundleShortVersionString": version}))
                    return ""
                if args[0] == "lipo":
                    return "x86_64" if scenario == "architecture" else "arm64"
                if args[:2] == ("codesign", "--verify"):
                    return ""
                if len(args) == 2 and args[1] == "--system-specs":
                    sha = "f" * 40 if scenario == "binary_sha" else identity["candidate_sha"]
                    return f"Zed: v1.24.2+stable.{sha} build\n"
                if args[:2] == ("dwarfdump", "--uuid"):
                    path = Path(args[2])
                    uuid = binary_uuid
                    if path.name in {"remote_server", "remote_server.dwarf"}:
                        uuid = remote_uuid
                    if path.name == "zed.dwarf" and scenario == "symbol_uuid":
                        uuid = remote_uuid
                    return f"UUID: {uuid} (arm64) {path.name}\n"
                if args[:2] in {("hdiutil", "verify"), ("hdiutil", "detach")}:
                    return ""
                raise AssertionError(f"unexpected macOS tool invocation: {args}")

            def bundle_command(args, *, cwd, env, check):
                bundle_environments.append(env.copy())
                target.mkdir(parents=True, exist_ok=True)
                for name in ("Zed-aarch64.dmg", "zed.dwarf", "remote_server.dwarf", "remote_server"):
                    (target / name).write_bytes(b"tiny stub artifact")
                return SimpleNamespace(returncode=0)

            class DebugInfoProcess:
                def __init__(self):
                    self.stdout = iter(["DW_TAG_compile_unit\n"])

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def terminate(self):
                    pass

                def wait(self):
                    return 0

            with (
                patch.object(release.sys, "platform", "darwin"),
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_WORKSPACE": str(workspace),
                        "RELEASE_TAG": tag,
                        "GH_TOKEN": "not-forwarded",
                        "CODEX_REQUEST_TOKEN": "not-forwarded",
                        "ZED_UPDATE_MANIFEST_URL": "not-forwarded",
                        "ZED_DOWNSTREAM_RELEASE_ID": "not-forwarded",
                    },
                    clear=True,
                ),
                patch.object(release, "build_identity", return_value=identity),
                # Recipe behavior has dedicated tests; this fixture fakes macOS
                # inspection and intentionally does not contain a Git checkout.
                patch.object(release, "verify_upstream_recipes",
                             side_effect=ValueError("upstream recipe mismatch")
                             if scenario == "recipe" else None) as recipe,
                patch.object(release, "run", side_effect=run),
                patch.object(release.subprocess, "run", side_effect=bundle_command),
                patch.object(
                    release.subprocess,
                    "Popen",
                    side_effect=lambda *_args, **_kwargs: DebugInfoProcess(),
                ),
            ):
                try:
                    release.build(app)
                finally:
                    self.last_recipe_calls = recipe.call_args_list
                    self.last_bundle_environments = list(bundle_environments)
            release_directory = workspace / "release"
            return {path.name for path in release_directory.iterdir()}, bundle_environments

    def test_build_passes_only_the_exact_fork_identity_to_the_bundle(self):
        artifact_names, bundle_environments = self.build_attempt("valid")
        self.assertEqual(len(bundle_environments), 1)
        env = bundle_environments[0]
        self.assertEqual(len(self.last_recipe_calls), 1)
        self.assertEqual(self.last_recipe_calls[0].args[1], "b" * 40)
        self.assertEqual(env["CARGO_INCREMENTAL"], "0")
        self.assertFalse(any(key.startswith("CARGO_PROFILE_") for key in env))
        self.assertNotIn("RUSTUP_TOOLCHAIN", env)
        self.assertNotIn("MACOSX_DEPLOYMENT_TARGET", env)
        self.assertEqual(
            {key for key in env if key.startswith("ZED_")},
            {"ZED_UPDATE_MANIFEST_URL", "ZED_DOWNSTREAM_RELEASE_ID", "ZED_COMMIT_SHA"},
        )
        self.assertEqual(
            env["ZED_UPDATE_MANIFEST_URL"],
            "https://github.com/maintainer/zed-fork/releases/latest/download/latest.json",
        )
        self.assertEqual(env["ZED_DOWNSTREAM_RELEASE_ID"], "1234")
        self.assertEqual(env["ZED_COMMIT_SHA"], "a" * 40)
        self.assertEqual(
            artifact_names,
            {
                "Zed.dmg",
                "latest.json",
                "provenance.json",
                "SHA256SUMS",
                "zed-1.24.2-titlebarless-debug-symbols-macos-aarch64.tar.gz",
            },
        )

    def test_recipe_refusal_prevents_any_bundle_invocation(self):
        with self.assertRaisesRegex(ValueError, "upstream recipe mismatch"):
            self.build_attempt("recipe")
        self.assertEqual(len(self.last_recipe_calls), 1)
        self.assertEqual(self.last_bundle_environments, [])

    def test_inspector_rejects_wrong_source_version_architecture_and_symbol_uuid(self):
        for scenario in ("source", "version", "architecture", "binary_sha", "symbol_uuid"):
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                self.build_attempt(scenario)


class RegressionManifestAdoptionTests(unittest.TestCase):
    def test_adoption_preserves_baseline_and_unions_new_regression_cases(self):
        config = "a" * 40
        upstream = "b" * 40
        seed = "c" * 40
        repair_head = "d" * 40
        adopted = "e" * 40
        repository = "maintainer/zed-fork"
        state = {
            "schema": 1,
            "config": config,
            "upstream": upstream,
            "seed": seed,
            "head": repair_head,
            "number": 2,
            "branch": "repair/zed-source",
            "created_at": "2026-10-16T10:00:00Z",
        }
        environment = {
            "GITHUB_REPOSITORY": repository,
            "GITHUB_SHA": config,
            "REPAIR_PR": "2",
            "REPAIR_HEAD": repair_head,
            "CANDIDATE_PR": "1",
            "CANDIDATE_SHA": repair_head,
        }
        baseline = [["auto_update", "test_upstream_baseline"]]
        existing = [["auto_update", "test_existing_regression"]]
        adopted_manifest = {
            "schema": 1,
            "test_cases": baseline,
            "regression_cases": existing,
        }
        worktree = None

        def run(*args, cwd=None, env=None):
            nonlocal worktree
            if args[:3] == ("git", "worktree", "add"):
                worktree = Path(args[4])
                (worktree / "patches").mkdir(parents=True)
                (worktree / "patches/source.json").write_text(
                    json.dumps(adopted_manifest, indent=2) + "\n"
                )
                return ""
            if args[:3] == ("git", "rev-parse", "HEAD"):
                return adopted
            return ""

        def api(path, *, method="GET", data=None, token=None):
            if path == f"repos/{repository}/commits/downstream":
                return {"sha": config}
            return {}

        def subprocess_run(args, **kwargs):
            if args[:2] == ["git", "check-ignore"]:
                return SimpleNamespace(stdout="", returncode=1)
            if args[:2] == ["git", "apply"]:
                return SimpleNamespace(stdout="", returncode=0)
            return SimpleNamespace(stdout="", returncode=0)

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, environment, clear=True),
            patch.object(release, "repair_pull", return_value=state),
            patch.object(release, "repair_changes", return_value=(["patches/0013.patch"], False)),
            patch.object(release, "check_review", return_value=(True, "reviewed")),
            patch.object(release, "check_delta_for_publication"),
            patch.object(release, "complete_adoption"),
            patch.object(release, "minor_findings", return_value=[]),
            patch.object(release, "api", side_effect=api),
            patch.object(release, "pages", return_value=[]),
            patch.object(release, "run", side_effect=run),
            patch.object(release.subprocess, "check_output", return_value=b"verified patch diff"),
            patch.object(release.subprocess, "run", side_effect=subprocess_run),
            patch.object(release, "copy_patch_series", return_value=[Path("patches/0013.patch")]),
            patch.object(
                release,
                "patch_identity",
                return_value=([Path("patches/0013.patch")], "f" * 64),
            ),
            patch.object(
                release,
                "patch_regressions",
                return_value={
                    ("auto_update", "test_upstream_baseline"),
                    ("auto_update", "test_new_downstream_case"),
                },
            ),
        ):
            release.land_repair(Path(temporary))
            adopted = json.loads((worktree / "patches/source.json").read_text())
            self.assertEqual(adopted["test_cases"], baseline)
            self.assertEqual(
                adopted["regression_cases"],
                [existing[0], ["auto_update", "test_new_downstream_case"]],
            )


class PublishedAssetVerificationTests(unittest.TestCase):
    def test_published_release_revalidates_every_asset_and_direct_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            identity, _, _, _, assets, tag = metadata_fixture(directory)
            record = {
                "id": identity["release_id"],
                "draft": False,
                "prerelease": False,
                "tag_name": tag,
                "target_commitish": identity["candidate_sha"],
                "assets": list(assets.values()),
            }

            def read_asset(_repository, asset):
                return (directory / asset["name"]).read_bytes()

            with (
                patch.object(
                    release,
                    "api",
                    return_value={
                        "object": {"type": "commit", "sha": identity["candidate_sha"]}
                    },
                ),
                patch.object(release, "asset_bytes", side_effect=read_asset),
            ):
                release.verify_published_release(
                    identity["repository"],
                    record,
                    identity["candidate_sha"],
                    identity["upstream_sha"],
                    identity["patchset_sha256"],
                    identity["version"],
                )


class UpstreamCompilerRecipeTests(unittest.TestCase):
    def fixture(self, app):
        workflow = ("jobs:\n  run_tests_mac:\n    steps:\n"
                    "      - run: cargo nextest run --workspace --no-fail-fast --no-tests=warn\n"
                    "      - run: cp ./.cargo/ci-config.toml ./../.cargo/config.toml\n"
                    "  bundle_mac_aarch64:\n    env:\n      CARGO_INCREMENTAL: 0\n"
                    "    steps:\n      - run: ./script/bundle-mac aarch64-apple-darwin\n")
        originals = {
            ".cargo/config.toml": '[env]\nMACOSX_DEPLOYMENT_TARGET = "10.15.7"\n',
            ".cargo/ci-config.toml": '[profile.dev]\ndebug = "limited"\n',
            ".cargo/bundle-config.toml": "# unchanged upstream bundle config\n",
            "rust-toolchain.toml": '[toolchain]\nchannel = "stable"\n',
            "script/bundle-mac": "cargo --config .cargo/bundle-config.toml build --release\n",
            ".github/workflows/release.yml": workflow,
            "Cargo.toml": '[profile.release]\nlto = "thin"\ncodegen-units = 1\n',
        }
        for relative, content in originals.items():
            path = app / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        def capture(args, *, cwd, env):
            self.assertEqual(args[:2], ["git", "show"])
            self.assertEqual(cwd, app)
            self.assertTrue(args[2].startswith("b" * 40 + ":"))
            return originals[args[2].split(":", 1)[1]]
        return originals, capture

    def test_matching_upstream_recipe_needs_no_compilation(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "app"
            _, capture = self.fixture(app)
            with patch.object(release, "capture", side_effect=capture):
                release.verify_upstream_recipes(app, "b" * 40)

    def test_profile_and_recipe_drift_are_rejected_before_cargo(self):
        for relative in ("Cargo.toml", ".cargo/ci-config.toml", "script/bundle-mac"):
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as temporary:
                app = Path(temporary) / "app"
                _, capture = self.fixture(app)
                path = app / relative
                if relative == "Cargo.toml":
                    path.write_text(path.read_text().replace('lto = "thin"', 'lto = "fat"'))
                else:
                    path.write_text(path.read_text() + "# downstream compiler override\n")
                with patch.object(release, "capture", side_effect=capture):
                    with self.assertRaisesRegex(ValueError, "differs|differ"):
                        release.verify_upstream_recipes(app, "b" * 40)

    def test_changed_upstream_test_profile_requires_porting(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "app"
            originals, capture = self.fixture(app)
            relative = ".github/workflows/release.yml"
            originals[relative] = originals[relative].replace("cargo nextest run --workspace", "cargo nextest run --release --workspace")
            (app / relative).write_text(originals[relative])
            with patch.object(release, "capture", side_effect=capture):
                with self.assertRaisesRegex(ValueError, "test recipe changed"):
                    release.verify_upstream_recipes(app, "b" * 40)

    def test_upstream_parent_configuration_is_scoped_and_existing_data_survives(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "app"
            self.fixture(app)
            parent = app.parent / ".cargo"
            with release.upstream_test_configuration(app):
                self.assertEqual((parent / "config.toml").read_bytes(), (app / ".cargo/ci-config.toml").read_bytes())
            self.assertFalse(parent.exists())
            parent.mkdir()
            sentinel = parent / "config.toml"
            sentinel.write_text("existing shared configuration")
            with self.assertRaises(FileExistsError):
                with release.upstream_test_configuration(app):
                    self.fail("must not enter with an existing parent configuration")
            self.assertEqual(sentinel.read_text(), "existing shared configuration")

    def test_recipe_failure_never_starts_regressions(self):
        with (patch.dict(os.environ, {"UPSTREAM_SHA": "b" * 40, "CANDIDATE_SHA": "c" * 40}),
              patch.object(release, "verify_upstream_recipes", side_effect=ValueError("recipe drift")),
              patch.object(release, "run_regressions") as regressions):
            with self.assertRaisesRegex(ValueError, "recipe drift"):
                release.test_patches(Path("/unused-app"))
            regressions.assert_not_called()

    def test_host_compiler_overrides_are_not_forwarded(self):
        injected = {"CARGO_PROFILE_RELEASE_LTO": "fat", "CARGO_PROFILE_DEV_CODEGEN_UNITS": "1",
                    "CARGO_PROFILE_RELEASE_DEBUG": "none", "RUSTFLAGS": "-C lto=fat",
                    "CARGO_INCREMENTAL": "0", "CARGO_BUILD_TARGET": "x86_64-apple-darwin",
                    "RUSTUP_TOOLCHAIN": "nightly", "MACOSX_DEPLOYMENT_TARGET": "99.0"}
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, injected):
            environment = release.isolated_environment(Path(temporary))
            self.assertFalse(injected.keys() & environment.keys())

    def test_checked_linux_recipe_validation_precedes_pin_and_macos(self):
        import re
        text = (release.ROOT / ".github/workflows/release.yml").read_text()
        activation = re.search(r"(?ms)^  activate-policy:\n(.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", text)[1]
        self.assertIn("needs.policy-check.result == 'success'", activation)
        check = activation.index("checked/scripts/release.py check-upstream-recipe")
        pin = activation.index("Pin The Checked Controller For Subsequent Runs")
        self.assertLess(check, pin)
        checkout = activation[:check]
        self.assertIn("ref: ${{ needs.prepare.outputs.candidate_config }}", checkout)
        self.assertIn("path: checked\n          persist-credentials: false", checkout)
        self.assertNotIn("GH_TOKEN:", checkout)
        macos = re.search(r"(?ms)^  test:\n(.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", text)[1]
        self.assertIn("needs: [prepare, activate-policy, land]", macos)

    def test_cancelled_workflows_cannot_continue_expensive_or_publishing_jobs(self):
        text = (release.ROOT / ".github/workflows/release.yml").read_text()
        for name in ("test", "publish", "prepare", "repair"):
            import re
            block = re.search(rf"(?ms)^  {name}:\n(.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", text)[1]
            condition = block.split("    if:", 1)[1].split("    runs-on:", 1)[0]
            self.assertIn("!cancelled()", condition)
