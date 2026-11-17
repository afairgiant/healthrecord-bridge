"""Regression tests on hand-built snippets: encounters, numbers, allergies and timestamps."""

from __future__ import annotations

import json

import pytest

from conftest import by_type
from healthrecord_bridge.ccda import convert_bytes
from healthrecord_bridge.ccda.ids import CdaId, resource_id
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
PROBLEMS = "11450-4"
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


PRECISION = "http://hl7.org/fhir/StructureDefinition/quantity-precision"


@pytest.mark.parametrize(
    ("raw", "value", "places"),
    [("1.50", 1.5, 2), ("100.00", 100.0, 2), ("0.10", 0.1, 2), ("1.5", 1.5, None), ("7", 7, None)],
)
def test_trailing_zeros_are_kept_as_precision(raw, value, places):
    (obs,) = by_type(convert(RESULTS, lab(f'<value xsi:type="PQ" value="{raw}" unit="mg/dL"/>')))[
        "Observation"
    ]
    q = obs["valueQuantity"]
    assert q["value"] == value and type(q["value"]) is type(value)
    if places is None:
        assert "_value" not in q
    else:
        assert q["_value"] == {"extension": [{"url": PRECISION, "valueInteger": places}]}


def test_real_value_keeps_precision():
    (obs,) = by_type(convert(RESULTS, lab('<value xsi:type="REAL" value="2.500"/>')))["Observation"]
    assert obs["valueQuantity"]["value"] == 2.5
    assert obs["valueQuantity"]["_value"]["extension"][0]["valueInteger"] == 3


def test_medication_period_keeps_precision():
    (med,) = by_type(convert(MEDICATIONS, medication("0.50")))["MedicationStatement"]
    repeat = med["dosage"][0]["timing"]["repeat"]
    assert repeat["period"] == 0.5
    assert repeat["_period"]["extension"][0]["valueInteger"] == 2


NKA_ALLERGY = (
    '<entry><act classCode="ACT" moodCode="EVN">'
    '<templateId root="2.16.840.1.113883.10.20.22.4.30"/><id root="9.9" extension="ACT-1"/>'
    '<code code="CONC" codeSystem="2.16.840.1.113883.5.6"/><statusCode code="active"/>'
    '<entryRelationship typeCode="SUBJ"><observation classCode="OBS" moodCode="EVN"'
    ' negationInd="true"><templateId root="2.16.840.1.113883.10.20.22.4.7"/>'
    '<id root="9.9" extension="OBS-1"/><code code="ASSERTION" codeSystem="2.16.840.1.113883.5.4"/>'
    '<statusCode code="completed"/>'
    '<value xsi:type="CD" code="{value}" codeSystem="2.16.840.1.113883.6.96"/>'
    "{substance}</observation></entryRelationship></act></entry>"
)
PENICILLIN = (
    '<participant typeCode="CSM"><participantRole classCode="MANU"><playingEntity classCode="MMAT">'
    '<code code="7980" codeSystem="2.16.840.1.113883.6.88" displayName="Penicillin G"/>'
    "</playingEntity></participantRole></participant>"
)


@pytest.mark.parametrize(
    ("value", "substance", "code", "verification"),
    [
        # A negated allergy type with no substance is a confirmed "no known allergy".
        ("419199007", "", "716186003", "confirmed"),
        ("416098002", "", "409137002", "confirmed"),
        ("414285001", "", "429625007", "confirmed"),
        # Epic writes NKDA as a negated propensity to adverse reactions to drug.
        ("419511003", "", "409137002", "confirmed"),
        ("418471000", "", "429625007", "confirmed"),
        ("420134006", "", "716186003", "confirmed"),
        # "Not allergic to dander" is not "no environmental allergies".
        ("232347008", "", "232347008", "refuted"),
        # A negated allergy to a named substance is refuted, and keeps the substance.
        ("416098002", PENICILLIN, "7980", "refuted"),
        # No standard "no known intolerance" concept: the type stays, refuted.
        ("59037007", "", "59037007", "refuted"),
    ],
)
def test_negated_allergy(value, substance, code, verification):
    entry = NKA_ALLERGY.replace("{value}", value).replace("{substance}", substance)
    (allergy,) = by_type(convert("48765-2", entry))["AllergyIntolerance"]
    assert allergy["code"]["coding"][0]["code"] == code
    assert allergy["verificationStatus"]["coding"][0]["code"] == verification
    # FHIR R4 ait-1: clinicalStatus SHALL be present unless entered-in-error.
    assert allergy["clinicalStatus"]["coding"][0]["code"] == "active"


