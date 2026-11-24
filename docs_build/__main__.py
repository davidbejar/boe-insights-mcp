"""Command line of the reference generator: ``python -m docs_build``.

Responsibility: parse arguments, call ``docs_build.reference`` and turn a ``ReferenceBuildError``
into one line per problem on stderr and exit status 1. Run it from the repository root.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from docs_build import reference


def _parser() -> argparse.ArgumentParser:
    """The argument parser; exactly one of ``--out``, ``--hash`` and ``--refresh`` is used."""
    parser = argparse.ArgumentParser(
        prog="python -m docs_build",
        description="Generate the reference pages (tools, resources, prompts; English and "
        "Spanish) of the docs site from the server itself.",
    )
    parser.add_argument("--out", type=Path, help="directory to write the six Markdown files to")
    parser.add_argument(
        "--summaries",
        type=Path,
        default=reference.DEFAULT_SUMMARIES,
        help="the Spanish summaries file (default: docs/reference/summaries.es.toml)",
    )
    parser.add_argument(
        "--hash",
        metavar="KEY",
        help="print the current source hash of ONE entry, e.g. tools.find_codes, and exit",
    )
    parser.add_argument(
        "--refresh",
        metavar="KEY",
        help="store the current source hash of ONE entry in the summaries file. Do this only "
        "after re-reading its Spanish summary against the changed English text.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return the exit status (0 ok, 1 build problems, 2 usage)."""
    parser = _parser()
    args = parser.parse_args(argv)
    modes = [value for value in (args.out, args.hash, args.refresh) if value is not None]
    if len(modes) != 1:
        parser.error("give exactly one of --out, --hash or --refresh")
    try:
        components = reference.collect_components()
        if args.hash is not None:
            entry = next((c for c in components if c.key == args.hash), None)
            if entry is None:
                raise reference.ReferenceBuildError(
                    [f"{args.hash}: the server has no such entry; names look like tools.NAME."]
                )
            print(reference.source_hash(entry))
        elif args.refresh is not None:
            digest = reference.refresh_summary_hash(args.refresh, components, args.summaries)
            print(f"{args.refresh}: source_hash set to {digest} in {args.summaries}")
        else:
            summaries = reference.load_summaries(args.summaries)
            written = reference.write_pages(args.out, components=components, summaries=summaries)
            print(f"wrote {len(written)} files to {args.out}")
    except reference.ReferenceBuildError as error:
        for problem in error.problems:
            print(problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
