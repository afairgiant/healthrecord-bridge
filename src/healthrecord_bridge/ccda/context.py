"""Shared state and helpers handed to every section mapper."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from lxml import etree

from . import codes
from .datetime_utils import ParsedTs, parse_ts
from .document import CdaDocument
from .ids import CdaId, content_id, fallback_id, identifier, resource_id
from .narrative import referenced_text
from .report import DocumentReport
from .xml import attr, child, children, text_of, xsi_type

HRB_TAG_SYSTEM = "urn:healthrecord-bridge"


@dataclass(slots=True)
class Context:
    """Everything a mapper needs: the document, the patient reference and the report."""

    doc: CdaDocument
    report: DocumentReport
    assume_tz: str | None = None
    patient_ref: dict = field(default_factory=dict)
    unmapped: set[str] = field(default_factory=set)
    truncated_timestamps: int = 0
    invalid_timestamps: int = 0
    claims: list[_Claim] = field(default_factory=list)
    """Every resource made by ``new_resource``, for ``assign_ids`` to give its final id."""

    # ---- codes and text ---------------------------------------------------------

    def concept(self, el: etree._Element | None) -> dict | None:
        return codes.codeable_concept(el, self.unmapped, self.doc.narrative.resolve)

    def system(self, oid: str | None) -> str | None:
        return codes.system_uri(oid, self.unmapped)

    def entry_text(self, el: etree._Element | None) -> str | None:
        """Narrative text for an entry's ``<text>`` child."""
        return referenced_text(child(el, "text"), self.doc.narrative)

    # ---- time -------------------------------------------------------------------

    def parse(self, raw: str | None) -> ParsedTs | None:
        p = parse_ts(raw, self.assume_tz)
        if p is not None and p.truncated:
            self.truncated_timestamps += 1
        if p is not None and p.invalid:
            self.invalid_timestamps += 1
        return p

    def ts(self, raw: str | None) -> str | None:
        p = self.parse(raw)
        return p.value if p else None

    def ts_value(self, el: etree._Element | None) -> str | None:
        """FHIR dateTime for a ``<effectiveTime value=".."/>`` style element."""
        if el is None or attr(el, "nullFlavor"):
            return None
        return self.ts(attr(el, "value"))

    def period(self, el: etree._Element | None) -> dict | None:
        """FHIR Period from an IVL_TS element with ``low``/``high`` children."""
        if el is None:
            return None
        out: dict = {}
        low = self.ts_value(child(el, "low"))
        high = self.ts_value(child(el, "high"))
        if low:
            out["start"] = low
        if high:
            out["end"] = high
        return out or None

    def effective(self, el: etree._Element | None) -> tuple[str, str | dict] | None:
        """Interpret an effectiveTime as ``("dateTime", str)`` or ``("period", dict)``.

        A point in time (``value``) wins; otherwise ``low``/``high``; when only
        a ``center`` exists it is used as the point in time. PIVL/EIVL timing
        elements are not effective times and return ``None``.
        """
        if el is None or attr(el, "nullFlavor"):
            return None
        if xsi_type(el) in {"PIVL_TS", "EIVL_TS", "SXPR_TS"}:
            return None
        point = self.ts_value(el)
        if point:
            return ("dateTime", point)
        period = self.period(el)
        if period:
            return ("period", period)
        center = self.ts_value(child(el, "center"))
        if center:
            return ("dateTime", center)
        return None

    def set_effective(
        self, resource: dict, el: etree._Element | None, point_key: str, period_key: str
    ) -> None:
        eff = self.effective(el)
        if eff is None:
            return
        kind, value = eff
        resource[point_key if kind == "dateTime" else period_key] = value

    # ---- ids and resource skeletons ---------------------------------------------

    def ids_of(self, el: etree._Element | None) -> list[CdaId]:
        out: list[CdaId] = []
        for id_el in children(el, "id"):
            if attr(id_el, "nullFlavor"):
                continue
            root = attr(id_el, "root")
            if root:
                out.append(CdaId(root, attr(id_el, "extension")))
        return out

    def new_resource(
        self,
        resource_type: str,
        el: etree._Element | None,
        section_code: str | None,
        *,
        what: dict | None,
        when: etree._Element | None = None,
    ) -> dict:
        """Skeleton with ``identifier`` and ``meta``, and a provisional ``id``.

        The resource's content is recorded for ``assign_ids``: ``what`` (the concept
        that names the entry, or ``None``), ``when`` (by default the element's
        ``effectiveTime``) and the element's ``value``. Build references with
        ``reference()`` as usual; ``assign_ids`` rewrites them.
        """
        cda_ids = self.ids_of(el)
        content = (
            _concept_key(what),
            _time_key(when if when is not None else child(el, "effectiveTime")),
            _value_key(child(el, "value")),
        )
        resource: dict = {
            "resourceType": resource_type,
            "id": f"{_PENDING}{len(self.claims)}",
            "meta": self.meta(),
        }
        if cda_ids:
            resource["identifier"] = [identifier(c) for c in cda_ids]
        cda_id = cda_ids[0] if cda_ids else None
        self.claims.append(_Claim(resource, cda_id, section_code or "", content))
        return resource

    def assign_ids(self) -> None:
        """Give every resource from ``new_resource`` its final id and rewrite references.

        Call it once the sections are mapped, before anything else references them. Two
        resources can end up with one id (the same entry repeated); merge them with
        ``fhir.bundle.dedupe``.

        The ids are worked out from everything the document produced, so none depends on
        an entry's position:

        - A CDA id that only one resource of a type claims, or that every claim makes with
          the same content, is the plain ``resource_id``. Identical claims are the same
          entry repeated (a problem inside an encounter diagnosis) and become one resource.
        - A CDA id that claims with different content share (Epic puts one id on several
          lab results; an act's id stands in for each of its observations) adds each
          claim's code and time, and its value only when code and time are the same.
        - Without a CDA id the key is the patient, section, code, time and value, so the
          same entry in another document or export gets the same id. An entry with no code
          and no time has nothing to be recognised by and stays within its document.
        """
        contents: defaultdict[tuple[str, CdaId], set[tuple[str, str, str]]] = defaultdict(set)
        for c in self.claims:
            if c.cda_id is not None:
                contents[(c.type, c.cda_id)].add(c.content)
        patient = self.patient_ref.get("reference", "")
        blank: Counter[tuple[str, str]] = Counter()
        renamed: dict[str, str] = {}
        for c in self.claims:
            what, when, value = c.content
            if c.cda_id is not None:
                shared = contents[(c.type, c.cda_id)]
                same_code_and_time = [k for k in shared if k[:2] == (what, when)]
                if len(shared) == 1:
                    rid = resource_id(c.type, c.cda_id)
                elif len(same_code_and_time) == 1:
                    rid = resource_id(c.type, c.cda_id, what, when)
                else:
                    rid = resource_id(c.type, c.cda_id, what, when, value)
            elif what or when:
                rid = content_id(c.type, patient, c.section, what, when, value)
            else:
                blank[(c.type, c.section)] += 1
                rid = fallback_id(c.type, self.doc.id, c.section, f"#{blank[(c.type, c.section)]}")
            renamed[f"{c.type}/{c.resource['id']}"] = f"{c.type}/{rid}"
            c.resource["id"] = rid
        # Only mapped resources can hold a provisional reference.
        _rewrite_references([c.resource for c in self.claims], renamed)

    def meta(self) -> dict:
        return {
            "source": f"urn:healthrecord-bridge:ccda:{self.doc.id}",
            "tag": [{"system": HRB_TAG_SYSTEM, "code": "ccda"}],
        }

    def reference(self, resource: dict, display: str | None = None) -> dict:
        ref: dict = {"reference": f"{resource['resourceType']}/{resource['id']}"}
        if display:
            ref["display"] = display
        return ref


