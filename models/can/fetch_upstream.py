"""Fetch the qualification inputs pinned by profile.json during image preparation."""

import json
import subprocess
from pathlib import Path

profile = json.loads(Path(__file__).with_name("profile.json").read_text())
for name, upstream in profile["upstream"].items():
    checkout = Path("/opt") / name
    subprocess.run(["git", "clone", upstream["repository"], str(checkout)], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "checkout", upstream["revision"]], check=True
    )
