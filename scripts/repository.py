"""The repository's files, as the check scripts see them."""

import subprocess
from pathlib import Path


def files() -> list[Path]:
    """Tracked files plus untracked ones that git doesn't ignore, relative to the current folder."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted({Path(name) for name in listed.split("\0") if name and Path(name).is_file()})
