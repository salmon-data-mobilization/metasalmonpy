"""S3 -- KNB environment support.

Mirrors metasalmon's ``tests/testthat/test-knb-environments.R`` at v0.4.0
(tag ``4e2bbb6c2a7cc578cb05f9f350c834c89796142c``).

Three invariants have their own tests here because each one, if it broke,
would break silently: a live production deposit that stopped demanding
confirmation, a test rehearsal that minted a production node identifier into a
deposited artifact, and an environment switched partway.

No test touches the network. The dry-run tests install an adapter whose every
method raises, which is the sentinel proving no adapter is constructed on the
planning path.
"""

import hashlib
import json
import os
import shutil
import tempfile
import unittest
import warnings

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

import metasalmonpy.knb_environments as knb_env
import metasalmonpy.knb_publication as knb
from metasalmonpy import publish_sdp_to_knb
from metasalmonpy.eml import write_eml_from_sdp
from metasalmonpy.text_safety import redact_secrets

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "knb")

# Values a production artifact contains and a test artifact must never
# contain, and the reverse. Kept as data so a new environment-derived URL is
# added in one place.
PRODUCTION_MARKERS = (
    "urn:node:KNB",
    "knb.ecoinformatics.org",
    "https://cn.dataone.org/cn/",
)

TEST_MARKERS = (
    "urn:node:mnTestKNB",
    "dev.nceas.ucsb.edu",
    "cn-stage.test.dataone.org",
)


def _extra_available() -> bool:
    try:
        import lxml.etree  # noqa: F401
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


_REQUIRES_EXTRA = unittest.skipUnless(
    _extra_available(),
    "requires the metasalmonpy[knb] extra (lxml, PyYAML)",
)


class _ExplodingAdapter:
    """Every method raises: proof the dry-run path never reaches an adapter."""

    def __getattr__(self, name):
        def explode(*args, **kwargs):
            raise AssertionError(
                f"the dry-run path must never call adapter.{name}"
            )

        return explode


