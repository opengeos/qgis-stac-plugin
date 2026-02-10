#!/usr/bin/env python3
"""
Package the STAC Explorer QGIS plugin for distribution.

Creates a zip file suitable for upload to the QGIS plugin repository
or distribution via GitHub releases.

Usage:
    python package_plugin.py
    python package_plugin.py --output /path/to/output/plugin.zip
    python package_plugin.py --no-verify
"""

import argparse
import os
import re
import zipfile
from pathlib import Path

PLUGIN_NAME = "stac_explorer"

# Patterns to exclude from the zip
EXCLUDE_PATTERNS = [
    r"^ui_.*\.py$",
    r"^resources_rc\.py$",
    r"^.*_rc\.py$",
    r"^.*\.pyc$",
    r"^.*\.pyo$",
    r"^.*\.bak$",
    r"^.*~$",
    r"^\..*\.swp$",
    r"^.*\.orig$",
]

# Directory names to exclude
EXCLUDE_DIRS = {
    "__pycache__",
    "__MACOSX",
    ".git",
    ".svn",
    ".hg",
    ".idea",
    ".vscode",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".eggs",
    "*.egg-info",
    "build",
    "dist",
    "node_modules",
    "help",
}


def should_exclude_file(filename):
    """Check if a file should be excluded based on its name.

    Args:
        filename: Name of the file to check.

    Returns:
        True if the file should be excluded.
    """
    for pattern in EXCLUDE_PATTERNS:
        if re.match(pattern, filename):
            return True
    return False


def should_exclude_dir(dirname):
    """Check if a directory should be excluded.

    Args:
        dirname: Name of the directory to check.

    Returns:
        True if the directory should be excluded.
    """
    if dirname.startswith("."):
        return True
    if dirname in EXCLUDE_DIRS:
        return True
    if dirname.endswith(".egg-info"):
        return True
    return False


def get_version_from_metadata(plugin_dir):
    """Extract version from metadata.txt file.

    Args:
        plugin_dir: Path to the plugin directory.

    Returns:
        Version string from metadata.txt.
    """
    metadata_file = plugin_dir / "metadata.txt"
    if metadata_file.exists():
        with open(metadata_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("version="):
                    return line.split("=", 1)[1].strip()
    return "unknown"


def package_plugin(
    source_dir,
    output_path=None,
    target_name=PLUGIN_NAME,
    include_version=True,
):
    """Package the QGIS plugin into a zip file.

    Args:
        source_dir: Path to the stac_explorer directory.
        output_path: Optional path for the output zip file.
        target_name: Name for the root folder in the zip.
        include_version: Whether to include version in the zip filename.

    Returns:
        Path to the created zip file.
    """
    if not source_dir.exists():
        raise FileNotFoundError(f"Source directory not found: {source_dir}")

    if not source_dir.is_dir():
        raise ValueError(f"Source path is not a directory: {source_dir}")

    version = get_version_from_metadata(source_dir)

    if output_path is None:
        if include_version:
            zip_name = f"{target_name}-{version}.zip"
        else:
            zip_name = f"{target_name}.zip"
        output_path = source_dir.parent / zip_name

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    print(f"Packaging plugin from: {source_dir}")
    print(f"Output zip file: {output_path}")
    print(f"Root folder name in zip: {target_name}")
    print(f"Plugin version: {version}")
    print()

    files_added = 0
    files_excluded = 0

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs if not should_exclude_dir(d)]

            for file in files:
                file_path = Path(root) / file

                if should_exclude_file(file):
                    print(f"  Excluding: {file_path.relative_to(source_dir)}")
                    files_excluded += 1
                    continue

                if file.startswith("."):
                    print(f"  Excluding hidden: {file_path.relative_to(source_dir)}")
                    files_excluded += 1
                    continue

                rel_path = file_path.relative_to(source_dir)
                archive_name = Path(target_name) / rel_path

                zipf.write(file_path, archive_name)
                print(f"  Adding: {archive_name}")
                files_added += 1

    print()
    print("Package created successfully!")
    print(f"  Files added: {files_added}")
    print(f"  Files excluded: {files_excluded}")
    print(f"  Output: {output_path}")
    print(f"  Size: {output_path.stat().st_size / 1024:.1f} KB")

    return output_path


def verify_zip(zip_path):
    """Verify the contents of the created zip file.

    Args:
        zip_path: Path to the zip file to verify.
    """
    print()
    print("Verifying zip contents:")
    print("-" * 50)

    with zipfile.ZipFile(zip_path, "r") as zipf:
        has_issues = False
        for name in zipf.namelist():
            if "__pycache__" in name:
                print(f"  WARNING: Found __pycache__: {name}")
                has_issues = True
            if "__MACOSX" in name:
                print(f"  WARNING: Found __MACOSX: {name}")
                has_issues = True
            if ".git" in name.split("/"):
                print(f"  WARNING: Found .git: {name}")
                has_issues = True

        if not has_issues:
            print("  All checks passed!")

        print()
        print("Zip contents:")
        print("-" * 50)
        for name in sorted(zipf.namelist()):
            info = zipf.getinfo(name)
            if not name.endswith("/"):
                print(f"  {name} ({info.file_size} bytes)")


def main():
    """Run the packaging script."""
    parser = argparse.ArgumentParser(
        description="Package STAC Explorer QGIS plugin for distribution",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output path for the zip file",
    )
    parser.add_argument(
        "--source",
        "-s",
        type=Path,
        default=None,
        help=(
            "Source directory "
            f"(default: {PLUGIN_NAME} in the same folder as this script)"
        ),
    )
    parser.add_argument(
        "--name",
        "-n",
        type=str,
        default=PLUGIN_NAME,
        help=f"Target plugin name for the zip root folder (default: {PLUGIN_NAME})",
    )
    parser.add_argument(
        "--no-version",
        action="store_true",
        help="Don't include version in the zip filename",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip verification of the created zip",
    )

    args = parser.parse_args()

    script_dir = Path(__file__).parent.resolve()
    source_dir = args.source if args.source else script_dir / PLUGIN_NAME

    try:
        zip_path = package_plugin(
            source_dir=source_dir,
            output_path=args.output,
            target_name=args.name,
            include_version=not args.no_version,
        )

        if not args.no_verify:
            verify_zip(zip_path)

        print()
        print("=" * 50)
        print("Plugin packaged successfully!")
        print(f"Upload this file to the QGIS plugin repository:")
        print(f"  {zip_path}")
        print("=" * 50)

    except Exception as e:
        print(f"Error: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
