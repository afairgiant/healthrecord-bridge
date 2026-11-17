"""Discover C-CDA documents inside zip archives, including IHE XDM exports.

Epic MyChart's "Download my record" produces an IHE XDM archive shaped like
``MachineReadable_XDMFormat/IHE_XDM/<subset>/DOC0001.XML`` next to
``METADATA.XML``, ``INDEX.HTM`` and ``README.TXT``. Any zip that holds C-CDA XML
anywhere is accepted; the XDM layout is just preferred when present.
"""

from __future__ import annotations

import os
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

MAX_MEMBER_BYTES = 256 * 1024 * 1024
"""A document larger than this (a zip member uncompressed, or a bare XML file) is refused."""
MAX_DOCUMENTS = 10_000
"""A zip with more XML documents than this is refused."""
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
"""A zip whose XML documents add up to more than this (uncompressed) is refused."""

_SKIP_NAMES = {"metadata.xml", "index.htm", "index.html", "readme.txt"}


def is_zip(path: str | os.PathLike[str]) -> bool:
    return zipfile.is_zipfile(path)


def iter_xml_members(zf: zipfile.ZipFile) -> Iterator[zipfile.ZipInfo]:
    """Yield XML members in a stable order, preferring those under an ``IHE_XDM`` folder."""
    xdm: list[zipfile.ZipInfo] = []
    other: list[zipfile.ZipInfo] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        p = PurePosixPath(info.filename)
        if p.suffix.lower() != ".xml" or p.name.lower() in _SKIP_NAMES:
            continue
        if any(part.upper() == "IHE_XDM" for part in p.parts):
            xdm.append(info)
        else:
            other.append(info)
    members = xdm if xdm else other
    yield from sorted(members, key=lambda i: i.filename)


def iter_documents(path: str | os.PathLike[str]) -> Iterator[tuple[str, bytes]]:
    """Yield ``(member name, bytes)`` for every candidate XML in the zip.

    The members are checked against ``MAX_DOCUMENTS``, ``MAX_TOTAL_BYTES`` and
    ``MAX_MEMBER_BYTES`` before anything is extracted. The sizes come from the zip's
    directory, and ``zipfile`` never returns more than a member's declared size.
    """
    name = Path(path).name
    with zipfile.ZipFile(path) as zf:
        members = list(iter_xml_members(zf))
        if len(members) > MAX_DOCUMENTS:
            raise ValueError(
                f"{name} has {len(members)} XML documents, over the {MAX_DOCUMENTS} limit"
            )
        total = sum(info.file_size for info in members)
        if total > MAX_TOTAL_BYTES:
            raise ValueError(
                f"{name} holds {total} bytes of XML, over the {MAX_TOTAL_BYTES} byte limit"
            )
        for info in members:
            if info.file_size > MAX_MEMBER_BYTES:
                raise ValueError(
                    f"{name}:{info.filename} is {info.file_size} bytes, "
                    f"over the {MAX_MEMBER_BYTES} byte limit"
                )
        for info in members:
            with zf.open(info) as fh:
                yield f"{name}:{info.filename}", fh.read(MAX_MEMBER_BYTES + 1)


def read_xml_file(path: str | os.PathLike[str]) -> bytes:
    """Read a bare XML file, refusing one over ``MAX_MEMBER_BYTES`` like a zip member."""
    p = Path(path)
    with p.open("rb") as fh:
        data = fh.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        raise ValueError(f"{p.name} is over the {MAX_MEMBER_BYTES} byte limit")
    return data
