from importlib.metadata import version

import salmonpy


def test_public_version_matches_package_metadata():
    assert salmonpy.__version__ == version("salmonpy")