def test_negated_allergy_to_a_substance_named_only_by_text_keeps_it():
    named = (
        '<participant typeCode="CSM"><participantRole classCode="MANU">'
        '<playingEntity classCode="MMAT"><code nullFlavor="OTH"/><name>Penicillin</name>'
        "</playingEntity></participantRole></participant>"
    )
    entry = NKA_ALLERGY.replace("{value}", "419199007").replace("{substance}", named)
    (allergy,) = by_type(convert("48765-2", entry))["AllergyIntolerance"]
    assert allergy["code"] == {"text": "Penicillin"}
    assert allergy["verificationStatus"]["coding"][0]["code"] == "refuted"


@pytest.mark.parametrize(
    ("text", "kept"),
    [("Drug allergy", False), ("No known latex allergy", True), ("NKDA", True)],
)
def test_no_known_code_keeps_only_negative_text(text, kept):
    value = (
        '<value xsi:type="CD" code="416098002" codeSystem="2.16.840.1.113883.6.96">'
        f"<originalText>{text}</originalText></value>"
    )
    entry = NKA_ALLERGY.replace(
        '<value xsi:type="CD" code="{value}" codeSystem="2.16.840.1.113883.6.96"/>', value
    ).replace("{substance}", "")
    (allergy,) = by_type(convert("48765-2", entry))["AllergyIntolerance"]
    assert allergy["code"]["coding"][0]["code"] == "409137002"
    assert allergy["code"].get("text") == (text if kept else None)


def test_ivl_real_bounds_read_as_decimals():
    value = '<value xsi:type="IVL_REAL"><low value="5"/><high value="7.50"/></value>'
    (obs,) = by_type(convert(RESULTS, lab(value)))["Observation"]
    low, high = obs["valueRange"]["low"], obs["valueRange"]["high"]
    assert low == {"value": 5.0} and type(low["value"]) is float
    assert high["value"] == 7.5 and high["_value"]["extension"][0]["valueInteger"] == 2


@pytest.mark.parametrize(
    ("raw", "value", "places"),
    [
        ("100E-2", 1.0, 2),  # exponent notation is a decimal, not the int 1
        ("1E+2", 100, None),
        ("12345678901234567.25", 1.2345678901234568e16, None),  # float lost digits: no claim
    ],
)
def test_decimal_edge_cases(raw, value, places):
    (obs,) = by_type(convert(RESULTS, lab(f'<value xsi:type="PQ" value="{raw}" unit="g"/>')))[
        "Observation"
    ]
    q = obs["valueQuantity"]
    assert q["value"] == value and type(q["value"]) is type(value)
    if places is None:
        assert "_value" not in q
    else:
        assert q["_value"]["extension"][0]["valueInteger"] == places


def test_impossible_timestamps_are_noted_in_the_report():
    data = DOCUMENT.format(section=RESULTS, entries=lab("")).replace(
        '<statusCode code="completed"/><effectiveTime value="20260301"/>',
        '<statusCode code="completed"/><effectiveTime value="20260231120000+9999"/>',
    )
    bundle, report = convert_bytes(data.encode("utf-8"), "snippet.xml")
    (obs,) = by_type(bundle)["Observation"]
    assert obs["effectiveDateTime"] == "2026-02"
    notes = report.documents[0].notes
    assert any("impossible day or UTC offset" in n for n in notes)
    assert not any("assume_timezone" in n for n in notes)


# ---- issue 11: FHIR validity -----------------------------------------------------------------


