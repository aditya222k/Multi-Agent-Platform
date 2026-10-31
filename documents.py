"""PDF, Word, and text documents: checked, read once into text in a separate process, and fitted into prompts."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from core import UserError


MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_DOCUMENTS = 10  # Per chat.
MAX_NAME_LENGTH = 255
MAX_TEXT_CHARS = 1_000_000  # Kept per document; longer text is cut with a warning.
MAX_PAGES = 2000
EXTRACT_SECONDS = 60
EXTRACT_MEMORY = 1024 * 1024 * 1024
# A .docx file is a ZIP archive. These stop a small upload from unpacking into gigabytes.
MAX_ZIP_ENTRIES = 2000
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
MAX_XML_BYTES = 50 * 1024 * 1024
# Characters of document text in each prompt, shared by all of a chat's documents.
DOCUMENT_BUDGET = 80_000
KIND_LABELS = {"pdf": "PDF", "docx": "Word document", "txt": "Text file"}
# Upload types for the Supabase bucket, which matches them exactly, without a charset.
MEDIA_TYPES = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "txt": "text/plain"}
DOCUMENT_FILE = re.compile(r"^[0-9a-f]{32}\.(pdf|docx|txt)$")
PAGE_MARK = re.compile(r"^\[page (\d+)\]$", re.MULTILINE)
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
OLE_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def document_kind(data: bytes) -> str:
    """The real file type from its first bytes; the file name is never trusted."""
    if b"%PDF-" in data[:1024]:
        return "pdf"
    if data.startswith(OLE_HEADER):
        raise UserError("Old Word files (.doc) are not supported. Open it in Word, save it as .docx, and attach that.")
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if "word/document.xml" in archive.namelist():
                    return "docx"
        except (zipfile.BadZipFile, OSError, ValueError):
            raise UserError("This file looks like a damaged Word document.") from None
        raise UserError("Only Word documents (.docx) can be attached from ZIP-based files.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = None
    if text is not None and "\x00" not in text:
        return "txt"
    raise UserError("Attach a PDF, a Word document (.docx), or a plain text file.")


def clean_name(value: object) -> str:
    if not isinstance(value, str):
        raise UserError("The file needs a name.")
    # Only shown to people; files are stored under random IDs, never this name.
    name = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = "".join(char for char in name if ord(char) >= 32 and char != "\x7f")
    if not name:
        raise UserError("The file needs a name.")
    return name[:MAX_NAME_LENGTH]


def _limit_memory() -> None:
    # macOS ignores or rejects address-space limits, so there the time limit is the safeguard.
    if sys.platform.startswith("linux"):
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (EXTRACT_MEMORY, EXTRACT_MEMORY))
        except (ImportError, ValueError, OSError):
            pass


def read_document(data: bytes, name: object) -> dict:
    """Check a file and read its text once, in a separate process with a time limit. Returns the saved record."""
    name = clean_name(name)
    if not data:
        raise UserError(f"{name} is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UserError("Documents can be up to 25 MB.")
    kind = document_kind(data)
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), kind], input=data,
                                capture_output=True, timeout=EXTRACT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise UserError(f"Reading {name} took too long, so it was stopped.") from None
    try:
        output = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError):
        output = None
    if not isinstance(output, dict):
        raise UserError(f"{name} could not be read. It may be damaged or too complex.")
    if output.get("error"):
        raise UserError(f"{name}: {output['error']}")
    document_id = uuid.uuid4().hex
    return {
        "id": document_id,
        "name": name,
        "kind": kind,
        "size": len(data),
        "pages": int(output["pages"]),
        "text": output["text"],
        "warnings": [str(warning) for warning in output["warnings"]],
        "file": f"{document_id}.{kind}",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def clean_document(raw: object) -> dict | None:
    """A saved document record, checked like everything else loaded from storage."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not isinstance(raw.get("text"), str):
        return None
    if raw.get("kind") not in KIND_LABELS or not isinstance(raw.get("file"), str) or not DOCUMENT_FILE.fullmatch(raw["file"]):
        return None
    try:
        name = clean_name(raw.get("name"))
    except UserError:
        return None
    warnings = raw.get("warnings") if isinstance(raw.get("warnings"), list) else []
    return {
        "id": raw["id"],
        "name": name,
        "kind": raw["kind"],
        "size": raw["size"] if type(raw.get("size")) is int else 0,
        "pages": raw["pages"] if type(raw.get("pages")) is int else 0,
        "text": raw["text"][:MAX_TEXT_CHARS],
        "warnings": [str(warning)[:300] for warning in warnings[:10]],
        "file": raw["file"],
        "created_at": str(raw.get("created_at", "")),
    }


def summary(document: dict) -> dict:
    """What the page shows: everything except the text itself."""
    return {**{key: value for key, value in document.items() if key != "text"}, "chars": len(document["text"])}


def _trim(document: dict, limit: int) -> tuple[str, str]:
    """The document's text within limit characters, cut at a page or line boundary, and a note on what was left out."""
    text = document["text"]
    if len(text) <= limit:
        return text, ""
    if document["kind"] == "pdf":
        cuts = [mark for mark in PAGE_MARK.finditer(text) if 0 < mark.start() <= limit]
        if cuts:
            included = int(cuts[-1].group(1)) - 1
            return text[:cuts[-1].start()].rstrip(), f"pages 1–{included} of {document['pages']} included"
    cut = text.rfind("\n", 0, limit)
    cut = cut if cut > limit // 2 else limit
    if document["kind"] == "pdf":
        return text[:cut].rstrip(), f"only the start of page 1 of {document['pages']} included"
    return text[:cut].rstrip(), f"first {max(1, round(100 * cut / len(text)))}% included"


