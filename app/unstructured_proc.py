"""Wrapper around the `unstructured` library (self-hosted).

Responsibilities:
  * Pick a partition strategy ('fast' for office docs / text, 'hi_res' for
    PDFs and images that benefit from layout detection + OCR).
  * Partition the file -> chunk by title -> return clean text + metadata.
  * Be robust: catch per-file errors so one bad file can't kill the run.

Heavy: importing unstructured pulls a lot of modules. Imports are local
to keep cold start of the FastAPI process tolerable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from .config import Settings, UnstructuredStrategy
from .logging_config import get_logger

log = get_logger(__name__)


# Mime/extension routing for picking a strategy in 'auto' mode.
_HI_RES_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}
_FAST_OK_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml",
    ".html", ".htm", ".eml", ".msg", ".rtf",
    ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".odt", ".ods", ".odp",
    ".epub",
}
# CAD files — handled by dwg_parser.py (libredwg + ezdxf), not Unstructured.
_CAD_EXT = {".dwg", ".dxf"}


@dataclass
class Chunk:
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class ImageBlob:
    """A raw image extracted from a document, for multimodal embedding."""
    data: bytes
    mime: str
    metadata: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    chunks: list[Chunk] = field(default_factory=list)
    images: list[ImageBlob] = field(default_factory=list)


def _pick_strategy(file_path: str, configured: UnstructuredStrategy) -> str:
    if configured != UnstructuredStrategy.AUTO:
        return configured.value
    ext = os.path.splitext(file_path)[1].lower()
    if ext in _HI_RES_EXT:
        return "hi_res"
    return "fast"


def is_supported_extension(file_name: str) -> bool:
    ext = os.path.splitext(file_name)[1].lower()
    return ext in _HI_RES_EXT or ext in _FAST_OK_EXT or ext in _CAD_EXT


def is_cad_extension(file_name: str) -> bool:
    return os.path.splitext(file_name)[1].lower() in _CAD_EXT


def partition_and_chunk(
    file_path: str,
    settings: Settings,
    *,
    extract_images: bool = False,
) -> ParseResult:
    """Partition a single file. Returns a ParseResult holding text chunks
    and optionally raw image blobs (for multimodal providers).

    Raises on truly unrecoverable errors. Returns an empty ParseResult for
    files we explicitly skip.
    """
    # Local imports — see module docstring.
    from unstructured.chunking.title import chunk_by_title
    from unstructured.partition.auto import partition

    if not is_supported_extension(file_path):
        log.info("unstructured.skip.unsupported", file=os.path.basename(file_path))
        return ParseResult()

    # ─── DWG/DXF — CAD files use a separate parser (libredwg + ezdxf) ──
    if is_cad_extension(file_path):
        from .dwg_parser import parse_dwg_or_dxf
        return parse_dwg_or_dxf(file_path, extract_image=extract_images)

    strategy = _pick_strategy(file_path, settings.unstructured_strategy)
    log.info(
        "unstructured.partition.start",
        file=os.path.basename(file_path),
        strategy=strategy,
        extract_images=extract_images,
    )

    partition_kwargs = dict(
        filename=file_path,
        strategy=strategy,
        include_page_breaks=True,
        languages=["eng", "nor"],
    )
    if extract_images and strategy == "hi_res":
        # Ask Unstructured to keep image elements with payloads (b64 by default).
        partition_kwargs.update(
            extract_images_in_pdf=True,
            extract_image_block_types=["Image", "Table"],
            extract_image_block_to_payload=True,
        )

    try:
        elements = partition(**partition_kwargs)
    except Exception as e:  # noqa: BLE001
        if strategy == "hi_res":
            log.warning(
                "unstructured.hi_res.fallback",
                file=os.path.basename(file_path),
                err=str(e),
            )
            elements = partition(filename=file_path, strategy="fast")
        else:
            raise

    if not elements:
        return ParseResult()

    # Pull out image blobs *before* chunk_by_title (which collapses Image
    # elements into surrounding text).
    images: list[ImageBlob] = []
    if extract_images:
        for el in elements:
            blob = _image_blob_from(el)
            if blob:
                images.append(blob)

    chunks_raw = chunk_by_title(
        elements,
        max_characters=settings.unstructured_chunk_max_chars,
        new_after_n_chars=int(settings.unstructured_chunk_max_chars * 0.9),
        overlap=settings.unstructured_chunk_overlap,
        combine_text_under_n_chars=200,
    )

    chunks: list[Chunk] = []
    for el in chunks_raw:
        text = (el.text or "").strip()
        if not text:
            continue
        md_obj = el.metadata
        md = md_obj.to_dict() if hasattr(md_obj, "to_dict") else {}
        for noisy in (
            "coordinates",
            "links",
            "regex_metadata",
            "parent_id",
            "orig_elements",
            "text_as_html",
            "image_base64",
        ):
            md.pop(noisy, None)
        chunks.append(Chunk(text=text, metadata=_flatten_metadata(md)))

    log.info(
        "unstructured.partition.done",
        file=os.path.basename(file_path),
        elements=len(elements),
        chunks=len(chunks),
        images=len(images),
    )
    return ParseResult(chunks=chunks, images=images)


def _image_blob_from(el) -> ImageBlob | None:
    """Convert an Unstructured Image element with embedded payload to ImageBlob.

    Unstructured stores the bytes as base64 in element.metadata.image_base64
    when extract_image_block_to_payload=True. We decode and pair with the
    detected mime type (default to PNG).
    """
    try:
        category = getattr(el, "category", None) or el.__class__.__name__
        if category not in ("Image", "Table", "FigureCaption"):
            return None
        md_obj = el.metadata
        md = md_obj.to_dict() if hasattr(md_obj, "to_dict") else {}
        b64 = md.get("image_base64")
        if not b64:
            return None
        import base64
        data = base64.b64decode(b64)
        mime = md.get("image_mime_type") or "image/png"
        # Light cleanup for the metadata we keep.
        clean = {
            "category": category,
            "page_number": md.get("page_number"),
            "filetype": md.get("filetype"),
        }
        return ImageBlob(data=data, mime=mime, metadata={k: v for k, v in clean.items() if v is not None})
    except Exception as e:  # noqa: BLE001
        log.warning("unstructured.image_blob.error", err=str(e))
        return None


def _flatten_metadata(md: dict) -> dict:
    """Pinecone metadata must be primitive types or lists of strings.

    Drop None/empty, coerce simple dicts to strings.
    """
    flat: dict = {}
    for k, v in md.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            flat[k] = v
        elif isinstance(v, list):
            # Only keep string-coercible lists.
            try:
                flat[k] = [str(x) for x in v if x is not None][:50]
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                s = str(v)
                if 0 < len(s) < 500:
                    flat[k] = s
            except Exception:  # noqa: BLE001
                pass
    return flat
