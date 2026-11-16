# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Patient: the US Core `birthsex` extension is no longer derived from
  `administrativeGenderCode`, which is a different data element. It comes from the Birth
  Sex Observation (template `2.16.840.1.113883.10.20.22.4.200`, LOINC `76689-9`) when the
  document has one (`M`, `F`, or `UNK` for a null value) and is left out otherwise.
- AllergyIntolerance: a "no known allergy" entry (a negated allergy observation with no
  substance) is now the negated statement FHIR and US Core expect: a SNOMED code with
  `verificationStatus` `confirmed`, instead of the allergy type ("Food allergy") marked
  `refuted`. Allergy to substance and propensity to adverse reactions (to substance)
  become "No known allergy" (`716186003`); drug allergy and propensity to adverse
  reactions to drug become "No known drug allergy" (`409137002`); the food equivalents
  become "No known food allergy" (`429625007`). Source text is kept on the code only when
  it reads as a negation ("No known latex allergy"). Everything else negated stays
  `refuted` with its own code: a named substance (by code or by name only), a dander
  allergy and intolerances.
- Anonymizer and synthesizer: identifier hashes, the day shift and the synthesizer's random
  choices are now HMAC-SHA256 keyed from the seed, one key per purpose, instead of
  `sha256(seed + value)`. A short identifier such as an MRN can no longer be recovered by
  hashing guesses unless the seed is known. The same seed still gives the same output, but
  no longer the same output as 0.2.1.
- The anonymize report (`--report`, `AnonymizeReport.to_dict()`, and the `anonymize` part of
  the synthesize report) no longer contains `day_shift`, which undid the date shift for
  anyone holding the report. `AnonymizeReport.day_shift` is still set in Python. The
  terminal summary no longer prints it either.
- `hrb anonymize` and `hrb synthesize` print a random seed to stderr with a reminder to
  keep it private, and no longer echo a seed passed with `--seed`. The README treats the
  seed as a secret.

### Added

- Anonymize report fields `dates_in_text_shifted`, `binary_payloads_removed` and
  `non_xml_body` (`null`, `"file"` when an embedded-file body was replaced, or `"text"`).

### Fixed

- Anonymizer: year-only and year-month timestamps (`<birthTime value="1985"/>`,
  `<effectiveTime value="201905"/>`) kept their real values. They are now shifted: a
  year-month value from the 15th of its month, a year-only value by the shift in whole
  years, at least one. Numbers that look like years (PQ, INT and range values) are left
  alone.
- Anonymizer: dates written in text and in `displayName` were not shifted. Day, month-year
  and (after words such as "in" or "diagnosed") bare-year dates in common US, ISO and
  month-name formats now move by the day shift in their own format; dates labelled as a
  birth date are rounded to January 1 like `birthTime`; a date that only makes sense
  day-first is reported as a residual finding.
- Anonymizer: a full 8-digit value on a numeric element (`INT`, `PQ` with a unit) was
  shifted as if it were a date.
- Anonymizer: phone numbers, email addresses, MRNs and NPIs were only scrubbed in section
  narrative, titles and `originalText`, and the residual check only looked there. Both now
  cover every text node, so an `ST` result value or a `desc` is scrubbed and checked too.
- Anonymizer and synthesizer: embedded files (`nonXMLBody`, `observationMedia`, base64
  `ED` values) passed through untouched and could hold a whole unredacted document. Any
  element with `representation="B64"` or a non-text `mediaType` now has all of its content
  replaced with a placeholder, including data after a `reference` child (thumbnails and
  `compression`/`integrityCheck` are dropped), and the CLI warns when the document body is
  a `nonXMLBody`, saying whether it was removed or scrubbed as text.
- Quantities, `REAL` values and medication timing periods lost significant trailing zeros
  (`1.50` became `1.5`). The number is still a plain JSON number, and the FHIR
  `quantity-precision` extension (on `_value` / `_period`) now records the source's
  decimal places when they would otherwise be lost and the number still holds the source
  value exactly. Exponent notation (`100E-2`) is read as a decimal, and `IVL_REAL` bounds
  read as decimals like `REAL`.
- Timestamps with an impossible day (`20240231`, February 29 outside a leap year) or an
  offset FHIR does not allow (beyond 13:59 other than exactly 14:00, or minutes over 59,
  such as `+9999` or `+1430`) produced invalid FHIR dateTimes. They are now kept only at
  the precision before the impossible part (`2024-02`, or the date without its time), and
  the conversion report counts them. `assume_timezone` rejects such offsets.
- `hrb --version` and `healthrecord_bridge.__version__` read the installed package version
  instead of a hardcoded string that had fallen behind (0.2.1 reported 0.2.0). The release
  workflow now fails when the pushed tag does not match the `pyproject.toml` version.
- Encounters: an attribute-only `dischargeDispositionCode` in the HL7 namespace was always
  dropped (lxml elements without children are falsy), so
  `Encounter.hospitalization.dischargeDisposition` was never filled from it.
