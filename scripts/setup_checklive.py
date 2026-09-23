"""Prepare only a new pinned checkout; never reset an existing checkout."""
import argparse
from pathlib import Path
import subprocess

REPO = "https://github.com/tmq9999/Check-Account-ChatGPT.git"
COMMIT = "791beb350370c191bbe8ddbe0b4a3fa0072620d9"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path(".deps/Check-Account-ChatGPT"))
    target = parser.parse_args().path.resolve()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--no-checkout", REPO, str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "--detach", COMMIT], check=True)
    revision = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(target), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, check=True).stdout
    if revision != COMMIT or dirty:
        raise SystemExit("Existing checkout differs from the pinned source; left unchanged. Select a new path.")
    print("CheckLive dependency ready at:", target)


if __name__ == "__main__":
    main()
