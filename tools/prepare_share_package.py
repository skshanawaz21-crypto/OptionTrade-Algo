from __future__ import annotations

import argparse
import fnmatch
import zipfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIST = ROOT / "dist"

EXCLUDE_PATTERNS = [
    ".env",
    "access_token.txt",
    "fyers_access_token.txt",
    "logs.txt",
    "dashboard_stdout.log",
    "dashboard_stderr.log",
    "python-*.exe",
    ".git/**",
    ".venv/**",
    "venv/**",
    "__pycache__/**",
    "**/__pycache__/**",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    ".pytest_cache/**",
    "dist/**",
    "data/**",
]

FORBIDDEN_PACKAGE_PATHS = {
    ".env",
    "access_token.txt",
    "fyers_access_token.txt",
    "logs.txt",
    "dashboard_stdout.log",
    "dashboard_stderr.log",
    "data/paper_state.json",
    "data/engine_commands.jsonl",
}

EMPTY_RUNTIME_DIRS = [
    "data/",
    "data/candles/",
    "data/log_archive/",
]


def _relative_posix(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _is_excluded(relative_path: str) -> bool:
    return any(fnmatch.fnmatchcase(relative_path, pattern) for pattern in EXCLUDE_PATTERNS)


def collect_package_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = _relative_posix(path)
        if _is_excluded(relative):
            continue
        files.append(path)
    return sorted(files, key=_relative_posix)


def build_package(output: Path, dry_run: bool = False) -> list[str]:
    files = collect_package_files()
    relative_files = [_relative_posix(path) for path in files]
    forbidden = sorted(FORBIDDEN_PACKAGE_PATHS.intersection(relative_files))
    if forbidden:
        joined = ", ".join(forbidden)
        raise RuntimeError(f"Refusing to package private runtime files: {joined}")

    if dry_run:
        return relative_files

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for directory in EMPTY_RUNTIME_DIRS:
            archive.writestr(directory, "")
        for path, relative in zip(files, relative_files):
            archive.write(path, relative)
    return relative_files


def default_output_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_DIST / f"OptionTrader-share-{stamp}.zip"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a clean OptionTrader zip for sharing. The package keeps code, "
            "strategy config, docs, and tests, but excludes local secrets, tokens, "
            "paper-trade state, logs, and cached candles."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output_path(),
        help="Destination zip path. Defaults to dist/OptionTrader-share-<timestamp>.zip.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the file list without creating a zip.",
    )
    args = parser.parse_args()

    files = build_package(args.output, dry_run=args.dry_run)
    if args.dry_run:
        print("Files that would be included:")
        for relative in files:
            print(relative)
        print(f"Total files: {len(files)}")
        return 0

    print(f"Created {args.output}")
    print(f"Included files: {len(files)}")
    print("Excluded local secrets, tokens, paper state, logs, and cached candles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
