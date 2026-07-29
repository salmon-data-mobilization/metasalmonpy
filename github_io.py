from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import time
from typing import Dict, Optional

try:
    import importlib.metadata as importlib_metadata  # type: ignore
except ImportError:  # pragma: no cover
    import importlib_metadata  # type: ignore

import requests

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("salmonpy requires pandas; install via `pip install pandas`.") from exc


def github_raw_url(
    path: str, ref: str = "main", repo: Optional[str] = None
) -> str:
    """
    Build a stable raw.githubusercontent.com URL for a GitHub repository.

    If a full HTTP(S) URL is supplied, it is returned unchanged after stripping
    any query string; GitHub blob URLs are rewritten to raw URLs automatically.
    """
    target = _resolve_github_path(path, ref=ref, repo=repo)
    return target["url"]


def read_github_csv(
    path: str,
    ref: str = "main",
    repo: Optional[str] = None,
    token: Optional[str] = None,
    **kwargs,
) -> pd.DataFrame:
    """
    Read a CSV from a GitHub repository.

    Accepts a repo path or full GitHub/raw URL, sends the GitHub PAT via the
    Authorization header, retries transient errors, and returns a pandas DataFrame.
    """
    target = _resolve_github_path(path, ref=ref, repo=repo)
    token = token or _github_token()
    if not token:
        raise ValueError(
            "No GitHub token found. Set GITHUB_PAT/GH_TOKEN or run "
            "metasalmon::ms_setup_github() to configure git credentials."
        )

    headers = {
        "Authorization": f"token {token}",
        "User-Agent": _user_agent(),
        "Accept": "text/csv",
    }

    resp = _perform_request(target["url"], headers=headers)

    if resp.status_code == 401:
        raise PermissionError("GitHub authentication failed. Refresh your PAT and retry.")

    if resp.status_code == 403:
        if resp.headers.get("x-github-sso"):
            raise PermissionError(
                "Access blocked by org SSO. Re-authorize your PAT for this org in GitHub settings."
            )
        raise PermissionError("Access to the repository was denied. Confirm your PAT has repo scope.")

    if resp.status_code == 404:
        raise FileNotFoundError(
            f"{target['path']} not found at ref {target['ref']} in {target['repo']}."
        )

    resp.raise_for_status()
    return pd.read_csv(io.BytesIO(resp.content), **kwargs)


def ms_setup_github(repo: str = "dfo-pacific-science/qualark-data", token: Optional[str] = None) -> str:
    """
    Verify GitHub token discovery for private-repository CSV access.

    Python cannot safely create or store a PAT for you. Set GITHUB_PAT/GH_TOKEN
    or configure git credentials, then call this to confirm the token works.
    """
    token_val = token or _github_token()
    if not token_val:
        raise ValueError("No GitHub token found. Set GITHUB_PAT/GH_TOKEN or configure git credentials.")
    repo = repo.strip("/")
    resp = requests.get(
        f"https://api.github.com/repos/{repo}",
        headers={
            "Authorization": f"token {token_val}",
            "User-Agent": _user_agent(),
            "Accept": "application/vnd.github.v3+json",
        },
        timeout=15,
    )
    if resp.status_code == 401:
        raise PermissionError("GitHub authentication failed. Refresh your PAT and retry.")
    if resp.status_code == 403 and resp.headers.get("x-github-sso"):
        raise PermissionError("Access blocked by org SSO. Re-authorize your PAT for this org in GitHub settings.")
    if resp.status_code == 404:
        raise FileNotFoundError(f"Repository '{repo}' was not found or token lacks access.")
    resp.raise_for_status()
    return token_val



def _perform_request(url: str, headers: Dict[str, str], max_tries: int = 4) -> requests.Response:
    """Perform a GET with simple exponential backoff for transient errors."""
    last_exc: Optional[Exception] = None
    for attempt in range(max_tries):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
        except requests.RequestException as exc:  # pragma: no cover - network failure
            last_exc = exc
            if attempt == max_tries - 1:
                raise
            time.sleep(2**attempt * 0.5)
            continue

        if resp.status_code >= 500 and attempt < max_tries - 1:
            time.sleep(2**attempt * 0.5)
            continue

        return resp

    if last_exc:
        raise last_exc

    raise RuntimeError("Request failed without a response.")


def _github_token() -> Optional[str]:
    """
    Look for a GitHub token in env vars or the git credential store.

    Order of precedence: GITHUB_PAT, GH_TOKEN, git credential helper (password entry).
    """
    for env_var in ("GITHUB_PAT", "GH_TOKEN"):
        value = os.getenv(env_var)
        if value:
            return value

    git = shutil.which("git")
    if not git:
        return None

    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            text=True,
            capture_output=True,
            check=True,
            timeout=5,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                password = line.split("=", 1)[1].strip()
                if password:
                    return password
    except Exception:
        return None

    return None