def problem(
    effective: str, status: str | None, *, act_status: str = "active", act_effective: str = ""
) -> str:
    """A Problem Concern Act; ``effective`` is the observation's effectiveTime content, or the
    whole element when it starts with ``<effectiveTime``."""
    if not effective.startswith("<effectiveTime"):
        effective = f"<effectiveTime>{effective}</effectiveTime>"
    status_obs = (
        '<entryRelationship typeCode="REFR"><observation classCode="OBS" moodCode="EVN">'
        '<templateId root="2.16.840.1.113883.10.20.22.4.6"/>'
        '<code code="33999-4" codeSystem="2.16.840.1.113883.6.1"/><statusCode code="completed"/>'
        f'<value xsi:type="CD" code="{status}" codeSystem="2.16.840.1.113883.6.96"/>'
        "</observation></entryRelationship>"
        if status
        else ""
    )
    return (
        '<entry><act classCode="ACT" moodCode="EVN">'
        '<templateId root="2.16.840.1.113883.10.20.22.4.3"/><id root="9.9" extension="ACT-1"/>'
        f'<code code="CONC" codeSystem="2.16.840.1.113883.5.6"/><statusCode code="{act_status}"/>'
        f"{act_effective}"
        '<entryRelationship typeCode="SUBJ"><observation classCode="OBS" moodCode="EVN">'
        '<templateId root="2.16.840.1.113883.10.20.22.4.4"/><id root="9.9" extension="OBS-1"/>'
        '<code code="55607006" codeSystem="2.16.840.1.113883.6.96"/>'
        f'<statusCode code="completed"/>{effective}'
        '<value xsi:type="CD" code="38341003" codeSystem="2.16.840.1.113883.6.96"/>'
        f"{status_obs}</observation></entryRelationship></act></entry>"
    )


ACTIVE, INACTIVE = "55561003", "73425007"


@pytest.mark.parametrize(
    ("effective", "status", "clinical", "abatement"),
    [
        # con-4: an abated condition cannot be active; C-CDA says high means resolved.
        ('<low value="20200101"/><high value="20230601"/>', ACTIVE, "resolved", "2023-06-01"),
        ('<low value="20200101"/><high nullFlavor="UNK"/>', ACTIVE, "resolved", None),
        ('<low value="20200101"/><high value="20230601"/>', None, "resolved", "2023-06-01"),
        # An explicit inactive status already satisfies con-4 and is kept.
        ('<low value="20200101"/><high value="20230601"/>', INACTIVE, "inactive", "2023-06-01"),
        # No high: the status stands.
        ('<low value="20200101"/>', ACTIVE, "active", None),
        ('<low value="20200101"/><high nullFlavor="NI"/>', ACTIVE, "active", None),
    ],
)
def test_condition_clinical_status_agrees_with_abatement(effective, status, clinical, abatement):
    (cond,) = by_type(convert(PROBLEMS, problem(effective, status)))["Condition"]
    assert cond["clinicalStatus"]["coding"][0]["code"] == clinical
    assert cond.get("abatementDateTime") == abatement


def test_resolution_date_outranks_the_act_status_code():
    entry = problem('<low value="20200101"/><high value="20230601"/>', None, act_status="suspended")
    (cond,) = by_type(convert(PROBLEMS, entry))["Condition"]
    assert cond["clinicalStatus"]["coding"][0]["code"] == "resolved"


@pytest.mark.parametrize("act_high", ['<high nullFlavor="UNK"/>', '<high value="20240101"/>'])
def test_concern_act_end_is_not_the_problem_resolution(act_high):
    entry = problem(
        '<effectiveTime nullFlavor="UNK"/>',
        ACTIVE,
        act_effective=f'<effectiveTime><low value="20200101"/>{act_high}</effectiveTime>',
    )
    (cond,) = by_type(convert(PROBLEMS, entry))["Condition"]
    assert cond["clinicalStatus"]["coding"][0]["code"] == "active"
    assert cond["onsetDateTime"] == "2020-01-01"  # the act still supplies the onset
    assert "abatementDateTime" not in cond


@pytest.mark.parametrize(
    ("interpretation", "expected"),
    [
        ('code="N" codeSystem="2.16.840.1.113883.5.83"', [{"text": "Normal"}]),
        ('code="HH" codeSystem="2.16.840.1.113883.5.83"', [{"text": "Critical high"}]),
        ('code="N" codeSystem="2.16.840.1.113883.5.83" displayName="Nml"', [{"text": "Nml"}]),
        # No label to give: ref-1 needs low, high or text, and "" is not a FHIR string.
        ('code="N" codeSystem="1.2.3.4.5"', None),
    ],
)
def test_reference_range_with_only_an_interpretation(interpretation, expected):
    entry = lab(
        '<value xsi:type="PQ" value="5" unit="mg/dL"/><referenceRange><observationRange>'
        f"<interpretationCode {interpretation}/></observationRange></referenceRange>"
    )
    (obs,) = by_type(convert(RESULTS, entry))["Observation"]
    assert obs.get("referenceRange") == expected


