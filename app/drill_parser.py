"""Parse Excellon drill files and the Altium DRR drill report."""

import os
import re
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


@dataclass
class DrillHole:
    x_mm: float
    y_mm: float
    diameter_mm: float
    plated: bool = True
    layer_from: str = "Top Layer"     # Altium layer name (from DRR)
    layer_to:   str = "Bottom Layer"  # Altium layer name (from DRR)


# ── DRR parser ─────────────────────────────────────────────────────────────

def parse_drr(drr_path: str) -> Dict[str, Tuple[str, str]]:
    """Parse an Altium .DRR drill report.

    Returns {drill_filename: (from_layer_name, to_layer_name)}.
    Example: {'Fibo_Router-RoundHoles-Plated.TX1': ('Top Layer', 'Int1 (GND)')}
    """
    result: Dict[str, Tuple[str, str]] = {}
    current_pair: Optional[Tuple[str, str]] = None

    try:
        with open(drr_path, "r", errors="ignore") as f:
            for line in f:
                line = line.strip()

                # "Layer Pair : Top Layer to Int1 (GND)"
                m = re.match(r"Layer Pair\s*:\s*(.+?)\s+to\s+(.+)", line, re.IGNORECASE)
                if m:
                    current_pair = (m.group(1).strip(), m.group(2).strip())
                    continue

                # "ASCII Plated RoundHoles File : Fibo_Router-RoundHoles-Plated.TX1"
                m2 = re.search(r"File\s*:\s*(.+)", line, re.IGNORECASE)
                if m2 and current_pair:
                    fname = os.path.basename(m2.group(1).strip())
                    result[fname] = current_pair

    except Exception as exc:
        log.warning("DRR parse failed for %s: %s", drr_path, exc)

    return result


# ── Excellon parser ─────────────────────────────────────────────────────────

def _parse_coord(raw: str, divisor: int, scale: float) -> float:
    """Convert a raw Excellon coordinate string to mm.

    Handles both integer-scaled format (e.g. '184009' with divisor=10000)
    and explicit-decimal format (e.g. '-84.4009' used by EasyEDA Pro).
    """
    if '.' in raw:
        return float(raw) * scale
    return int(raw) / divisor * scale


