"""Read/write EPUB OPF metadata without touching the book payload."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Dict, Iterable, Optional

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
CONTAINER_PATH = "META-INF/container.xml"

MAX_OPF_BYTES = 8 * 1024 * 1024
MAX_ENTRY_BYTES = 512 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 5000

_READ_CHUNK_BYTES = 64 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024

ET.register_namespace("", OPF_NS)
ET.register_namespace("dc", DC_NS)
ET.register_namespace("opf", OPF_NS)


class EpubMetadataError(RuntimeError):
    pass


def _bounded_read(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """Read a ZIP entry, aborting once more than ``limit`` bytes are produced.

    The entry's declared ``file_size`` is deliberately ignored: only bytes
    actually decompressed from the archive count toward the limit.
    """
    chunks = []
    total = 0
    with archive.open(name) as handle:
        while True:
            chunk = handle.read(min(_READ_CHUNK_BYTES, limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise EpubMetadataError(
                    f"EPUB entry {name!r} exceeds the {limit}-byte limit"
                )
            chunks.append(chunk)
    return b"".join(chunks)


def _validate_entry_count(infos: list) -> None:
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise EpubMetadataError(
            f"EPUB has {len(infos)} entries, above the limit of {MAX_ARCHIVE_ENTRIES}"
        )


def _validate_declared_sizes(infos: Iterable[zipfile.ZipInfo]) -> None:
    total = 0
    violations = []
    for info in infos:
        if info.file_size > MAX_ENTRY_BYTES:
            violations.append(
                f"entry {info.filename!r} declares {info.file_size} bytes, "
                f"above the limit of {MAX_ENTRY_BYTES}"
            )
        total += info.file_size
    if violations:
        raise EpubMetadataError("EPUB entry too large: " + "; ".join(violations))
    if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise EpubMetadataError(
            f"EPUB declares {total} uncompressed bytes, above the limit of "
            f"{MAX_TOTAL_UNCOMPRESSED_BYTES}"
        )


def _read_opf_root(archive: zipfile.ZipFile, opf_path: str) -> ET.Element:
    try:
        raw = _bounded_read(archive, opf_path, MAX_OPF_BYTES)
    except KeyError as exc:
        raise EpubMetadataError(f"EPUB is missing OPF entry {opf_path!r}") from exc
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise EpubMetadataError(f"malformed OPF: {exc}") from exc


def _find_opf_path(archive: zipfile.ZipFile) -> str:
    try:
        raw = _bounded_read(archive, CONTAINER_PATH, MAX_OPF_BYTES)
        root = ET.fromstring(raw)
        for element in root.iter():
            if element.tag.endswith("rootfile") and element.get("full-path"):
                return element.get("full-path")
    except (KeyError, ET.ParseError):
        pass
    for name in archive.namelist():
        if name.lower().endswith(".opf"):
            return name
    raise EpubMetadataError("EPUB has no OPF package document")


def _metadata_element(root: ET.Element) -> ET.Element:
    metadata = root.find(f"{{{OPF_NS}}}metadata")
    if metadata is None:
        metadata = root.find("metadata")
    if metadata is None:
        metadata = ET.Element(f"{{{OPF_NS}}}metadata")
        root.insert(0, metadata)
    return metadata


def _first_child(parent: ET.Element, tag: str) -> Optional[ET.Element]:
    for child in list(parent):
        if isinstance(child.tag, str) and child.tag == tag:
            return child
    return None


def _set_text(parent: ET.Element, tag: str, value: str) -> ET.Element:
    node = _first_child(parent, tag)
    if node is None:
        node = ET.SubElement(parent, tag)
    node.text = value
    return node


def _set_calibre_meta(metadata: ET.Element, name: str, value: str) -> None:
    for child in list(metadata):
        if child.tag == "meta" and child.get("name") == name:
            child.set("content", value)
            return
        if child.tag == f"{{{OPF_NS}}}meta" and child.get("name") == name:
            child.set("content", value)
            return
    ET.SubElement(metadata, "meta", {"name": name, "content": value})


def _set_collection_meta(metadata: ET.Element, series: str, series_index: float) -> None:
    node = None
    for child in list(metadata):
        if child.get("property") == "belongs-to-collection":
            node = child
            break
    if node is None:
        node = ET.SubElement(metadata, "meta", {"property": "belongs-to-collection", "id": "series-id"})
    node.text = series
    refine_type = None
    refine_pos = None
    for child in list(metadata):
        refines = child.get("refines")
        if refines == "#series-id" and child.get("property") == "collection-type":
            refine_type = child
        if refines == "#series-id" and child.get("property") == "group-position":
            refine_pos = child
    if refine_type is None:
        refine_type = ET.SubElement(metadata, "meta", {"refines": "#series-id", "property": "collection-type"})
    refine_type.text = "series"
    if refine_pos is None:
        refine_pos = ET.SubElement(metadata, "meta", {"refines": "#series-id", "property": "group-position"})
    refine_pos.text = _format_index(series_index)


def _format_index(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return str(int(number)) if number.is_integer() else str(number)


def _apply_metadata(root: ET.Element, fields: Dict[str, Any]) -> None:
    metadata = _metadata_element(root)
    title = str(fields.get("title") or "").strip()
    author = str(fields.get("author") or "").strip()
    series = str(fields.get("series") or "").strip()
    series_index = fields.get("series_index")
    if title:
        _set_text(metadata, f"{{{DC_NS}}}title", title)
    if author:
        _set_text(metadata, f"{{{DC_NS}}}creator", author)
    if series:
        _set_calibre_meta(metadata, "calibre:series", series)
        _set_calibre_meta(metadata, "calibre:title_sort", title or series)
        if series_index is not None:
            _set_calibre_meta(metadata, "calibre:series_index", _format_index(series_index))
            _set_collection_meta(metadata, series, series_index)
    if author:
        _set_calibre_meta(metadata, "calibre:author_sort", author)


def read_epub_metadata(path: str) -> Dict[str, Any]:
    if not str(path or "").lower().endswith(".epub"):
        raise EpubMetadataError("not an EPUB")
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        _validate_entry_count(infos)
        opf_path = _find_opf_path(archive)
        root = _read_opf_root(archive, opf_path)
    metadata = _metadata_element(root)
    result = {"title": "", "author": "", "series": "", "series_index": None}
    title = _first_child(metadata, f"{{{DC_NS}}}title")
    creator = _first_child(metadata, f"{{{DC_NS}}}creator")
    if title is not None and title.text:
        result["title"] = title.text.strip()
    if creator is not None and creator.text:
        result["author"] = creator.text.strip()
    for child in list(metadata):
        name = child.get("name") or ""
        if name == "calibre:series":
            result["series"] = (child.get("content") or "").strip()
        elif name == "calibre:series_index":
            raw = child.get("content") or ""
            try:
                result["series_index"] = float(raw)
            except ValueError:
                pass
    return result


def _create_temp_path(path: str) -> str:
    directory = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".ntmeta",
        dir=directory,
    )
    os.close(fd)
    return temp_path


def write_epub_metadata(path: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite only the OPF entry, atomically preserving every other ZIP entry."""
    if not str(path or "").lower().endswith(".epub"):
        raise EpubMetadataError("not an EPUB")
    if os.path.islink(path):
        raise EpubMetadataError("refusing to rewrite a symlinked EPUB")
    if not os.path.isfile(path):
        raise EpubMetadataError("EPUB file not found")

    temp_path = None
    try:
        with zipfile.ZipFile(path, "r") as source:
            infos = source.infolist()
            _validate_entry_count(infos)
            _validate_declared_sizes(infos)
            opf_path = _find_opf_path(source)
            root = _read_opf_root(source, opf_path)
            _apply_metadata(root, fields)
            xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)

            temp_path = _create_temp_path(path)
            with zipfile.ZipFile(temp_path, "w") as output:
                for info in infos:
                    if info.filename == opf_path:
                        output.writestr(info, xml)
                        continue
                    with source.open(info) as src, output.open(info, "w") as dst:
                        shutil.copyfileobj(src, dst, _COPY_CHUNK_BYTES)
        # The source is closed here so the replacement works on Windows too.
        os.chmod(temp_path, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(temp_path, path)
        temp_path = None
    except Exception:
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise
    return read_epub_metadata(path)
