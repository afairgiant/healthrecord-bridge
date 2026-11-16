"""``hrb`` command line interface."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .ccda import CcdaError, convert_many
from .ccda.anonymize import AnonymizeReport, anonymize_file
from .ccda.synthesize import synthesize_file
from .fhir.ndjson import write_ndjson


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hrb",
        description="Convert personal health record exports into FHIR R4.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser(
        "ccda2fhir",
        help="convert HL7 C-CDA XML (bare files or MyChart XDM zips) to a FHIR R4 Bundle",
    )
    c.add_argument("inputs", metavar="INPUT", nargs="+", help=".xml or .zip input files")
    c.add_argument(
        "-o", "--output", metavar="bundle.json", help="write the Bundle here (default: stdout)"
    )
    c.add_argument(
        "--ndjson", metavar="DIR", help="also write one <ResourceType>.ndjson per type into DIR"
    )
    c.add_argument("--pretty", action="store_true", help="indent the JSON output")
    c.add_argument("--report", metavar="report.json", help="write the conversion report as JSON")
    c.add_argument(
        "--assume-timezone",
        metavar="OFFSET",
        help=(
            "UTC offset for timestamps that have a time but no offset "
            "(write negative offsets as --assume-timezone=-05:00)"
        ),
    )
    c.add_argument("-q", "--quiet", action="store_true", help="do not print the report to stderr")
    c.set_defaults(func=cmd_ccda2fhir)

    a = sub.add_parser(
        "anonymize",
        help="scrub identity and shift dates in a C-CDA XML file so it can be shared",
    )
    a.add_argument("input", metavar="INPUT", help="C-CDA XML file")
    a.add_argument("output", metavar="OUTPUT", help="where to write the scrubbed XML")
    a.add_argument(
        "--seed",
        metavar="SEED",
        help=(
            "secret seed for the day shift and id hashes (same seed, same output; "
            "keep it private; default: random)"
        ),
    )
    a.add_argument("--report", metavar="report.json", help="write the anonymize report as JSON")
    a.set_defaults(func=cmd_anonymize)

    s = sub.add_parser(
        "synthesize",
        help="anonymize a C-CDA XML file and replace its clinical content with made-up data",
    )
    s.add_argument("input", metavar="INPUT", help="C-CDA XML file")
    s.add_argument("output", metavar="OUTPUT", help="where to write the synthetic XML")
    s.add_argument(
        "--seed",
        metavar="SEED",
        help=(
            "secret seed for the anonymizer and every choice (same seed, same output; "
            "keep it private; default: random)"
        ),
    )
    s.add_argument("--report", metavar="report.json", help="write the synthesize report as JSON")
    s.set_defaults(func=cmd_synthesize)
    return parser


def cmd_ccda2fhir(args: argparse.Namespace) -> int:
    try:
        bundle, report = convert_many(args.inputs, assume_timezone=args.assume_timezone)
    except (CcdaError, FileNotFoundError, ValueError, OSError) as exc:
        print(f"hrb: error: {exc}", file=sys.stderr)
        return 1
    indent = 2 if args.pretty else None
    text = json.dumps(bundle, indent=indent, ensure_ascii=False, allow_nan=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.write("\n")
    elif not args.ndjson:
        sys.stdout.write(text)
        sys.stdout.write("\n")
    if args.ndjson:
        write_ndjson(bundle, args.ndjson)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2)
            fh.write("\n")
    if not args.quiet:
        print(report.format(), file=sys.stderr)
    return 0


def cmd_anonymize(args: argparse.Namespace) -> int:
    seed = args.seed if args.seed is not None else secrets.token_hex(8)
    try:
        report = anonymize_file(Path(args.input), Path(args.output), seed=seed)
    except (CcdaError, FileNotFoundError, ValueError, OSError) as exc:
        print(f"hrb: error: {exc}", file=sys.stderr)
        return 1
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2)
            fh.write("\n")
    print(f"{args.output}: {report.summary()}")
    _print_scrub_warnings(None if args.seed else seed, report)
    _print_residuals(report.residual_findings)
    return 0


def cmd_synthesize(args: argparse.Namespace) -> int:
    seed = args.seed if args.seed is not None else secrets.token_hex(8)
    try:
        report = synthesize_file(Path(args.input), Path(args.output), seed=seed)
    except (CcdaError, FileNotFoundError, ValueError, OSError) as exc:
        print(f"hrb: error: {exc}", file=sys.stderr)
        return 1
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2)
            fh.write("\n")
    print(f"{args.output}: {report.summary()}")
    _print_scrub_warnings(None if args.seed else seed, report.anonymize)
    if report.unrecognized_sections:
        print(
            f"hrb: warning: {len(report.unrecognized_sections)} unrecognized sections were "
            "blanked wholesale",
            file=sys.stderr,
        )
        for section in report.unrecognized_sections:
            print(
                f"  {section['code'] or '(no code)'} {section['title'] or ''} "
                f"({section['entries']} entries)",
                file=sys.stderr,
            )
    _print_residuals(report.residual_findings)
    return 0


def _print_scrub_warnings(random_seed: str | None, report: AnonymizeReport) -> None:
    """Show a random seed (so the run can be repeated) and warn about a ``nonXMLBody``."""
    if random_seed is not None:
        print(
            f"hrb: seed {random_seed} (keep it private: it reproduces this output, "
            "and with it the id hashes can be checked against guesses)",
            file=sys.stderr,
        )
    if report.non_xml_body == "file":
        print(
            "hrb: warning: the document body is a nonXMLBody (a scanned or attached file); "
            "it was replaced with a placeholder, so the output has no clinical content",
            file=sys.stderr,
        )
    elif report.non_xml_body == "text":
        print(
            "hrb: warning: the document body is a nonXMLBody of plain text; it was scrubbed "
            "like narrative, but read all of it before sharing the output",
            file=sys.stderr,
        )


def _print_residuals(findings: list[str]) -> None:
    if not findings:
        return
    print(
        f"hrb: warning: {len(findings)} residual findings; read the output before sharing it",
        file=sys.stderr,
    )
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
