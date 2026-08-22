import os
import unittest
from unittest import mock

import requests

from metasalmonpy import github_raw_url, read_github_csv
from metasalmonpy import github_io


class GithubIOTests(unittest.TestCase):
    def test_github_raw_url_builds(self):
        url = github_raw_url("path/to/file.csv", repo="owner/repo")
        self.assertEqual(
            url,
            "https://raw.githubusercontent.com/owner/repo/main/path/to/file.csv",
        )

    def test_blob_and_raw_resolution(self):
        blob = github_io._resolve_github_path(
            "https://github.com/owner/repo/blob/main/path/to/file.csv",
            ref="ignored",
            repo=None,
        )
        self.assertEqual(blob["url"], "https://raw.githubusercontent.com/owner/repo/main/path/to/file.csv")
        self.assertEqual(blob["repo"], "owner/repo")
        self.assertEqual(blob["ref"], "main")

        raw = github_io._resolve_github_path(
            "https://raw.githubusercontent.com/owner/repo/main/path/to/file.csv",
            ref="ignored",
            repo=None,
        )
        self.assertEqual(raw["path"], "path/to/file.csv")

        token_url = "https://raw.githubusercontent.com/owner/repo/main/path/to/file.csv?token=SECRET"
        token_clean = github_io._resolve_github_path(token_url, ref="ignored", repo=None)
        self.assertEqual(token_clean["url"], "https://raw.githubusercontent.com/owner/repo/main/path/to/file.csv")

    def test_read_github_csv_works_without_token(self):
        # Mirrors metasalmon: public repositories are readable anonymously —
        # the Authorization header is sent only when a token is discovered.
        captured = {}

        def fake_request(url, headers, **kwargs):
            captured["headers"] = headers
            resp = mock.Mock()
            resp.status_code = 200
            resp.content = b"a,b\n1,2\n"
            return resp

        with mock.patch("metasalmonpy.github_io._github_token", return_value=None):
            with mock.patch("metasalmonpy.github_io._perform_request", side_effect=fake_request):
                df = read_github_csv("data/file.csv", repo="owner/repo", token="")
        self.assertEqual(len(df), 1)
        self.assertNotIn("Authorization", captured["headers"])

    def test_read_github_csv_integration(self):
        if not os.getenv("METASALMONPY_RUN_QUALARK_TEST", ""):
            self.skipTest("Qualark fetch test disabled. Set METASALMONPY_RUN_QUALARK_TEST=1 to enable.")

        token = github_io._github_token()
        if not token:
            self.skipTest("No GitHub token configured; skipping Qualark fetch test.")

        repo = os.getenv("METASALMONPY_QUALARK_TEST_REPO", "dfo-pacific-science/qualark-data")
        path = os.getenv("METASALMONPY_QUALARK_TEST_PATH", "data/gold/dimension_tables/dim_date.csv")
        ref = os.getenv("METASALMONPY_QUALARK_TEST_REF", "main")
        headers = {"Authorization": f"token {token}", "User-Agent": "metasalmonpy-test"}

        try:
            resp = requests.get(f"https://api.github.com/repos/{repo}", headers=headers, timeout=10)
        except requests.RequestException as exc:
            self.skipTest(f"Network unavailable for GitHub API: {exc}")
        if resp.status_code != 200:
            self.skipTest(f"Cannot access {repo}: {resp.status_code}")

        try:
            resp = requests.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers=headers,
                params={"ref": ref},
                timeout=10,
            )
        except requests.RequestException as exc:
            self.skipTest(f"Network unavailable for contents check: {exc}")
        if resp.status_code == 404:
            self.skipTest("Test CSV path not reachable.")
        if resp.status_code != 200:
            self.skipTest(f"Content check failed: {resp.status_code}")

        try:
            df = read_github_csv(path, ref=ref, repo=repo, token=token)
        except requests.RequestException as exc:
            self.skipTest(f"Network unavailable for raw fetch: {exc}")
        self.assertGreater(df.shape[0], 0)
        self.assertGreater(df.shape[1], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
