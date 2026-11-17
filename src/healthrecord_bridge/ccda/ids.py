"""Deterministic FHIR resource ids and identifiers derived from CDA ``id`` elements.

Every resource id is a UUIDv5 under a fixed namespace, so converting the same
export twice yields the same ids and downstream importers can upsert.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

NAMESPACE_HRB = uuid.uuid5(uuid.NAMESPACE_DNS, "healthrecord-bridge")
"""Fixed namespace for all healthrecord-bridge generated ids."""

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_OID_RE = re.compile(r"^[0-2](\.(0|[1-9][0-9]*))+$")


@dataclass(frozen=True, slots=True)
class CdaId:
    """A CDA instance identifier (``<id root=".." extension=".."/>``)."""

    root: str
    extension: str | None = None

    def __str__(self) -> str:
        return f"{self.root}:{self.extension}" if self.extension else self.root

    def as_uri(self) -> str:
        """Return the identifier as a URN (``urn:oid:`` or ``urn:uuid:``)."""
        return f"{_urn_prefix(self.root)}{self.root}"


def _urn_prefix(root: str) -> str:
    if _UUID_RE.match(root):
        return "urn:uuid:"
    if _OID_RE.match(root):
        return "urn:oid:"
    return ""


def resource_id(resource_type: str, cda_id: CdaId, *discriminator: str) -> str:
    """Deterministic id: ``uuid5(NAMESPACE_HRB, "<resourceType>|<root>|<extension>")``.

    ``discriminator`` parts (the entry's code and time) are appended after an ``@``
    marker when a document puts one CDA id on several elements, so each of them gets
    a stable id of its own that does not depend on their order.
    """
    key = f"{resource_type}|{cda_id.root}|{cda_id.extension or ''}"
    if discriminator:
        key += "|@" + "|".join(discriminator)
    return str(uuid.uuid5(NAMESPACE_HRB, key))


def content_id(resource_type: str, patient: str, section: str, *content: str) -> str:
    """Deterministic id for a section entry that carries no ``id`` of its own.

    The key is the patient, the section and the entry's content (its code and time),
    not the document, so the same entry in another document or a newer export gets
    the same id.
    """
    tail = "|".join(content)
    return str(uuid.uuid5(NAMESPACE_HRB, f"{resource_type}|#content|{patient}|{section}|{tail}"))


def fallback_id(resource_type: str, document_id: CdaId, *parts: str) -> str:
    """Document-scoped id: for a header resource (Patient, custodian) without an ``id``,
    and for a section entry with nothing else to key on (no id, code or time).

    The key is the document id followed by caller-supplied parts.
    """
    tail = "|".join(parts)
    key = f"{resource_type}|{document_id.root}|{document_id.extension or ''}|{tail}"
    return str(uuid.uuid5(NAMESPACE_HRB, key))


def identifier(cda_id: CdaId) -> dict:
    """FHIR ``Identifier`` for a CDA id.

    With an extension the root is the system (``urn:oid:...`` or ``urn:uuid:...``)
    and the extension the value. Without an extension the root itself is the
    value and the system is ``urn:ietf:rfc:3986``.
    """
    if cda_id.extension:
        system = cda_id.as_uri() if _urn_prefix(cda_id.root) else cda_id.root
        return {"system": system, "value": cda_id.extension}
    return {"system": "urn:ietf:rfc:3986", "value": cda_id.as_uri() or cda_id.root}