- `NaN`, `Infinity` and overflowing values such as `1e400` in PQ and REAL values and
  medication periods are no longer written as numbers, which made the bundle and NDJSON
  invalid JSON. They are kept as `valueString`, and both writers now refuse non-finite
  numbers.

## [0.2.1] - 2026-11-14

### Fixed

- Narrative references: the text behind a `<reference value="#id"/>` is read once per id
  and shared by every entry that points at it. A document whose entries all referenced one
  large paragraph took time and memory proportional to entries × paragraph size.
- `system_uri`: an HL7 v2 table OID whose table number is a non-ASCII digit (such as `²`,
  which `str.isdigit` accepts and `int` rejects) is returned as `urn:oid:` instead of
  raising `ValueError`.

## [0.2.0] - 2026-11-03

### Added

- C-CDA synthesizer (`hrb synthesize`, `healthrecord_bridge.ccda.synthesize`): runs the
  anonymizer, then replaces every medication, problem, allergy, immunization, procedure,
  lab result, vital sign value, social history value and free-text field with made-up data
  from built-in tables, deterministically from a seed. Narratives are rebuilt as plain
  tables that keep the `ID` attributes entries point at; text-only and unrecognised
  sections are blanked. `SynthesizeReport` carries the anonymize report, entry counts per
  section, unrecognized sections and residual findings.
- Synthesizer: race, ethnicity, religion, marital status and a code-less gender are
  neutralized, the birth year is drawn 25 to 70 years before the latest date in the
  document, and provider specialty `originalText` and display names become "Removed".
- Synthesizer: Epic's `X-CE-PFD` patient-facing note section is blanked without being
  reported as unrecognized.

### Changed

- Anonymizer: names, addresses and telecoms with a `nullFlavor` but content are scrubbed
  like any other, keeping the `nullFlavor`.
- Anonymizer: Epic customer OIDs (`1.2.840.114350.1.13.<customer>`) get customer number `0`
  in every attribute and text node; the count is reported as `oids_rewritten`.
- Anonymizer: `assigningAuthorityName` values other than `EPIC`/`EPC` become `Example`.
- Anonymizer: runs of nine or more digits in OID roots and narrative `ID` attributes are
  hashed, with the references that point at them updated.
- Anonymizer: `NPI: 1234567890` patterns in narrative are blanked, and header name tokens are
  replaced in every text node of the document, not only in narrative.

### Fixed

- Converter: a CDA id reused on several elements of one document (Epic puts an organizer's id
  on a component observation and shares ids between result observations) no longer collapses
  them into one resource. The first element keeps the plain id; later ones add the section
  code and ordinals, so ids stay deterministic and `DiagnosticReport.result` references point
  at the right observation.
- Converter: `ED` result values that reference the narrative become `valueString`, or
  `dataAbsentReason` `unknown` when the referenced block is empty.
- Converter: `CodeableConcept.text` is always set from `originalText`, so a lab coded only by
  an Epic-local translation under a null LOINC code keeps its narrative name.
- Converter: LOINC 85847-2 is listed as "Patient Care team information".
- Synthesizer: a translation takes the same table row as its primary code (ICD-10-CM for
  conditions, SNOMED CT for CPT procedures) or a hashed local code, instead of a code from
  another table; the free-text sig code is kept; narrative IDs with no cell of their own go on
  a `content` element that holds the entry's name rather than an empty one.
- Anonymizer and synthesizer residual checks no longer report the scrubber's own
  substitutes (Alex, Testperson, Example Health System, ...), so a fully scrubbed document
  reports zero findings.
- Anonymizer: words of the provider organization name were treated as patient name parts,
  which turned "Patient Health Summary" into "Patient Alex Summary".
- Anonymizer: the phone pattern no longer matches digit groups inside OIDs in narrative.

## [0.1.0] - 2026-11-02

### Added

- C-CDA R2.1 to FHIR R4 converter (`hrb ccda2fhir`, `healthrecord_bridge.ccda.convert`).
- Input: bare C-CDA XML, IHE XDM zips as produced by Epic MyChart, plain zips containing
  C-CDA XML. Hardened XML parsing for untrusted archives.
- Header mapping to `Patient`, `Organization`, `DocumentReference` and `Provenance`.
- Section mapping for Allergies, Medications, Problems, Procedures, Encounters, Vital Signs,
  Results, Immunizations and Social History.
- Deterministic UUIDv5 resource ids, CDA identifiers carried as `identifier`, `meta.source`
  and `meta.tag` on every resource.
- `ConversionReport` with per-section entry, resource and skip counts, unmapped sections
  and unmapped code systems.
- NDJSON writer (one file per resource type) and a merged-bundle mode for several inputs.
- C-CDA anonymizer (`hrb anonymize`, `healthrecord_bridge.ccda.anonymize`): seeded day
  shift, hashed identifiers, substituted names, addresses and telecoms, narrative scrubbing
  and an `AnonymizeReport` with residual findings for a human reviewer.
