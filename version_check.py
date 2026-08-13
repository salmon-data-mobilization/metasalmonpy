from __future__ import annotations

import re
import warnings
from typing import Optional

import requests


def _version_parts(value: str) -> tuple[int, ...]:
    match = re.search(r"\d+(?:\.\d+)*", str(value))
    if not match:
        raise ValueError(f"Version {value!r} is not usable.")
    return tuple(int(part) for part in match.group(0).split("."))


def check_for_updates(
    repo: str = "salmon-data-mobilization/metasalmonpy",
    current: Optional[str] = None,
    timeout: float = 2,
    quiet: bool = False,
    request_fn=None,
) -> dict:
    """Check the latest GitHub release only when explicitly called."""
    if not isinstance(repo, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        repo.strip(),
    ):
        raise ValueError("repo must use the 'owner/name' form.")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero.")
    if current is None:
        from . import __version__

        current = __version__

    request_fn = request_fn or requests.get
    install_command = (
        "python -m pip install --upgrade "
        "git+https://github.com/salmon-data-mobilization/metasalmonpy.git"
    )
    result = {
        "status": "unavailable",
        "current_version": str(current),
        "latest_version": None,
        "update_available": None,
        "repo": repo,
        "release_tag": None,
        "release_url": None,
        "install_command": install_command,
        "message": None,
    }
    try:
        response = request_fn(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
        tag = str(body.get("tag_name") or "").strip()
        latest = re.sub(r"^[vV]", "", tag)
        current_parts = _version_parts(str(current))
        latest_parts = _version_parts(latest)
        width = max(len(current_parts), len(latest_parts))
        current_parts += (0,) * (width - len(current_parts))
        latest_parts += (0,) * (width - len(latest_parts))
        if current_parts < latest_parts:
            status = "update_available"
        elif current_parts == latest_parts:
            status = "up_to_date"
        else:
            status = "development_ahead"
        result.update(
            {
                "status": status,
                "latest_version": latest,
                "update_available": current_parts < latest_parts,
                "release_tag": tag,
                "release_url": body.get("html_url"),
                "message": body.get("name"),
            }
        )
    except Exception as exc:
        result["message"] = str(exc)

    if not quiet:
        if result["status"] == "update_available":
            warnings.warn(
                f"A newer metasalmonpy release is available: {current} -> "
                f"{result['latest_version']}. Upgrade with: {install_command}",
                UserWarning,
                stacklevel=2,
            )
        elif result["status"] == "unavailable":
            warnings.warn(
                f"Could not check for a newer metasalmonpy release: "
                f"{result['message']}",
                UserWarning,
                stacklevel=2,
            )
    return result


__all__ = ["check_for_updates"]
