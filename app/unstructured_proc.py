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


@dataclass
class Chunk:
    text: str
    metadata: dict = field(default_factory=dict)


def _pick_strategy(file_path: str, configured: UnstructuredStrategy) -> str:
    if configured != UnstructuredStrategy.AUTO:
        return configured.value
    ext = os.path.splitext(file_path)[1].lower()
    if ext in _HI_RES_EXT:
        return "hi_res"
    return "fast"


def is_supported_extension(file_name: str) -> bool:
    ext = os.path.splitext(file_name)[1].lower()
    return ext in _HI_RES_EXT or ext in _FAST_OK_EXT


def partition_and_chunk(file_path: str, settings: Settings) -> list[Chunk]:
    """Partition a single file and return chunked Chunk objects.

    Raises on truly unrecoverable errors. Returns [] for files we explicitly
    skip (e.g. unsupported binary types).
    """
    # Local imports — see module docstring.
    from unstructured.chunking.title import chunk_by_title
    from unstructured.partition.auto import partition

    if not is_supported_extension(file_path):
        log.info("unstructured.skip.unsupported", file=os.path.basename(file_path))
        return []

    strategy = _pick_strategy(file_path, settings.unstructured_strategy)
    log.info(
        "unstructured.partition.start",
        file=os.path.basename(file_path),
        strategy=strategy,
    )

    try:
        elements = partition(
            filename=file_path,
            strategy=strategy,
            # Keep page numbers and other useful metadata where possible.
            include_page_breaks=True,
            # OCR languages — adjust for your tenant. eng+nor covers most.
            languages=["eng", "nor"],
        )
    except Exception as e:  # noqa: BLE001
        # Some hi_res calls fail on weird PDFs; fall back to 'fast' once.
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
        return []

    chunks = chunk_by_title(
        elements,
        max_characters=settings.unstructured_chunk_max_chars,
        new_after_n_chars=int(settings.unstructured_chunk_max_chars * 0.9),
        overlap=settings.unstructured_chunk_overlap,
        combine_text_under_n_chars=200,
    )

    out: list[Chunk] = []
    for el in chunks:
        text = (el.text or "").strip()
        if not text:
            continue
        md_obj = el.metadata
        md = md_obj.to_dict() if hasattr(md_obj, "to_dict") else {}
        # Prune fields that aren't useful in Pinecone metadata and can be huge.
        for noisy in (
            "coordinates",
            "links",
            "regex_metadata",
            "parent_id",
            "orig_elements",
            "text_as_html",
        ):
            md.pop(noisy, None)
        out.append(Chunk(text=text, metadata=_flatten_metadata(md)))

    log.info(
        "unstructured.partition.done",
        file=os.path.basename(file_path),
        elements=len(elements),
        chunks=len(out),
    )
    return out


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
