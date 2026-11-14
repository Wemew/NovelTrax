"""Read/write EPUB OPF metadata without touching the book payload."""

from __future__ import annotations

import os
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Dict, Optional

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
CONTAINER_PATH = "META-INF/container.xml"

ET.register_namespace("", OPF_NS)
ET.register_namespace("dc", DC_NS)
ET.register_namespace("opf", OPF_NS)


class EpubMetadataError(RuntimeError):
    pass


def _find_opf_path(archive: zipfile.ZipFile) -> str:
    try:
        raw = archive.read(CONTAINER_PATH)
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
        opf_path = _find_opf_path(archive)
        try:
            root = ET.fromstring(archive.read(opf_path))
        except ET.ParseError as exc:
            raise EpubMetadataError(f"malformed OPF: {exc}") from exc
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


def write_epub_metadata(path: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite only the OPF entry, atomically preserving every other ZIP entry."""
    if not str(path or "").lower().endswith(".epub"):
        raise EpubMetadataError("not an EPUB")
    if not os.path.isfile(path):
        raise EpubMetadataError("EPUB file not found")

    with zipfile.ZipFile(path, "r") as archive:
        opf_path = _find_opf_path(archive)
        try:
            root = ET.fromstring(archive.read(opf_path))
        except ET.ParseError as exc:
            raise EpubMetadataError(f"malformed OPF: {exc}") from exc
        _apply_metadata(root, fields)
        xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        infos = archive.infolist()
        payloads = {info.filename: archive.read(info) for info in infos}

    temp_path = f"{path}.noveltrax-meta-tmp"
    try:
        with zipfile.ZipFile(temp_path, "w") as output:
            for info in infos:
                data = xml if info.filename == opf_path else payloads[info.filename]
                output.writestr(info, data)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
    return read_epub_metadata(path)
