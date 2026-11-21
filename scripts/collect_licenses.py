# Ported from MaxWay12/Genshin-Navigator@99e1e13:scripts/collect_licenses.py (MIT License)
"""
Collect installed Python package license metadata for requirements.txt packages
and optionally export full third-party license texts for portable/installer builds.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

LICENSE_WORDS = ("license", "copying", "notice", "authors")
REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def parse_requirements(requirements_path: Path) -> list[str]:
    """Parse top-level distribution names from a requirements.txt file."""
    if not requirements_path.is_file():
        raise FileNotFoundError(f"Requirements file not found: {requirements_path}")

    names: list[str] = []
    seen: set[str] = set()
    for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = REQ_NAME_RE.match(line)
        if not match:
            continue
        pkg = match.group(1)
        norm = pkg.lower().replace("_", "-")
        if norm not in seen:
            seen.add(norm)
            names.append(pkg)
    return names


def _resolve_license_label(metadata: importlib.metadata.PackageMetadata) -> str:
    """Resolve a concise license identifier from PEP 639 / Core Metadata fields."""
    lic_expr = (metadata.get("License-Expression") or "").strip()
    if lic_expr:
        return lic_expr

    raw_lic = (metadata.get("License") or "").strip()
    if raw_lic and raw_lic.upper() != "UNKNOWN":
        first_line = next((ln.strip() for ln in raw_lic.splitlines() if ln.strip()), "")
        if first_line and len(first_line) <= 100:
            return first_line

    classifiers = [
        c for c in (metadata.get_all("Classifier") or []) if c.startswith("License ::")
    ]
    if classifiers:
        labels = [c.split("::")[-1].strip() for c in classifiers]
        return " / ".join(dict.fromkeys(labels))

    if raw_lic and raw_lic.upper() != "UNKNOWN":
        return "Custom (see package LICENSE file)"
    return "Unknown"


def _resolve_homepage(metadata: importlib.metadata.PackageMetadata) -> str:
    home = (metadata.get("Home-Page") or "").strip()
    if home and home.upper() != "UNKNOWN":
        return home
    for entry in metadata.get_all("Project-URL") or []:
        if "," in entry:
            label, url = [part.strip() for part in entry.split(",", 1)]
            if label.lower() in {"homepage", "home", "repository", "source", "source code"}:
                return url
    for entry in metadata.get_all("Project-URL") or []:
        if "," in entry:
            return entry.split(",", 1)[1].strip()
    return "-"


def _find_license_files(distribution: importlib.metadata.Distribution) -> list[Path]:
    found: list[Path] = []
    for item in distribution.files or ():
        filename = Path(str(item)).name.lower()
        if not any(word in filename for word in LICENSE_WORDS):
            continue
        source = Path(distribution.locate_file(item))
        if source.is_file():
            found.append(source)
    return found


def collect_metadata_summary(
    packages: Sequence[str],
    *,
    include_python: bool = True,
) -> list[dict[str, Any]]:
    """Inspect importlib.metadata for each requested package."""
    entries: list[dict[str, Any]] = []
    if include_python:
        entries.append(
            {
                "package": "Python",
                "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "license": "PSF-2.0",
                "installed": True,
                "homepage": "https://www.python.org/",
                "license_files": 1 if (Path(sys.base_prefix) / "LICENSE.txt").is_file() else 0,
            }
        )

    for pkg in packages:
        try:
            dist = importlib.metadata.distribution(pkg)
        except importlib.metadata.PackageNotFoundError:
            entries.append(
                {
                    "package": pkg,
                    "version": "-",
                    "license": "Not installed",
                    "installed": False,
                    "homepage": "-",
                    "license_files": 0,
                }
            )
            continue

        md = dist.metadata
        lic_files = _find_license_files(dist)
        entries.append(
            {
                "package": md.get("Name", pkg),
                "version": dist.version,
                "license": _resolve_license_label(md),
                "installed": True,
                "homepage": _resolve_homepage(md),
                "license_files": len(lic_files),
            }
        )
    return entries


def format_markdown_summary(entries: Sequence[dict[str, Any]]) -> str:
    """Format package license metadata as a clean Markdown table."""
    lines = [
        "# Third-Party Python Dependency Licenses",
        "",
        "| Package | Version | License | Status | Homepage |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in entries:
        status = "installed" if item["installed"] else "not installed"
        lines.append(
            f"| {item['package']} | {item['version']} | {item['license']} | "
            f"{status} | {item['homepage']} |"
        )
    return "\n".join(lines) + "\n"


def copy_license_texts(packages: Sequence[str], output_dir: Path) -> dict[str, list[str]]:
    """
    Copy distributable license texts from installed package dist-info folders
    (and Python's LICENSE.txt) into output_dir.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    copied: dict[str, list[str]] = {}

    for pkg in packages:
        try:
            dist = importlib.metadata.distribution(pkg)
        except importlib.metadata.PackageNotFoundError:
            continue

        destination = output_dir / f"{dist.metadata['Name']}-{dist.version}"
        found_names: list[str] = []
        for item in dist.files or ():
            filename = Path(str(item)).name.lower()
            if not any(word in filename for word in LICENSE_WORDS):
                continue
            source = Path(dist.locate_file(item))
            if not source.is_file():
                continue
            safe_name = "__".join(
                part for part in Path(str(item)).parts if part not in {".", ".."}
            )
            target = destination / safe_name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            found_names.append(target.name)

        license_text = dist.metadata.get("License")
        if not found_names and license_text and len(license_text.strip()) > 80:
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / "LICENSE-from-package-metadata.txt"
            target.write_text(license_text.strip() + "\n", encoding="utf-8")
            found_names.append(target.name)

        copied[f"{dist.metadata['Name']}=={dist.version}"] = sorted(set(found_names))

    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        py_dest = output_dir / f"Python-{sys.version_info.major}.{sys.version_info.minor}"
        py_dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(python_license, py_dest / "LICENSE.txt")
        copied[f"Python {sys.version_info.major}.{sys.version_info.minor}"] = ["LICENSE.txt"]

    return copied


def main(argv: Sequence[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Collect installed Python dependency license metadata from requirements.txt."
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        default=root / "requirements.txt",
        help="Path to requirements.txt (default: <repo>/requirements.txt).",
    )
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        help="Additional package name(s) to include alongside requirements.txt.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output file path for the summary (writes to stdout if omitted).",
    )
    parser.add_argument(
        "--copy-dir",
        type=Path,
        default=None,
        help="Optional directory to copy full dist-info license text files into.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output summary as JSON instead of Markdown.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any requirement is not installed in the current environment.",
    )
    args = parser.parse_args(argv)

    packages = parse_requirements(args.requirements)
    seen = {p.lower().replace("_", "-") for p in packages}
    for extra in args.extra:
        norm = extra.lower().replace("_", "-")
        if norm not in seen:
            seen.add(norm)
            packages.append(extra)

    entries = collect_metadata_summary(packages, include_python=True)
    rendered = (
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n"
        if args.as_json
        else format_markdown_summary(entries)
    )

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Wrote license summary ({len(entries)} entries) to {args.output}")
    else:
        sys.stdout.write(rendered)

    if args.copy_dir is not None:
        copied = copy_license_texts(packages, args.copy_dir)
        print(f"Copied license texts for {len(copied)} distributions into {args.copy_dir}")

    if args.strict and any(not item["installed"] for item in entries):
        missing = [item["package"] for item in entries if not item["installed"]]
        print(f"Missing installed packages: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