def _parse_file(filepath: str) -> List[DrillHole]:
    holes: List[DrillHole] = []
    tools: dict = {}
    current_tool: Optional[int] = None
    coord_divisor: int = 10000
    unit_scale: float = 1.0
    plated: bool = True
    in_header: bool = True
    last_x: Optional[float] = None
    last_y: Optional[float] = None

    with open(filepath, "r", errors="ignore") as f:
        lines = f.readlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1

        if not line:
            continue

        if line.startswith(";"):
            m = re.search(r"FILE_FORMAT=(\d+):(\d+)", line)
            if m:
                coord_divisor = 10 ** int(m.group(2))
            if "TYPE=NON_PLATED" in line:
                plated = False
            elif "TYPE=PLATED" in line:
                plated = True
            continue

        if line == "M48":
            in_header = True
            continue
        if line in ("%", "M95"):
            in_header = False
            continue
        if line.startswith("M30") or line.startswith("M00"):
            break

        if in_header:
            if "METRIC" in line:
                unit_scale = 1.0
                # Parse inline format spec: METRIC,LZ,0000.00000
                # Number of digits after decimal → coord_divisor
                mf = re.search(r"\d+\.(\d+)", line)
                if mf:
                    coord_divisor = 10 ** len(mf.group(1))
            elif "INCH" in line:
                unit_scale = 25.4
                mf = re.search(r"\d+\.(\d+)", line)
                if mf:
                    coord_divisor = 10 ** len(mf.group(1))
            m = re.match(r"T(\d+)(?:F[\d.]+S[\d.]+)?C([\d.]+)", line)
            if m:
                tools[int(m.group(1))] = float(m.group(2)) * unit_scale
            continue

        if re.match(r"G9[01]|G05", line):
            continue

        if re.match(r"^T\d+$", line):
            current_tool = int(re.search(r"\d+", line).group())
            continue

        # Slot: G00 → M15 → G01 → M16
        g00 = re.match(r"G00X(-?[\d.]+)Y(-?[\d.]+)", line)
        if g00 and current_tool is not None:
            sx = _parse_coord(g00.group(1), coord_divisor, unit_scale)
            sy = _parse_coord(g00.group(2), coord_divisor, unit_scale)
            ex, ey = sx, sy
            j = i
            while j < len(lines):
                l2 = lines[j].strip()
                j += 1
                m1 = re.match(r"G01X(-?[\d.]+)(?:Y(-?[\d.]+))?", l2)
                m2 = re.match(r"G01Y(-?[\d.]+)", l2)
                if m1:
                    ex = _parse_coord(m1.group(1), coord_divisor, unit_scale)
                    ey = _parse_coord(m1.group(2), coord_divisor, unit_scale) if m1.group(2) else sy
                    break
                elif m2:
                    ey = _parse_coord(m2.group(1), coord_divisor, unit_scale)
                    ex = sx
                    break
                elif l2 == "M16":
                    break
            holes.append(DrillHole((sx + ex) / 2, (sy + ey) / 2,
                                   tools.get(current_tool, 0.3), plated))
            continue

        x_m = re.search(r"X(-?[\d.]+)", line)
        y_m = re.search(r"Y(-?[\d.]+)", line)

        if x_m:
            last_x = _parse_coord(x_m.group(1), coord_divisor, unit_scale)
        if y_m:
            last_y = _parse_coord(y_m.group(1), coord_divisor, unit_scale)

        if (x_m or y_m) and last_x is not None and last_y is not None \
                and current_tool is not None:
            holes.append(DrillHole(last_x, last_y,
                                   tools.get(current_tool, 0.3), plated))

    return holes


def parse_drill_files(
    paths: List[str],
    layer_pairs: Optional[Dict[str, Tuple[str, str]]] = None,
) -> List[DrillHole]:
    """Parse multiple Excellon files.

    If *layer_pairs* (from parse_drr) is provided, each hole gets the
    from/to layer names of its source file instead of the defaults.
    Deduplicates by position at 0.01 mm resolution.
    """
    pairs = layer_pairs or {}
    all_holes: List[DrillHole] = []

    for path in paths:
        fname = os.path.basename(path)
        from_layer, to_layer = pairs.get(fname, ("Top Layer", "Bottom Layer"))
        try:
            file_holes = _parse_file(path)
            for h in file_holes:
                h.layer_from = from_layer
                h.layer_to   = to_layer
            all_holes.extend(file_holes)
        except Exception as exc:
            log.warning("Drill parse failed for %s: %s", path, exc)

    # Deduplicate: same position rounded to 0.01 mm
    # When the same hole appears in multiple files (e.g. TXT + TX*), keep the
    # one with the most specific layer pair (non-through-hole wins).
    seen: Dict[Tuple[float, float], DrillHole] = {}
    for h in all_holes:
        key = (round(h.x_mm, 2), round(h.y_mm, 2))
        if key not in seen:
            seen[key] = h
        else:
            existing = seen[key]
            # Prefer the entry whose span is shorter (more specific / blind/buried)
            existing_span = _layer_span_score(existing)
            new_span      = _layer_span_score(h)
            if new_span < existing_span:
                seen[key] = h

    return list(seen.values())


def _layer_span_score(h: DrillHole) -> int:
    """Lower = more specific (blind/buried). Higher = through-hole."""
    top = "top" in h.layer_from.lower() or "top" in h.layer_to.lower()
    bot = "bottom" in h.layer_from.lower() or "bottom" in h.layer_to.lower()
    if top and bot:
        return 2   # full through-hole
    if top or bot:
        return 1   # blind via
    return 0       # buried via