def fit_documents(documents: list[dict], budget: int = DOCUMENT_BUDGET) -> list[tuple[str, str]]:
    """Each document's label and text for a prompt. Short documents fit whole; long ones share what is left."""
    if not documents:
        return []
    shares = {}
    remaining, left = budget, len(documents)
    for document in sorted(documents, key=lambda item: len(item["text"])):
        shares[document["id"]] = min(len(document["text"]), remaining // left)
        remaining -= shares[document["id"]]
        left -= 1
    fitted = []
    for document in documents:
        text, note = _trim(document, shares[document["id"]])
        details = [KIND_LABELS[document["kind"]]]
        if document["pages"]:
            details.append(f"{document['pages']} page{'s' if document['pages'] != 1 else ''}")
        if note:
            details.append(note)
        fitted.append((f"{document['name']} ({', '.join(details)})", text))
    return fitted


# Everything below runs in the separate reading process.

def _finish(text: str, pages: int, warnings: list[str]) -> dict:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        warnings.append(f"Very long: only the first {MAX_TEXT_CHARS:,} characters were kept.")
    return {"text": text, "pages": pages, "warnings": warnings}


def extract_pdf(data: bytes) -> dict:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise UserError("Install pypdf (python3 -m pip install pypdf) to attach PDF files.") from None
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise UserError("This PDF is password protected. Remove the password and attach it again.")
    total = len(reader.pages)
    parts, empty = [], 0
    for number in range(min(total, MAX_PAGES)):
        text = (reader.pages[number].extract_text() or "").strip()
        empty += not text
        # Page markers let bots cite pages and let long documents be trimmed between pages.
        parts.append(f"[page {number + 1}]\n{text}")
    read = min(total, MAX_PAGES)
    if not read or empty == read:
        raise UserError("This PDF has no selectable text. It is probably scanned, and scanned PDFs are not supported yet.")
    warnings = []
    if empty:
        warnings.append(f"{empty} of {read} pages have no text (probably scanned images).")
    if total > MAX_PAGES:
        warnings.append(f"Only the first {MAX_PAGES:,} of {total:,} pages were read.")
    return _finish("\n\n".join(parts), total, warnings)


def _paragraph(paragraph: ElementTree.Element) -> str:
    pieces = []
    for element in paragraph.iter():
        if element.tag == W + "t":
            pieces.append(element.text or "")
        elif element.tag == W + "tab":
            pieces.append("\t")
        elif element.tag in (W + "br", W + "cr"):
            pieces.append("\n")
    text = "".join(pieces).strip()
    if not text:
        return ""
    style = paragraph.find(f"{W}pPr/{W}pStyle")
    heading = re.fullmatch(r"(?i)heading\s?([1-6])", style.get(W + "val", "")) if style is not None else None
    if heading:
        return "#" * int(heading.group(1)) + " " + text
    if paragraph.find(f"{W}pPr/{W}numPr") is not None:
        return "- " + text
    return text


def _blocks(parent: ElementTree.Element, lines: list[str]) -> None:
    for element in parent:
        if element.tag == W + "p":
            line = _paragraph(element)
            if line:
                lines.append(line)
        elif element.tag == W + "tbl":
            for row in element.findall(W + "tr"):
                cells = [" ".join(filter(None, (_paragraph(item) for item in cell.iter(W + "p")))) for cell in row.findall(W + "tc")]
                if any(cells):
                    lines.append("| " + " | ".join(cell.replace("\n", " ") for cell in cells) + " |")
        elif element.tag == W + "sdt":
            content = element.find(W + "sdtContent")
            if content is not None:
                _blocks(content, lines)


def extract_docx(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES or sum(entry.file_size for entry in entries) > MAX_UNZIPPED_BYTES:
            raise UserError("This Word document unpacks to too much data, so it was not read.")
        # The sizes in a ZIP's header can lie, so the read itself is capped too.
        with archive.open("word/document.xml") as handle:
            xml = handle.read(MAX_XML_BYTES + 1)
    if len(xml) > MAX_XML_BYTES:
        raise UserError("This Word document is too large to read.")
    # Word never writes a DOCTYPE; refusing one rules out entity tricks in the XML.
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise UserError("This Word document contains unsupported XML.")
    body = ElementTree.fromstring(xml).find(W + "body")
    lines: list[str] = []
    if body is not None:
        _blocks(body, lines)
    if not lines:
        raise UserError("This Word document has no text.")
    return _finish("\n".join(lines), 0, [])


def extract_text(data: bytes) -> dict:
    text = data.decode("utf-8-sig")
    if not text.strip():
        raise UserError("This text file is empty.")
    return _finish(text, 0, [])


EXTRACTORS = {"pdf": extract_pdf, "docx": extract_docx, "txt": extract_text}


def _main() -> None:
    _limit_memory()
    data = sys.stdin.buffer.read(MAX_UPLOAD_BYTES + 1)
    try:
        result = EXTRACTORS[sys.argv[1]](data)
    except UserError as error:
        result = {"error": str(error)}
    except Exception:
        result = {"error": "This file could not be read. It may be damaged."}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    _main()