def _sdp(test_case: unittest.TestCase, name: str = "sdp-public") -> str:
    root = tempfile.mkdtemp()
    test_case.addCleanup(shutil.rmtree, root, True)
    target = os.path.join(root, "sdp")
    shutil.copytree(os.path.join(_DATA_DIR, name), target)
    # Realpath so ``os.path.relpath`` against a returned artifact path --
    # which the publisher normalizes -- compares like with like on macOS,
    # where ``/tmp`` is a symlink to ``/private/tmp``.
    return os.path.realpath(target)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _sha256_file(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class KnbEnvironmentRegistryTests(unittest.TestCase):
    def test_the_registry_is_closed_complete_and_switches_whole(self):
        registry = knb_env.environment_registry()
        self.assertEqual(set(registry), {"test", "production"})

        for name, record in registry.items():
            # The record must carry exactly the declared field set -- no more,
            # no fewer. This is what fails when a field is added to one
            # environment and forgotten in the other.
            knb_env.validate_environment_config(record, name)
            self.assertEqual(
                set(record), set(knb_env.environment_fields())
            )

        test = knb_env.knb_config("test")
        production = knb_env.knb_config("production")

        # The verified facts, from the node documents themselves (2026-08-22).
        self.assertEqual(test["node_id"], "urn:node:mnTestKNB")
        self.assertEqual(test["dataone_network"], "STAGING")
        self.assertEqual(
            test["mn_base_url"], "https://dev.nceas.ucsb.edu/knb/d1/mn"
        )
        self.assertEqual(
            test["cn_base_url"], "https://cn-stage.test.dataone.org/cn"
        )
        self.assertEqual(test["token_option"], "dataone_test_token")
        self.assertFalse(test["durable"])
        self.assertEqual(test["max_replicas"], 0)

        self.assertEqual(production["node_id"], "urn:node:KNB")
        self.assertEqual(production["dataone_network"], "PROD")
        self.assertEqual(
            production["mn_base_url"],
            "https://knb.ecoinformatics.org/knb/d1/mn",
        )
        self.assertEqual(production["cn_base_url"], "https://cn.dataone.org/cn")
        self.assertEqual(production["token_option"], "dataone_token")
        self.assertTrue(production["durable"])
        self.assertEqual(production["max_replicas"], 3)

        # Atomicity: every derived URL belongs to that environment's own base
        # URL. A production Solr endpoint under a test coordinating node is the
        # exact failure this asserts cannot exist.
        for record in (test, production):
            self.assertTrue(
                record["mn_endpoint"].startswith(record["mn_base_url"])
            )
            self.assertTrue(
                record["object_endpoint"].startswith(record["mn_base_url"])
            )
            self.assertTrue(
                record["resolver"].startswith(record["cn_base_url"])
            )
            self.assertTrue(
                record["solr_endpoint"].startswith(record["cn_base_url"])
            )
            self.assertTrue(
                record["cn_endpoint"].startswith(record["cn_base_url"])
            )

        # No environment-distinguishing value may be shared between
        # environments.
        for field in knb_env.environment_fields():
            if field in ("max_replicas", "durable"):
                continue
            self.assertNotEqual(
                test[field], production[field], msg=f"shared field {field}"
            )

    def test_a_partially_specified_environment_record_is_refused(self):
        complete = knb_env.knb_config("production")

        # RED anchor for the atomicity guard: drop one field, and the record
        # must stop being usable rather than silently fall back to a default.
        for field in ("solr_endpoint", "node_id", "token_option"):
            broken = dict(complete)
            del broken[field]
            with self.assertRaises(ValueError) as caught:
                knb_env.validate_environment_config(broken, "production")
            self.assertIn("complete registry record", str(caught.exception))

        blank = dict(complete)
        blank["resolver"] = ""
        with self.assertRaises(ValueError) as caught:
            knb_env.validate_environment_config(blank, "production")
        self.assertIn("must be one non-empty string", str(caught.exception))

        extra = dict(complete)
        extra["custom_endpoint"] = "https://example.invalid/"
        with self.assertRaises(ValueError) as caught:
            knb_env.validate_environment_config(extra, "production")
        self.assertIn("Unexpected field", str(caught.exception))

    def test_environment_selection_is_exact(self):
        self.assertEqual(
            knb_env.knb_config("test")["knb_environment"], "test"
        )
        self.assertEqual(
            knb_env.knb_config("production")["knb_environment"], "production"
        )

        # Partial matching an environment name is how a rehearsal becomes a
        # production deposit.
        for value in ("prod", "PRODUCTION", "staging", "Test", ""):
            with self.assertRaises(ValueError):
                knb_env.knb_config(value)
        for value in (None, 1, ["test", "production"]):
            with self.assertRaises(ValueError) as caught:
                knb_env.knb_config(value)
            self.assertIn("must be exactly one", str(caught.exception))

    def test_a_dry_run_defaults_to_test_and_a_live_call_names_its_target(self):
        # Brett's 2026-08-22 ruling: develop against the test environment
        # first, then post to production once the package looks good there.
        self.assertEqual(
            knb_env.resolve_environment(None, dry_run=True)["knb_environment"],
            "test",
        )
        self.assertEqual(
            knb_env.resolve_environment(None, dry_run=True)["node_id"],
            "urn:node:mnTestKNB",
        )

        # An unstated environment on a live call is an error, not a default --
        # in particular it never silently means production.
        with self.assertRaises(ValueError) as caught:
            knb_env.resolve_environment(None, dry_run=False)
        self.assertIn("requires an explicit", str(caught.exception))
        self.assertEqual(
            knb_env.resolve_environment("production", dry_run=False)["node_id"],
            "urn:node:KNB",
        )

    def test_the_environment_is_re_derived_from_the_node_id(self):
        self.assertEqual(
            knb_env.config_for_node("urn:node:KNB")["knb_environment"],
            "production",
        )
        self.assertEqual(
            knb_env.config_for_node("urn:node:mnTestKNB")["knb_environment"],
            "test",
        )
        with self.assertRaises(ValueError) as caught:
            knb_env.config_for_node("urn:node:SOMETHINGELSE")
        self.assertIn("not a registered KNB member node", str(caught.exception))

        # The piecemeal switch, refused: a plan claiming the production network
        # under the test node identifier, and the reverse.
        for plan in (
            {"node_id": "urn:node:mnTestKNB", "environment": "PROD"},
            {"node_id": "urn:node:KNB", "environment": "STAGING"},
            {
                "node_id": "urn:node:KNB",
                "environment": "PROD",
                "knb_environment": "test",
            },
        ):
            with self.assertRaises(ValueError) as caught:
                knb_env.plan_config(plan)
            self.assertIn("mixes KNB environments", str(caught.exception))

        self.assertEqual(
            knb_env.plan_config(
                {
                    "node_id": "urn:node:KNB",
                    "environment": "PROD",
                    "knb_environment": "production",
                }
            )["solr_endpoint"],
            "https://cn.dataone.org/cn/v2/query/solr/",
        )

    def test_production_identifier_preimages_are_unchanged_by_scoping(self):
        # Production's scope is empty and is dropped, so every production PID
        # minted before this module is byte-identical. If this fails, existing
        # published packages can no longer be re-planned.
        self.assertEqual(
            knb_env.pid_preimage("", "data", "dataset", "table"),
            "data:dataset:table",
        )
        self.assertEqual(
            knb_env.pid_preimage(None, "data", "dataset"), "data:dataset"
        )
        self.assertEqual(
            knb_env.pid_preimage("knb-test", "data", "dataset"),
            "knb-test:data:dataset",
        )
        # A list argument flattens, which is how the EML package-id preimage
        # reaches this function on both sides.
        self.assertEqual(
            knb_env.pid_preimage("", ["a", "b"], "c"), "a:b:c"
        )
        self.assertEqual(knb_env.knb_config("production")["pid_scope"], "")
        self.assertTrue(knb_env.knb_config("test")["pid_scope"])


class KnbEnvironmentCredentialTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(knb.set_dataone_token, None)
        self.addCleanup(knb.set_dataone_test_token, None)
        knb.set_dataone_token(None)
        knb.set_dataone_test_token(None)

    def test_each_environment_names_its_own_missing_token(self):
        with self.assertRaises(ValueError) as caught:
            knb_env.require_token(knb_env.knb_config("production"))
        self.assertIn("dataone_token", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            knb_env.require_token(knb_env.knb_config("test"))
        self.assertIn("dataone_test_token", str(caught.exception))

        # The test credential is a different token: supplying only the
        # production one must not satisfy the test environment.
        knb.set_dataone_token("production-jwt")
        with self.assertRaises(ValueError) as caught:
            knb_env.require_token(knb_env.knb_config("test"))
        self.assertIn("dataone_test_token", str(caught.exception))
        self.assertEqual(
            knb_env.require_token(knb_env.knb_config("production")),
            "production-jwt",
        )

    def test_both_environment_tokens_are_redacted_from_captured_text(self):
        # 0.2.5 shipped the redaction rule structurally, for any qualified
        # ``*_token`` name. The test credential this stream introduces must
        # already be covered; asserting it here is what keeps that true.
        redacted = redact_secrets(
            "dataone_token=PRODSECRET dataone_test_token=TESTSECRET "
            "DATAONE_TEST_TOKEN=TESTSECRET"
        )
        self.assertNotIn("PRODSECRET", redacted)
        self.assertNotIn("TESTSECRET", redacted)


@_REQUIRES_EXTRA
class KnbEnvironmentPublicationTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(knb.set_knb_adapter, None)

    def _dry_run(self, path, knb_environment, public=True):
        knb.set_knb_adapter(_ExplodingAdapter())
        with warnings.catch_warnings():
            # A test plan warns that it is a rehearsal; that is asserted on its
            # own below rather than in every call.
            warnings.simplefilter("ignore", UserWarning)
            return publish_sdp_to_knb(
                path,
                public=public,
                dry_run=True,
                knb_environment=knb_environment,
            )

    def test_a_test_dry_run_mints_no_production_identity(self):
        path = _sdp(self)
        result = self._dry_run(path, "test")

        self.assertEqual(result["knb_environment"], "test")
        manifest = result["manifest"]
        self.assertEqual(manifest["node_id"], "urn:node:mnTestKNB")
        self.assertEqual(manifest["environment"], "STAGING")
        self.assertEqual(manifest["knb_environment"], "test")

        # Every deposited-artifact byte stream, checked together. The manifest,
        # the OAI-ORE resource map, and the EML record are the three things a
        # live call would send; none may carry production identity.
        artifacts = {
            "manifest": _read_text(str(result["manifest_path"])),
            "resource_map": _read_text(str(result["resource_map_path"])),
            "eml": _read_text(
                os.path.join(path, "publication", "test", "eml.xml")
            ),
        }
        for name, text in artifacts.items():
            for marker in PRODUCTION_MARKERS:
                self.assertNotIn(marker, text, msg=f"{marker} in {name}")

        # ... and the resource map and EML must actually carry the test
        # identity, so this test cannot pass by producing empty artifacts.
        self.assertIn("cn-stage.test.dataone.org", artifacts["resource_map"])
        self.assertIn("dev.nceas.ucsb.edu", artifacts["eml"])

    def test_a_test_dry_run_leaves_the_reviewed_production_eml_intact(self):
        path = _sdp(self)
        production_eml = os.path.join(path, "metadata", "eml.xml")
        write_eml_from_sdp(path, overwrite=True)
        self.assertTrue(os.path.exists(production_eml))
        before = _sha256_file(production_eml)

        self._dry_run(path, "test")

        # Assert on the file hash rather than the return value: the return
        # value would look fine either way.
        self.assertEqual(before, _sha256_file(production_eml))
        self.assertTrue(
            os.path.exists(os.path.join(path, "publication", "test", "eml.xml"))
        )

    def test_test_and_production_plans_share_no_minted_identifier(self):
        test_result = self._dry_run(_sdp(self), "test")
        production_result = self._dry_run(_sdp(self), "production")

        def pids(result):
            return {
                str(obj["pid"]) for obj in result["manifest"]["objects"]
            }

        self.assertEqual(pids(test_result) & pids(production_result), set())
        self.assertNotEqual(
            test_result["package_id"], production_result["package_id"]
        )
        self.assertNotEqual(
            test_result["series_id"], production_result["series_id"]
        )
        # The SDP archive is the sharpest case: its bytes are identical in both
        # environments, so only the environment scope separates its identifier.
        self.assertNotEqual(
            test_result["resource_map_pid"],
            production_result["resource_map_pid"],
        )

    def test_each_environment_writes_its_own_default_paths(self):
        test_path = _sdp(self)
        production_path = _sdp(self)
        test_result = self._dry_run(test_path, "test")
        production_result = self._dry_run(production_path, "production")

        self.assertEqual(
            os.path.relpath(str(test_result["manifest_path"]), test_path),
            os.path.join("publication", "test", "knb-manifest.json"),
        )
        self.assertEqual(
            os.path.relpath(str(test_result["resource_map_path"]), test_path),
            os.path.join("publication", "test", "resource-map.rdf"),
        )
        # Production keeps its existing default, so a package published before
        # this change still finds its manifest where it left it.
        self.assertEqual(
            os.path.relpath(
                str(production_result["manifest_path"]), production_path
            ),
            os.path.join("publication", "knb-manifest.json"),
        )
        self.assertTrue(
            os.path.exists(
                os.path.join(production_path, "metadata", "eml.xml")
            )
        )
        self.assertFalse(
            os.path.isdir(
                os.path.join(production_path, "publication", "test")
            )
        )

    def test_an_omitted_environment_defaults_to_test_only_for_a_dry_run(self):
        path = _sdp(self)
        knb.set_knb_adapter(_ExplodingAdapter())
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = publish_sdp_to_knb(path, public=True, dry_run=True)
        self.assertEqual(result["knb_environment"], "test")
        # Said at the call, not only in the documentation.
        rehearsal = [
            str(item.message)
            for item in caught
            if "rehearsal" in str(item.message)
        ]
        self.assertEqual(len(rehearsal), 1)
        self.assertIn("urn:node:mnTestKNB", rehearsal[0])

        # Live with no environment stated: refused before anything is planned.
        with self.assertRaises(ValueError) as error:
            publish_sdp_to_knb(
                path, public=True, dry_run=False, confirm=True
            )
        self.assertIn("requires an explicit", str(error.exception))

    def test_a_live_publish_still_demands_explicit_confirmation(self):
        path = _sdp(self)
        knb.set_knb_adapter(_ExplodingAdapter())
        # The confirmation gate is the oldest safety property on this path, and
        # naming an environment must not become a way around it. A rehearsal
        # does not relax it either.
        for environment, confirm in (
            ("production", None),
            ("production", False),
            ("test", None),
        ):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with self.assertRaises(ValueError) as caught:
                    publish_sdp_to_knb(
                        path,
                        public=True,
                        dry_run=False,
                        confirm=confirm,
                        knb_environment=environment,
                    )
            self.assertIn("confirm=True", str(caught.exception))

    def test_production_identifiers_are_unchanged_by_the_environment_scope(self):
        # The strongest statement the release makes about production: nothing
        # an existing published package depends on moved. The fixture is R's
        # own v0.1.8 output, so this is a cross-implementation pin, and the
        # empty production ``pid_scope`` is what makes it hold.
        result = self._dry_run(_sdp(self), "production")
        with open(
            os.path.join(_DATA_DIR, "r", "public", "knb-manifest.json"),
            encoding="utf-8",
        ) as handle:
            era = json.load(handle)

        era_pids = {
            (str(obj["role"]), str(obj["path"])): str(obj["pid"])
            for obj in era["objects"]
        }
        planned = {
            (str(obj["role"]), str(obj["path"])): str(obj["pid"])
            for obj in result["manifest"]["objects"]
        }
        # The metadata object and every data object are byte-derived on both
        # sides; the ZIP and ORE are contract-level only (PARITY.md rows 4, 17).
        shared = {
            key for key in era_pids if key[0] in ("metadata", "data")
        }
        self.assertTrue(shared)
        self.assertTrue(shared <= set(planned))
        for key in sorted(shared):
            self.assertEqual(planned[key], era_pids[key], msg=str(key))
        self.assertEqual(result["package_id"], era["package_id"])
        self.assertEqual(result["series_id"], era["series_id"])

    def test_a_test_rehearsal_survives_an_ordinary_package_rewrite(self):
        from metasalmonpy import (
            read_salmon_datapackage,
            write_salmon_datapackage,
        )

        path = _sdp(self)
        self._dry_run(path, "test")
        rehearsal = os.path.join(
            path, "publication", "test", "knb-manifest.json"
        )
        self.assertTrue(os.path.exists(rehearsal))
        before = _sha256_file(rehearsal)

        # ``publication/`` is a sidecar the base writer preserves. A rehearsal
        # is publication-writer output, so an ordinary metadata rewrite must
        # leave it alone -- registering it as a package-managed path would
        # delete it here.
        package = read_salmon_datapackage(path)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            write_salmon_datapackage(
                resources=package["resources"],
                dataset_meta=package["dataset"],
                table_meta=package["tables"],
                dict_df=package["dictionary"],
                codes=package["codes"],
                path=path,
                overwrite=True,
            )

        self.assertTrue(os.path.exists(rehearsal))
        self.assertEqual(before, _sha256_file(rehearsal))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
