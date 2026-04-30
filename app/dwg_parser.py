"""Indekser AutoCAD DWG/DXF-filer for søk.

DWG er Autodesks proprietære binærformat. Vi konverterer det til DXF
(tekst-format) med libredwg's `dwg2dxf` CLI, parser med `ezdxf` for å
hente tekst (titteltabell, lag, blokker, annotasjoner, dimensjoner), og
rendrer til PNG for multimodal embedding der Gemini er aktiv.

Fokus på det som er søkbart innhold:
  • Drawing-properties (DWGPROPS): tittel, forfatter, prosjekt, dato
  • Titteltabell: blokk-attributter (gjerne KUNDE/PROSJEKT/REV/DATO/SKALA)
  • All TEXT, MTEXT, ATTRIB, ATTDEF, MULTILEADER
  • Lag-navn (gir rom-/bygningsdel-info: A-WALL, A-DOOR, M-PIPE-...)
  • Blokk-navn (forteller hvilke symboler som er brukt)
  • Dimensjoner (mål)

Alt samles til ett tekst-dokument som chunkes som vanlig.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Optional

from .logging_config import get_logger
from .unstructured_proc import Chunk, ImageBlob, ParseResult

log = get_logger(__name__)


def is_dwg_or_dxf(file_name: str) -> bool:
    ext = os.path.splitext(file_name)[1].lower()
    return ext in (".dwg", ".dxf")


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _convert_dwg_to_dxf(dwg_path: str, out_dir: str) -> Optional[str]:
    """Run libredwg's dwg2dxf to produce a sibling .dxf file.

    Returns the path to the produced .dxf, or None if conversion failed.
    """
    cli = _which("dwg2dxf")
    if not cli:
        log.warning(
            "dwg.no_converter",
            hint="libredwg-tools is missing — install via apt-get install libredwg-tools",
        )
        return None
    base = os.path.splitext(os.path.basename(dwg_path))[0]
    out_path = os.path.join(out_dir, f"{base}.dxf")
    try:
        proc = subprocess.run(
            [cli, "-y", "-o", out_path, dwg_path],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0 or not os.path.exists(out_path):
            log.warning(
                "dwg.convert.failed",
                file=os.path.basename(dwg_path),
                stderr=(proc.stderr or "")[:300],
            )
            return None
        return out_path
    except subprocess.TimeoutExpired:
        log.warning("dwg.convert.timeout", file=os.path.basename(dwg_path))
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("dwg.convert.error", file=os.path.basename(dwg_path), err=str(e))
        return None


def _extract_text_from_dxf(dxf_path: str) -> tuple[str, dict]:
    """Walk the DXF and produce a single text blob plus metadata dict.

    Captures headers, summary props, layer names, block names, all TEXT/
    MTEXT entities, ATTRIB entries (often used in title blocks), and
    dimensions. Logs but doesn't raise on partial parse errors.
    """
    import ezdxf
    from ezdxf import recover

    parts: list[str] = []
    metadata: dict = {}

    try:
        doc, _auditor = recover.readfile(dxf_path)
    except ezdxf.DXFStructureError as e:
        log.warning("dwg.dxf.invalid", file=os.path.basename(dxf_path), err=str(e))
        return "", {}
    except Exception as e:  # noqa: BLE001
        log.warning("dwg.dxf.error", file=os.path.basename(dxf_path), err=str(e))
        return "", {}

    # ─── Document properties (DWGPROPS / SUMMARYINFO) ──────────────
    try:
        si = doc.summary_info
        for attr in ("title", "subject", "author", "comments",
                     "keywords", "last_saved_by", "manager"):
            v = getattr(si, attr, "") or ""
            if v:
                metadata[f"prop_{attr}"] = v
                parts.append(f"{attr.upper()}: {v}")
        for k, v in (si.custom_properties or {}).items():
            if v:
                metadata[f"custom_{k}"] = str(v)
                parts.append(f"{k}: {v}")
    except Exception:  # noqa: BLE001
        pass

    # ─── Header: dwg version, creation date, units ─────────────────
    try:
        hdr = doc.header
        for var in ("$ACADVER", "$TDCREATE", "$TDUPDATE", "$INSUNITS",
                    "$LIMMIN", "$LIMMAX", "$EXTMIN", "$EXTMAX"):
            try:
                v = hdr.get(var, default=None)
                if v is not None:
                    parts.append(f"{var}: {v}")
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass

    # ─── Layer names (gives building-element taxonomy) ─────────────
    try:
        layers = sorted(set(layer.dxf.name for layer in doc.layers))
        if layers:
            parts.append("LAYERS: " + ", ".join(layers))
            metadata["layer_count"] = len(layers)
    except Exception:  # noqa: BLE001
        pass

    # ─── Block names (symbol taxonomy) ─────────────────────────────
    try:
        block_names = []
        for block in doc.blocks:
            n = block.name
            # Skip anonymous & paper-space blocks.
            if not n or n.startswith("*"):
                continue
            block_names.append(n)
        if block_names:
            parts.append("BLOCKS: " + ", ".join(sorted(set(block_names))))
            metadata["block_count"] = len(set(block_names))
    except Exception:  # noqa: BLE001
        pass

    # ─── Text-bearing entities across all spaces ──────────────────
    text_count = 0
    msp = doc.modelspace()
    text_lines: list[str] = []

    def _grab(entity_iter):
        nonlocal text_count
        for e in entity_iter:
            try:
                t = (e.dxf.text or "").strip()
            except Exception:
                t = ""
            if not t:
                # MText / Multileader handle text differently.
                t = getattr(e, "plain_text", lambda: "")()
            if isinstance(t, str) and t.strip():
                text_lines.append(t.strip())
                text_count += 1

    try:
        _grab(msp.query("TEXT MTEXT ATTRIB ATTDEF MULTILEADER"))
        # Also paper space layouts (where title blocks usually live).
        for layout in doc.layouts:
            if layout.name == "Model":
                continue
            try:
                _grab(layout.query("TEXT MTEXT ATTRIB ATTDEF MULTILEADER"))
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        log.warning("dwg.text.error", err=str(e))

    if text_lines:
        parts.append("TEKST FRA TEGNINGEN:")
        parts.extend(text_lines)
    metadata["text_entities"] = text_count

    # ─── Dimensions (numbers users care about) ────────────────────
    try:
        dims = []
        for dim in msp.query("DIMENSION"):
            try:
                txt = dim.dxf.text or dim.get_measurement()
                if txt:
                    dims.append(str(txt))
            except Exception:  # noqa: BLE001
                pass
        if dims:
            parts.append("DIMENSJONER: " + ", ".join(dims[:200]))
    except Exception:  # noqa: BLE001
        pass

    return "\n".join(parts), metadata


def _render_dxf_to_png(dxf_path: str, out_path: str, max_size: int = 2000) -> bool:
    """Render the modelspace to PNG using ezdxf's matplotlib backend.

    Returns True on success. Best-effort — failures here just mean the
    drawing won't have an image embedding, the text still goes through.
    """
    try:
        import ezdxf
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
        import matplotlib.pyplot as plt

        doc = ezdxf.readfile(dxf_path)
        msp = doc.modelspace()

        fig = plt.figure(figsize=(20, 14), dpi=120)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_aspect("equal")
        ax.set_axis_off()

        ctx = RenderContext(doc)
        backend = MatplotlibBackend(ax)
        Frontend(ctx, backend).draw_layout(msp, finalize=True)

        fig.savefig(out_path, dpi=120, bbox_inches="tight",
                    facecolor="white", edgecolor="none")
        plt.close(fig)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception as e:  # noqa: BLE001
        log.warning("dwg.render.failed", err=str(e))
        return False


def parse_dwg_or_dxf(file_path: str, *, extract_image: bool = False) -> ParseResult:
    """Top-level entry point — handles both .dwg and .dxf inputs.

    Returns ParseResult with text chunks and (optionally) one rendered PNG
    for multimodal embedding.
    """
    ext = os.path.splitext(file_path)[1].lower()
    work_dir = tempfile.mkdtemp(prefix="dwg_")
    try:
        if ext == ".dwg":
            dxf_path = _convert_dwg_to_dxf(file_path, work_dir)
            if not dxf_path:
                return ParseResult()
        else:
            dxf_path = file_path

        text_blob, md = _extract_text_from_dxf(dxf_path)
        chunks: list[Chunk] = []
        if text_blob.strip():
            chunks.append(Chunk(
                text=text_blob,
                metadata={
                    "filetype": ext.lstrip("."),
                    "source": "dwg-parser",
                    **md,
                },
            ))

        images: list[ImageBlob] = []
        if extract_image:
            png_path = os.path.join(work_dir, "render.png")
            if _render_dxf_to_png(dxf_path, png_path):
                with open(png_path, "rb") as fh:
                    images.append(ImageBlob(
                        data=fh.read(),
                        mime="image/png",
                        metadata={
                            "filetype": ext.lstrip("."),
                            "source": "dwg-parser",
                            "rendered": True,
                        },
                    ))

        log.info(
            "dwg.parse.done",
            file=os.path.basename(file_path),
            text_chars=len(text_blob),
            images=len(images),
        )
        return ParseResult(chunks=chunks, images=images)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