def _resolve_github_path(path: str, ref: str, repo: Optional[str]) -> Dict[str, str]:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string.")
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("ref must be a non-empty string.")
    if repo is not None and (not isinstance(repo, str) or "/" not in repo):
        raise ValueError("repo must look like 'owner/name'.")

    clean_repo = repo.lstrip("/") if repo else None
    clean_ref = ref.strip()
    blob_pattern = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")
    raw_pattern = re.compile(r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$")

    if re.match(r"^https?://", path):
        clean_url = path.split("?", 1)[0]

        blob_match = blob_pattern.match(clean_url)
        if blob_match:
            owner, name, blob_ref, blob_path = blob_match.groups()
            return {
                "url": f"https://raw.githubusercontent.com/{owner}/{name}/{blob_ref}/{blob_path}",
                "repo": f"{owner}/{name}",
                "ref": blob_ref,
                "path": blob_path,
            }

        raw_match = raw_pattern.match(clean_url)
        if raw_match:
            owner, name, raw_ref, raw_path = raw_match.groups()
            return {
                "url": clean_url,
                "repo": f"{owner}/{name}",
                "ref": raw_ref,
                "path": raw_path,
            }

        return {"url": clean_url, "repo": clean_repo or "", "ref": clean_ref, "path": path.lstrip("/")}

    clean_path = path.lstrip("/")
    if not clean_repo:
        raise ValueError("repo is required when path is not a full URL.")
    return {
        "url": f"https://raw.githubusercontent.com/{clean_repo}/{clean_ref}/{clean_path}",
        "repo": clean_repo,
        "ref": clean_ref,
        "path": clean_path,
    }


def _user_agent() -> str:
    try:
        version = importlib_metadata.version("salmonpy")
    except Exception:  # pragma: no cover - fallback only
        version = "unknown"
    return f"salmonpy/{version}"


def read_github_csv_dir(
    path: str,
    ref: str = "main",
    repo: Optional[str] = None,
    token: Optional[str] = None,
    pattern: str = r"\.csv$",
    **kwargs,
) -> Dict[str, pd.DataFrame]:
    """
    Read all CSV files from a GitHub directory.

    Uses the GitHub Contents API to list directory contents, filter for CSV files,
    and read each into a pandas DataFrame.

    Parameters
    ----------
    path : str
        Path to directory in repository (e.g., "data" or "inst/extdata")
        or full GitHub URL. Use "" for repository root.
    ref : str, default="main"
        Git reference (branch, tag, or commit SHA)
    repo : str, optional
        Repository in "owner/name" format (required if path is not a full URL)
    token : str, optional
        GitHub personal access token (PAT). If None, uses _github_token()
    pattern : str, default=r"\\.csv$"
        Regex pattern to filter files (case-insensitive)
    **kwargs
        Additional arguments passed to pd.read_csv()

    Returns
    -------
    Dict[str, pd.DataFrame]
        Dictionary mapping filenames (without .csv extension) to DataFrames

    Raises
    ------
    ValueError
        If no GitHub token found or path is invalid
    PermissionError
        If authentication fails or SSO authorization required
    FileNotFoundError
        If directory not found

    Examples
    --------
    >>> # Read all CSVs from a directory
    >>> data = read_github_csv_dir(
    ...     "inst/extdata",
    ...     repo="salmon-data-mobilization/metasalmon",
    ...     ref="main"
    ... )
    >>> print(data.keys())  # dict_keys(['column_dictionary', 'nuseds-fraser-coho-sample', ...])
    >>> print(data['column_dictionary'].head())
    """
    target = _resolve_github_path(path if path else "dummy", ref=ref, repo=repo)
    token_val = token or _github_token()

    if not token_val:
        raise ValueError(
            "No GitHub token found. Set GITHUB_PAT/GH_TOKEN or run "
            "metasalmon::ms_setup_github() to configure git credentials."
        )

    # Build API endpoint for directory contents
    api_url = f"https://api.github.com/repos/{target['repo']}/contents"
    if path and path.strip():
        # Remove leading slashes and handle blob URLs
        clean_path = path.strip().lstrip("/")
        # If it's a blob URL, extract just the path part
        if "/blob/" in clean_path:
            clean_path = target['path']
        api_url = f"{api_url}/{clean_path}"

    headers = {
        "Authorization": f"token {token_val}",
        "User-Agent": _user_agent(),
        "Accept": "application/vnd.github.v3+json",
    }

    # Add ref parameter
    params = {"ref": target["ref"]}

    try:
        resp = requests.get(api_url, headers=headers, params=params, timeout=15)

        if resp.status_code == 401:
            raise PermissionError("GitHub authentication failed. Refresh your PAT and retry.")

        if resp.status_code == 403:
            if resp.headers.get("x-github-sso"):
                raise PermissionError(
                    "Access blocked by org SSO. Re-authorize your PAT for this org in GitHub settings."
                )
            raise PermissionError("Access to the repository was denied. Confirm your PAT has repo scope.")

        if resp.status_code == 404:
            raise FileNotFoundError(
                f"Directory '{path}' not found at ref '{target['ref']}' in {target['repo']}."
            )

        resp.raise_for_status()
        contents = resp.json()

    except requests.RequestException as exc:
        raise RuntimeError(f"Unable to list directory contents: {exc}") from exc

    # Handle single file response (API returns dict, not list)
    if isinstance(contents, dict) and contents.get("type") == "file":
        raise ValueError(
            f"Path '{path}' is a file, not a directory. Use read_github_csv() instead."
        )

    # Ensure contents is a list
    if not isinstance(contents, list):
        print(f"ℹ Directory '{path}' is empty or invalid.")
        return {}

    # Filter for CSV files
    csv_files = [
        item for item in contents
        if item.get("type") == "file" and re.search(pattern, item.get("name", ""), re.IGNORECASE)
    ]

    if not csv_files:
        print(f"ℹ No CSV files found in '{path}'.")
        return {}

    # Read each CSV file
    print(f"ℹ Reading {len(csv_files)} CSV file{'s' if len(csv_files) > 1 else ''}...")
    result = {}

    for item in csv_files:
        file_path = item["path"]
        # Use read_github_csv to handle authentication and retries
        df = read_github_csv(
            path=file_path,
            ref=target["ref"],
            repo=target["repo"],
            token=token_val,
            **kwargs
        )
        # Use filename without extension as key
        filename = re.sub(r"\.csv$", "", item["name"], flags=re.IGNORECASE)
        result[filename] = df

    return result


__all__ = [
    "github_raw_url",
    "ms_setup_github",
    "read_github_csv",
    "read_github_csv_dir"
]
