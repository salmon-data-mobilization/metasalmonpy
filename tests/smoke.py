"""
Minimal smoke script for metasalmonpy.
"""

import tempfile

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

from metasalmonpy import create_sdp, read_salmon_datapackage


def run() -> None:
    if pd is None:
        raise RuntimeError("pandas not installed; install and rerun smoke test.")
    df = pd.DataFrame({"species": ["Coho", "Chinook"], "count": [100, 200]})
    tempdir = tempfile.mkdtemp(prefix="metasalmonpy-smoke-")
    create_sdp(
        df,
        path=tempdir,
        dataset_id="demo",
        table_id="observations",
        seed_semantics=False,
        overwrite=True,
    )
    pkg = read_salmon_datapackage(tempdir)
    assert "observations" in pkg["resources"]


if __name__ == "__main__":
    run()
    print("metasalmonpy smoke test passed")
