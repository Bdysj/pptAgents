"""Filesystem-derived progress percentage for a running job.

Every value below is anchored to a *real artifact the pipeline writes to disk*,
so progress reflects actual work done, not a timer. The dominant, naturally
fine-grained segment is per-page SVG generation (each page is one file in
``svg_output/``), which keeps most steps at/under ~3–4%. The coarser jumps are
in the Strategist phase (design_spec → spec_lock), where no finer on-disk signal
exists — these are left as honest milestones rather than fabricated sub-steps.

Pipeline artifacts (in order):
  sources/*.md  -> design_spec.md -> spec_lock.md -> images/*.png
  -> svg_output/*.svg (xN pages) -> notes/total.md -> notes/<page>.md (split)
  -> svg_final/*.svg (xN) -> exports/*.pptx
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .schemas import JobStatus

# Cumulative percentage anchors per milestone.
P_ACCEPTED = 2        # job claimed, converting/importing
P_CONVERTED = 4       # sources/*.md present
P_AGENT_START = 5     # agent launched (status GENERATING)
P_DESIGN_SPEC = 10    # design_spec.md written (Strategist outline)
P_SPEC_LOCK = 15      # spec_lock.md written (Phase A locked)
P_IMAGES_END = 23     # image acquisition complete (15 -> 23 over images)
P_SVG_END = 80        # per-page SVG generation complete (23 -> 80 over pages)
P_NOTES = 84          # notes/total.md written
P_NOTES_SPLIT_END = 88  # notes split per page (84 -> 88)
P_FINAL_END = 97      # svg_final/*.svg finalized (88 -> 97)
P_EXPORTING = 99      # svg->pptx underway (exports not yet present)
P_DONE = 100


def _count_svgs(d: Path) -> int:
    try:
        return sum(1 for _ in d.glob("*.svg"))
    except OSError:
        return 0


def _target_pages(project_path: Path, options: dict, svg_output: int, svg_final: int) -> int:
    """Best-effort target page count, in priority order:
      1. The authoritative "Page Count" field in design_spec.md (once written).
      2. Upper bound of the requested page_count range.
      3. Whatever already exists on disk.
      4. A sane default.
    Never reported below what already exists on disk."""
    on_disk = max(svg_output, svg_final)

    spec = project_path / "design_spec.md"
    if spec.is_file():
        try:
            text = spec.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        m = re.search(r"(?:Page\s*Count|页数|页面数量|总页数)\D{0,12}(\d{1,3})", text, re.IGNORECASE)
        if m:
            return max(int(m.group(1)), on_disk, 1)

    raw = (options or {}).get("page_count")
    if raw:
        nums = [int(n) for n in re.findall(r"\d+", str(raw))]
        if nums:
            return max(max(nums), on_disk, 1)

    if on_disk > 0:
        return on_disk
    return 15


def _image_ratio(project_path: Path, options: dict) -> float | None:
    """Fraction of planned images already generated, or None if images are not
    part of this job (image_mode none / no manifest)."""
    image_mode = str((options or {}).get("image_mode") or "ai").lower()
    if image_mode == "none":
        return 1.0
    images_dir = project_path / "images"
    manifest = images_dir / "image_prompts.json"
    total = 0
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(data, list):
                total = len(data)
            elif isinstance(data, dict):
                for key in ("images", "items", "prompts"):
                    if isinstance(data.get(key), list):
                        total = len(data[key])
                        break
        except (ValueError, OSError):
            total = 0
    if total <= 0:
        return None
    try:
        done = sum(1 for _ in images_dir.glob("*.png"))
    except OSError:
        done = 0
    return min(done / total, 1.0)


def _seg(lo: float, hi: float, ratio: float) -> float:
    return lo + (hi - lo) * max(0.0, min(ratio, 1.0))


def compute_progress(status: JobStatus, project_path: Path | None,
                     options: dict | None) -> int:
    """Return 0–100 for a job. Terminal states are handled by the caller; this
    focuses on live/active jobs and is safe to call even if the dir is gone."""
    if status == JobStatus.DONE:
        return P_DONE
    if status == JobStatus.PENDING:
        return 0
    if project_path is None or not project_path.is_dir():
        return P_ACCEPTED if status == JobStatus.CONVERTING else P_AGENT_START

    options = options or {}
    pct = P_ACCEPTED

    if any((project_path / "sources").glob("*.md")) if (project_path / "sources").is_dir() else False:
        pct = max(pct, P_CONVERTED)
    if status in (JobStatus.GENERATING,):
        pct = max(pct, P_AGENT_START)
    if (project_path / "design_spec.md").is_file():
        pct = max(pct, P_DESIGN_SPEC)
    if (project_path / "spec_lock.md").is_file():
        pct = max(pct, P_SPEC_LOCK)

        # Images (15 -> 23).
        ratio_img = _image_ratio(project_path, options)
        if ratio_img is not None:
            pct = max(pct, round(_seg(P_SPEC_LOCK, P_IMAGES_END, ratio_img)))

        svg_output = _count_svgs(project_path / "svg_output")
        svg_final = _count_svgs(project_path / "svg_final")
        target = _target_pages(project_path, options, svg_output, svg_final)

        # Per-page SVG generation (23 -> 80) — the fine-grained dominant segment.
        if svg_output > 0:
            pct = max(pct, round(_seg(P_IMAGES_END, P_SVG_END, svg_output / target)))

        # Notes written (84) then split per page (84 -> 88).
        notes_dir = project_path / "notes"
        if (notes_dir / "total.md").is_file():
            pct = max(pct, P_NOTES)
            try:
                split = sum(1 for p in notes_dir.glob("*.md") if p.name != "total.md")
            except OSError:
                split = 0
            if split > 0:
                pct = max(pct, round(_seg(P_NOTES, P_NOTES_SPLIT_END, split / target)))

        # Finalized SVGs (88 -> 97).
        if svg_final > 0:
            pct = max(pct, round(_seg(P_FINAL_END - 9, P_FINAL_END, svg_final / target)))

        # Export underway / done.
        if any((project_path / "exports").glob("*.pptx")) if (project_path / "exports").is_dir() else False:
            pct = max(pct, P_EXPORTING)

    # Never report 100 until the status itself is DONE.
    return int(min(pct, P_EXPORTING))