def without_effective_time(author: str = "", effective: str = "") -> bytes:
    data = DOCUMENT.format(section=RESULTS, entries=lab(""))
    line = '  <effectiveTime value="20260301"/>\n'
    return data.replace(line, effective + author, 1).encode("utf-8")


def header_author(time: str, root: str = "9.9") -> str:
    return (
        f'<author><time value="{time}"/>'
        f'<assignedAuthor><id root="{root}"/></assignedAuthor></author>'
    )


def test_document_time_falls_back_to_the_latest_author_time():
    # As text "20250315" sorts last, but 23:00 at -05:00 is 04:00 UTC on the 15th.
    author = header_author("20250315") + header_author("20250314230000-0500", "9.8")
    bundle, _ = convert_bytes(without_effective_time(author), "snippet.xml")
    resources = by_type(bundle)
    (prov,) = resources["Provenance"]
    (doc_ref,) = resources["DocumentReference"]
    assert prov["recorded"] == doc_ref["date"] == "2025-03-14T23:00:00-05:00"


def test_provenance_is_not_invented_without_any_time():
    bundle, report = convert_bytes(without_effective_time(), "snippet.xml")
    assert "Provenance" not in by_type(bundle)
    assert "1970" not in json.dumps(bundle)
    assert any("no usable effectiveTime" in n for n in report.documents[0].notes)


@pytest.mark.parametrize(
    ("effective", "author", "expected"),
    [
        # A leap second is a valid instant; comparing it must not crash.
        ("", "20250314235960-0500", "2025-03-14T23:59:60-05:00"),
        # An impossible effectiveTime is not padded into an instant; the author time is real.
        (
            '  <effectiveTime value="20240231"/>\n',
            "20240229103000-0500",
            "2024-02-29T10:30:00-05:00",
        ),
        ('  <effectiveTime value="20250314230000+9999"/>\n', "20250101", "2025-01-01T00:00:00Z"),
    ],
)
def test_document_time_edge_cases(effective, author, expected):
    data = without_effective_time(header_author(author), effective)
    bundle, _ = convert_bytes(data, "snippet.xml")
    (prov,) = by_type(bundle)["Provenance"]
    assert prov["recorded"] == expected


def concern_without_observation_ids(*values: str) -> str:
    observations = "".join(
        '<entryRelationship typeCode="SUBJ"><observation classCode="OBS" moodCode="EVN">'
        '<templateId root="2.16.840.1.113883.10.20.22.4.4"/>'
        '<code code="55607006" codeSystem="2.16.840.1.113883.6.96"/>'
        '<statusCode code="completed"/><effectiveTime><low value="20200101"/></effectiveTime>'
        f'<value xsi:type="CD" code="{v}" codeSystem="2.16.840.1.113883.6.96"/>'
        "</observation></entryRelationship>"
        for v in values
    )
    return (
        '<entry><act classCode="ACT" moodCode="EVN">'
        '<templateId root="2.16.840.1.113883.10.20.22.4.3"/><id root="9.9" extension="ACT-1"/>'
        '<code code="CONC" codeSystem="2.16.840.1.113883.5.6"/><statusCode code="active"/>'
        f"{observations}</act></entry>"
    )


def test_concern_act_id_shared_by_its_observations_does_not_depend_on_order():
    def ids(*values):
        conditions = by_type(convert(PROBLEMS, concern_without_observation_ids(*values)))
        return {c["code"]["coding"][0]["code"]: c["id"] for c in conditions["Condition"]}

    act = CdaId("9.9", "ACT-1")
    both = ids("38341003", "44054006")
    assert both == {
        code: resource_id("Condition", act, f"http://snomed.info/sct|{code}", "20200101")
        for code in ("38341003", "44054006")
    }
    assert both == ids("44054006", "38341003")
    # A single observation still takes the act's plain id.
    (alone,) = ids("38341003").values()
    assert alone == resource_id("Condition", act)
