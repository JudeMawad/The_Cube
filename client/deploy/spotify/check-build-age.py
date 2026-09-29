#!/usr/bin/env python3
"""Check the pinned binary and its build age without running the receiver."""

import argparse
import json
import sys

from soloist_release import DEFAULT_RELEASE, ReleaseError, load_release


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", default=DEFAULT_RELEASE)
    args = parser.parse_args(argv)
    try:
        _, record, status = load_release(args.release)
    except (OSError, ReleaseError):
        print("Soloist release validation failed; check release files and system clock.", file=sys.stderr)
        return 2
    print(json.dumps({"version": record["version_output"], **status}))
    return 10 if status["status"] == "expired" else 0


if __name__ == "__main__":
    sys.exit(main())