_PENDING = "hrb-pending-"


@dataclass(slots=True)
class _Claim:
    resource: dict
    cda_id: CdaId | None
    section: str
    content: tuple[str, str, str]
    """Code, time and value keys."""

    @property
    def type(self) -> str:
        return str(self.resource["resourceType"])


def _rewrite_references(node: object, renamed: dict[str, str]) -> None:
    """Point references at final ids; a list of references keeps each target once."""
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str) and ref in renamed:
            node["reference"] = renamed[ref]
        for v in node.values():
            _rewrite_references(v, renamed)
    elif isinstance(node, list):
        for v in node:
            _rewrite_references(v, renamed)
        if node and all(isinstance(v, dict) and "reference" in v for v in node):
            # A repeated entry now shares one id; keep one reference to it.
            node[:] = list({(v["reference"], v.get("display")): v for v in node}.values())


def _concept_key(cc: dict | None) -> str:
    """``system|code`` of the first coding with a code, else the concept's text."""
    if not cc:
        return ""
    for c in cc.get("coding", []):
        if c.get("code"):
            return f"{c.get('system', '')}|{c['code']}"
    return " ".join(str(cc.get("text") or "").split()).casefold()


def _value_key(el: etree._Element | None) -> str:
    """A result value as written: ``system|code``, ``value unit``, a range, or its text."""
    if el is None or attr(el, "nullFlavor"):
        return ""
    if code := attr(el, "code"):
        return f"{attr(el, 'codeSystem') or ''}|{code}"
    if (number := attr(el, "value")) is not None:
        return f"{number} {attr(el, 'unit') or ''}".strip()
    bounds = [_value_key(child(el, b)) for b in ("low", "high")]
    if any(bounds):
        return "..".join(bounds)
    return (text_of(el) or "").casefold()


def _time_key(el: etree._Element | None) -> str:
    """The raw TS of a point in time, else of its ``low``, else of its ``center``."""
    for e in (el, child(el, "low"), child(el, "center")):
        if v := attr(e, "value"):
            return v
    return ""


def status_code(el: etree._Element | None) -> str | None:
    """``statusCode/@code`` lowercased, or None."""
    sc = child(el, "statusCode")
    code = attr(sc, "code")
    return code.lower() if code else None


def negated(el: etree._Element | None) -> bool:
    return (attr(el, "negationInd") or "").lower() == "true"


def mood(el: etree._Element | None) -> str:
    return (attr(el, "moodCode") or "EVN").upper()
