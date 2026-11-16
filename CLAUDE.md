# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

healthrecord-bridge (`hrb`) converts HL7 C-CDA R2.1 documents (Epic MyChart "Download my record" exports, bare XML or IHE XDM zips) into a FHIR R4 Bundle plus NDJSON. It also has two tools for turning a real export into a shareable test fixture: `anonymize` and `synthesize`. It is pure Python 3.11+ and its only runtime dependency is `lxml`; keep it that way unless an issue discussion agreed otherwise.

## Commands

```sh
uv sync --all-extras                      # install, including dev deps
uv run ruff check . && uv run pytest      # both must pass (CI also runs ruff format --check .)
uv run ruff format .                      # fix formatting
uv run pytest tests/test_values.py::test_encounters   # a single test
uv run pytest -m slow                     # whole Synthea sample set; needs HRB_SYNTHEA_DIR, skipped otherwise
```

If `uv` is not on PATH, the project venv works directly: `.venv/Scripts/python.exe -m pytest`, `.venv/Scripts/ruff.exe check .` (Windows paths).

CI runs lint plus `pytest -m "not slow"` on Python 3.11, 3.12 and 3.13. Line length is 100.

## Architecture

**Conversion pipeline** (`ccda/__init__.py`): `convert` / `convert_many` / `convert_bytes` → `xdm.iter_documents` (zip inputs) → `CdaDocument.from_bytes` → `convert_document`, which maps the header (`header.py`: Patient, custodian Organization) and then dispatches each section → `DocumentReference` + `Provenance` → `fhir/bundle.py` merges bundles, deduplicating by `(resourceType, id)` with the later input winning.

- **Section dispatch** (`sections.py`): `SECTION_SPECS` matches a section by LOINC code *or* templateId root (with and without the `.1` suffix) and names its mapper. Sections that match nothing land in the report as unknown; `KNOWN_UNMAPPED` only supplies titles for them. A new section type means a new spec plus a mapper in `ccda/mappers/`.
- **Mappers** have the signature `(ctx: Context, section: Section, sr: SectionReport) -> list[dict]`. They return plain dicts, not model classes. Each entry they can't use is appended to `sr.skipped` with a reason; it is never silently dropped. Observation-shaped mappers (results, vitals, social history) share `mappers/_observation.py`.
- **`Context`** (`context.py`) is per-document state, and mappers should go through it rather than calling helpers directly:
  - `ctx.concept()` builds CodeableConcepts and records unmapped OIDs for the report.
  - `ctx.ts()` / `ctx.period()` / `ctx.set_effective()` handle timestamps, including truncating a time with no UTC offset unless `assume_timezone` was given.
  - `ctx.new_resource()` creates resources with deterministic ids.
  - `ctx.entry_text()` resolves `<reference value="#id"/>` into the section narrative.
- **Deterministic ids** (`ids.py`): every resource id is `uuid5(NAMESPACE_HRB, "<type>|<root>|<extension>")`, so re-importing the same export upserts instead of duplicating. When a CDA id is reused (Epic does this inside result organizers), `new_resource` adds a discriminator (section code and ordinals, or the element path). Entries without an id fall back to the document id plus caller-supplied parts. Changing any of this changes ids for existing users.
- **Untrusted XML**: always parse through `document.parse_xml` / `make_parser`, which rejects DOCTYPEs and entities and disables network access. `tests/test_security.py` covers this.
- **XML helpers** (`xml.py`): `child`, `children`, `attr`, `xsi_type`, namespace constants (`HL7_NS`, `SDTC_NS`).

**Anonymize / synthesize** (`ccda/anonymize.py`, `ccda/synthesize.py`): `synthesize` runs `anonymize` first and then replaces all clinical content from built-in tables.
- The seed is a secret. Identifier hashes, the day shift and the synthesizer's RNG are HMAC-SHA256 under per-purpose keys from `seeded_key(seed, purpose)`.
- `AnonymizeReport.to_dict()` deliberately leaves out `day_shift`.
- `SUBSTITUTES` is every string the scrubber writes; the residual check skips them, so add any new replacement text to it.
- Output must stay byte-identical for the same seed, and the converter must produce the same resources from the scrubbed file as from the original. The tests check both.

## Gotchas

- An lxml element with no children is falsy. Compare with `is None`, never `x or y`. This dropped `dischargeDispositionCode` (issue #5).
- Numbers from attributes go through `codes.finite_number` / `codes.quantity`. `NaN` and `Infinity` would make the JSON output invalid, and the writers use `allow_nan=False` as a backstop.
- `__version__` comes from package metadata. The version lives only in `pyproject.toml`, and `release.yml` fails if the pushed `v*` tag doesn't match it. Record changes under `## [Unreleased]` in `CHANGELOG.md` (Keep a Changelog format).
- The fixture XML files have CRLF line endings, so byte-level `replace` in tests must not assume `\n`.

## Tests and fixtures

- Tests import helpers with `from conftest import ...` (`HANDWRITTEN`, `SYNTHEA`, `EPIC`, `XDM_ZIP`, `NS`, `by_type`).
- For a construct the fixtures don't cover, either extend `tests/fixtures/handwritten/mychart_like_ccd.xml` or build a snippet document inline. `tests/test_result_shapes.py` and `tests/test_edge_values.py` show the snippet pattern.
- Assert specific mapped values, not just counts.
- Synthetic data only: never commit a real record, even a redacted one. Record where each new fixture came from in `tests/fixtures/README.md`.
