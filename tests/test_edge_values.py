"""Regression tests on hand-built snippets: discharge disposition and non-finite numbers."""

from __future__ import annotations

import json

import pytest

from conftest import by_type
from healthrecord_bridge.ccda import convert_bytes
from healthrecord_bridge.fhir.ndjson import write_ndjson

DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:sdtc="urn:hl7-org:sdtc"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <templateId root="2.16.840.1.113883.10.20.22.1.2"/>
  <id root="1.2.3.4" extension="DOC-1"/>
  <code code="34133-9" codeSystem="2.16.840.1.113883.6.1"/>
  <title>Snippet</title>
  <effectiveTime value="20260301"/>
  <recordTarget>
    <patientRole>
      <id root="1.2.3.4.5" extension="P-1"/>
      <patient>
        <name><given>Alex</given><family>Testperson</family></name>
        <administrativeGenderCode code="F" codeSystem="2.16.840.1.113883.5.1"/>
        <birthTime value="19800101"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component><structuredBody><component>
    <section>
      <code code="{section}" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Snippet section</title>
      {entries}
    </section>
  </component></structuredBody></component>
</ClinicalDocument>
"""

ENCOUNTERS = "46240-8"
RESULTS = "30954-2"
MEDICATIONS = "10160-0"
DISCHARGED_HOME = 'code="01" codeSystem="2.16.840.1.113883.12.112" displayName="Discharged home"'


def convert(section: str, entries: str) -> dict:
    data = DOCUMENT.format(section=section, entries=entries).encode("utf-8")
    bundle, _ = convert_bytes(data, "snippet.xml")
    return bundle


def encounter(disposition: str) -> str:
    return (
        '<entry><encounter classCode="ENC" moodCode="EVN">'
        '<id root="9.9" extension="ENC-1"/>'
        '<code code="99213" codeSystem="2.16.840.1.113883.6.12"/>'
        f'<effectiveTime value="20260301"/>{disposition}</encounter></entry>'
    )


@pytest.mark.parametrize("prefix", ["", "sdtc:"])
def test_attribute_only_discharge_disposition_is_kept(prefix):
    el = f"<{prefix}dischargeDispositionCode {DISCHARGED_HOME}/>"
    (enc,) = by_type(convert(ENCOUNTERS, encounter(el)))["Encounter"]
    coding = enc["hospitalization"]["dischargeDisposition"]["coding"][0]
    assert coding["code"] == "01"
    assert coding["display"] == "Discharged home"


def lab(value: str) -> str:
    return (
        '<entry><observation classCode="OBS" moodCode="EVN">'
        '<id root="9.9" extension="OBS-1"/>'
        '<code code="2345-7" codeSystem="2.16.840.1.113883.6.1" displayName="Glucose"/>'
        '<statusCode code="completed"/><effectiveTime value="20260301"/>'
        f"{value}</observation></entry>"
    )


NON_FINITE = ["NaN", "nan", "Infinity", "-inf", "1e400"]


@pytest.mark.parametrize("raw", NON_FINITE)
@pytest.mark.parametrize("kind", ["PQ", "REAL"])
def test_non_finite_value_is_not_a_quantity(kind, raw):
    unit = ' unit="mg/dL"' if kind == "PQ" else ""
    (obs,) = by_type(convert(RESULTS, lab(f'<value xsi:type="{kind}" value="{raw}"{unit}/>')))[
        "Observation"
    ]
    assert "valueQuantity" not in obs
    assert obs["valueString"] == raw


@pytest.mark.parametrize("raw", ["1.5", "100", "-0.25"])
def test_finite_real_value_is_still_a_quantity(raw):
    (obs,) = by_type(convert(RESULTS, lab(f'<value xsi:type="REAL" value="{raw}"/>')))[
        "Observation"
    ]
    assert obs["valueQuantity"] == {"value": float(raw)}


def medication(period: str) -> str:
    return (
        '<entry><substanceAdministration classCode="SBADM" moodCode="EVN">'
        '<id root="9.9" extension="MED-1"/><statusCode code="active"/>'
        '<effectiveTime xsi:type="IVL_TS"><low value="20260301"/></effectiveTime>'
        '<effectiveTime xsi:type="PIVL_TS" institutionSpecified="true" operator="A">'
        f'<period value="{period}" unit="h"/></effectiveTime>'
        "<consumable><manufacturedProduct><manufacturedMaterial>"
        '<code code="197361" codeSystem="2.16.840.1.113883.6.88" displayName="Amlodipine"/>'
        "</manufacturedMaterial></manufacturedProduct></consumable>"
        "</substanceAdministration></entry>"
    )


@pytest.mark.parametrize(("raw", "expected"), [("12", 12), ("0.5", 0.5), ("NaN", None)])
def test_medication_period_rejects_non_finite(raw, expected):
    (med,) = by_type(convert(MEDICATIONS, medication(raw)))["MedicationStatement"]
    repeat = med["dosage"][0].get("timing", {}).get("repeat", {}) if "dosage" in med else {}
    assert repeat.get("period") == expected


def test_writers_refuse_non_finite_numbers(tmp_path):
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": {"resourceType": "Observation", "valueQuantity": {"value": float("nan")}}}
        ],
    }
    with pytest.raises(ValueError):
        write_ndjson(bundle, tmp_path)
    nan_bundle = convert(RESULTS, lab('<value xsi:type="PQ" value="NaN" unit="mg/dL"/>'))
    json.dumps(nan_bundle, allow_nan=False)  # the converter output itself is valid JSON
