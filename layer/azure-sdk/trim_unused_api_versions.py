#!/usr/bin/env python3
"""
Deletes unused per-version submodules (v20YY_MM_DD/) from installed
azure-mgmt-* packages. These SDKs vendor every historical API version as a
separate submodule; since this app never passes api_version= explicitly
(see lambda/cleaners/azure.py), only each client's own computed default
versions are reachable at runtime — the rest is dead weight that pushes the
Lambda layer over its 250MB unzipped limit.

For every directory containing a root "_*client.py" file with sibling
v20*/ subdirectories, parses that file's DEFAULT_API_VERSION and any
per-operation-group overrides from its LATEST_PROFILE dict, then removes
every v20*/ folder not in that keep-set. Safe by construction: it only
ever removes version folders that aren't referenced by the SDK's own
version-resolution table for that client.

Usage: trim_unused_api_versions.py <site-packages-dir>
"""
import ast
import re
import shutil
import sys
from pathlib import Path


def find_default_and_overrides(client_file: Path) -> set[str]:
    src = client_file.read_text(errors="ignore")

    default_match = re.search(r'DEFAULT_API_VERSION\s*=\s*["\'](\d{4}-\d{2}-\d{2}[\w-]*)["\']', src)
    versions = {default_match.group(1)} if default_match else set()

    # Per-operation-group overrides live in a dict literal inside LATEST_PROFILE.
    profile_match = re.search(r"LATEST_PROFILE\s*=\s*ProfileDefinition\(\s*\{(.*?)\}\s*\}\s*,", src, re.DOTALL)
    if profile_match:
        for v in re.findall(r'["\'](\d{4}-\d{2}-\d{2}[\w-]*)["\']', profile_match.group(1)):
            versions.add(v)

    return versions


def version_to_dirname(version: str) -> str:
    # "2024-07-01" -> "v2024_07_01", "2021-02-01-preview" -> "v2021_02_01_preview"
    return "v" + version.replace("-", "_")


def main(root: str):
    root_path = Path(root)
    total_freed = 0
    total_kept_dirs = 0
    total_removed_dirs = 0

    for client_file in sorted(root_path.glob("**/_*client.py")):
        parent = client_file.parent
        if not parent.exists() or re.match(r"^v20\d{2}_\d{2}_\d{2}", parent.name):
            # Already removed by an earlier iteration, or this is a client
            # file living inside a version-pinned folder itself (e.g.
            # foo/v2019_10_01_preview/_foo_client.py) — not a trim root.
            continue
        version_dirs = sorted(d for d in parent.iterdir() if d.is_dir() and re.match(r"^v20\d{2}_\d{2}_\d{2}", d.name))
        if not version_dirs:
            continue

        versions = find_default_and_overrides(client_file)
        if not versions:
            print(f"  ! {parent}: no DEFAULT_API_VERSION found, skipping (keeping all {len(version_dirs)} versions)")
            continue

        keep_names = {version_to_dirname(v) for v in versions}
        for d in version_dirs:
            if d.name in keep_names:
                total_kept_dirs += 1
                continue
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d)
            total_freed += size
            total_removed_dirs += 1

    print(f"Removed {total_removed_dirs} unused API-version folders, kept {total_kept_dirs} in use.")
    print(f"Freed ~{total_freed / (1024*1024):.1f} MB.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
