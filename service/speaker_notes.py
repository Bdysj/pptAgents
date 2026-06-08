"""Extract the deck's speaker notes into a single, slide-ordered Markdown file.

The pipeline embeds one speaker note per slide into the .pptx, sourced from
``notes/<svg_stem>.md`` (one file per page) and matched to slides in sorted SVG
order — see ``svg_to_pptx/pptx_discovery.find_notes_files``. This module mirrors
that exact ordering and pairing to produce a standalone ``*_speaker_notes.md``
deliverable that matches the notes baked into the deck, one section per slide.
"""

from __future__ import annotations

from pathlib import Path


def _ordered_svg_stems(project_path: Path) -> list[str]:
    """Slide order as used by the exporter: prefer svg_final/ (what was actually
    exported), fall back to svg_output/."""
    for sub in ("svg_final", "svg_output"):
        d = project_path / sub
        if d.is_dir():
            stems = [p.stem for p in sorted(d.glob("*.svg"))]
            if stems:
                return stems
    return []


def build_speaker_notes_md(project_path: Path, pptx_path: Path) -> Path | None:
    """Compose exports/<deck>_speaker_notes.md from per-page notes, in slide
    order. Returns the written path, or None if there are no notes to extract.

    Never raises for missing notes — note extraction is best-effort packaging and
    must not fail the job."""
    notes_dir = project_path / "notes"
    if not notes_dir.is_dir():
        return None

    stems = _ordered_svg_stems(project_path)
    deck_title = pptx_path.stem

    sections: list[str] = []
    page_no = 0
    if stems:
        for stem in stems:
            note_file = notes_dir / f"{stem}.md"
            if not note_file.is_file():
                continue
            try:
                content = note_file.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not content:
                continue
            page_no += 1
            sections.append(f"## 第 {page_no} 页 · {stem}\n\n{content}")
    else:
        # Fallback: no SVGs to order by — use total.md verbatim if present.
        total = notes_dir / "total.md"
        if total.is_file():
            try:
                body = total.read_text(encoding="utf-8").strip()
            except OSError:
                body = ""
            if body:
                sections.append(body)

    if not sections:
        return None

    header = (
        f"# {deck_title} — 演讲稿\n\n"
        f"> 共 {page_no if page_no else len(sections)} 页 · 由 PPT Master 自动从演讲者备注导出\n"
    )
    document = header + "\n" + "\n\n---\n\n".join(sections) + "\n"

    out_path = pptx_path.with_name(f"{deck_title}_speaker_notes.md")
    out_path.write_text(document, encoding="utf-8")
    return out_path
