"""
building_fill.py
================

Purpose
-------
This script generates **height-filled residential building rasters** from raw
Microsoft Building Footprints (MSBF) building-height data. For each study
block, it:

1. Masks out non-residential buildings using OSM-derived parcel and building
   non-residential masks.
2. Estimates missing residential building-height pixels from nearby valid
   residential heights using either a nearest-neighbor search or a
   median-of-neighborhood estimate, with search distances that depend on
   whether the pixel falls inside an urban-area mask.
3. Merges the original MSBF residential height raster, a residential building
   fill raster (used as a presence mask), and the neighbor-derived heights
   into a single filled building-height raster used for the downstream
   damage/exposure analysis.

Inputs
------
The raw inputs required by this script are **placeholders** — they are *not*
distributed with this GitHub repository because of file-size limits and the
licensing of the underlying third-party datasets. To run the script, create
the directory tree shown below under ``Data/Preprocessing/`` at the repo root
and populate it with your own copies of the following per-block rasters
(10 m, co-registered to the same grid as the rest of the pipeline):

    Data/
    └── Preprocessing/
        ├── OSM/
        │   ├── Parcel-NR/BlockXX-Parcel-NR.tif    (OSM non-residential parcel mask)
        │   └── Bldg-NR/BlockXX-Bldg-NR.tif        (OSM non-residential building mask)
        ├── Buildings/BlockXX-Buildings.tif        (prior building-presence raster)
        ├── Buildings_MSBF_Ht/BlockXX-MSBF_Ht.tif  (raw MSBF building heights, m)
        └── Urban-Mask/BlockXX-Urban.tif           (urban-area mask; pixel>0 = urban)

Source of the raw datasets:

- **MSBF building heights** — Microsoft Global ML Building Footprints:
  https://github.com/microsoft/globalmlbuildingfootprints
- **OSM parcel / building non-residential masks** — rasterized OpenStreetMap
  land-use polygons and building footprints with non-residential tags.
- **Urban mask** — e.g. U.S. Census urban areas or an equivalent global
  urban-extent product.
- **Prior building-presence raster** (``Buildings/``) — any binary raster of
  building footprints that pre-dates MSBF, used as a presence mask when MSBF
  misses a pixel.

Intermediate rasters (``Mask-NR``, ``Buildings_Res``, ``MSBF_Res``,
``MSBF_NN``, ``MSBF_Med``) and the final output
(``Buildings_FilledM/BlockXX-Buildings.tif``) are **written by this script**
inside the same ``Data/Preprocessing/`` tree.

The resulting height-filled MSBF rasters (``BlockXX-Buildings.tif``) produced
by this script are the direct input for the downstream analysis pipeline and
are already shipped under ``Data/Input_data/MSBF/`` in this repository, so
users do not need to re-run this preprocessing step to reproduce the paper.

Dependencies
------------
- numpy
- rasterio

Notes
-----
- All paths below are **relative to the repository root** and resolved via
  ``REPO_ROOT`` (the parent of ``Scripts/``). To use a different data
  location, override ``PREPROC_DIR`` with an environment variable or edit it
  directly.
- The nearest-neighbor filler reproduces an "expanding ring" search pattern.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import numpy as np
import rasterio


# ---------------------------------------------------------------------------
# Path configuration (relative to repository root)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
PREPROC_DIR = Path(os.environ.get(
    "BUILDING_FILL_DATA_DIR",
    REPO_ROOT / "Data" / "Preprocessing",
))

# Each path is relative to PREPROC_DIR. ``{block}`` is substituted at runtime.
PATHS = {
    # --- raw inputs (user-supplied; not in this repo) ---
    "parcel_nr":        "OSM/Parcel-NR/Block{block}-Parcel-NR.tif",
    "building_nr":      "OSM/Bldg-NR/Block{block}-Bldg-NR.tif",
    "buildings_all":    "Buildings/Block{block}-Buildings.tif",
    "msbf_ht":          "Buildings_MSBF_Ht/Block{block}-MSBF_Ht.tif",
    "urban_mask":       "Urban-Mask/Block{block}-Urban.tif",
    # --- intermediate rasters written by this script ---
    "mask_nr":          "OSM/Mask-NR/Block{block}-Mask-NR.tif",
    "buildings_res":    "Buildings_Res/Block{block}-Buildings-Res.tif",
    "msbf_res":         "Buildings_MSBF_Ht/Block{block}-MSBF_Res.tif",
    "msbf_nn":          "Buildings_MSBF_Ht/Block{block}-MSBF_NN.tif",
    "msbf_med":         "Buildings_MSBF_Ht/Block{block}-MSBF_Med.tif",
    # --- final output (height-filled residential building raster) ---
    "buildings_filled": "Buildings_FilledM/Block{block}-Buildings.tif",
}


def _p(key: str, block: str) -> str:
    """Resolve a ``PATHS`` key for a given block into an absolute path."""
    return str(PREPROC_DIR / PATHS[key].format(block=block))



# ---------------------------------------------------------------------------
# Raster helpers
# ---------------------------------------------------------------------------
def _read_raster(path: str) -> tuple[np.ndarray, dict]:
    """Read a single-band raster into a float32 array (NaN for no-data)."""
    with rasterio.open(path) as ds:
        arr = ds.read(1).astype(np.float32)
        profile = ds.profile.copy()
        nodata = ds.nodata
    if nodata is not None:
        arr = np.where(arr == nodata, np.nan, arr)
    return arr, profile


def _write_raster(path: str, arr: np.ndarray, profile: dict,
                  nodata: float = 0.0) -> None:
    """Write a single-band float32 raster."""
    out = profile.copy()
    out.update(dtype="float32", count=1, nodata=nodata)
    arr_out = np.where(np.isnan(arr), nodata, arr).astype(np.float32)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **out) as ds:
        ds.write(arr_out, 1)


def _raster_math(path_a: str, path_b: str, path_out: str,
                 fn: Callable[[np.ndarray, np.ndarray], np.ndarray],
                 output_no_data_value: float = 0.0) -> None:
    """Pixel-wise combination of two co-registered rasters."""
    a, profile = _read_raster(path_a)
    b, _ = _read_raster(path_b)
    a0 = np.where(np.isnan(a), 0.0, a)
    b0 = np.where(np.isnan(b), 0.0, b)
    result = fn(a0, b0).astype(np.float32)
    _write_raster(path_out, result, profile, nodata=output_no_data_value)


# ---------------------------------------------------------------------------
# Core fill algorithms
# ---------------------------------------------------------------------------
def raster_nn(raster_in: str, raster_mask: str, raster_out: str,
              max_mask: int, max_non: int) -> None:
    """
    Fill no-data pixels in ``raster_in`` with the value of the nearest valid
    (>0) neighbor. Search distance depends on ``raster_mask`` (urban mask):

    - mask > 0  -> search up to ``max_mask`` pixels
    - mask <= 0 -> search up to ``max_non`` pixels
    """
    data, profile = _read_raster(raster_in)
    mask, _ = _read_raster(raster_mask)
    mask0 = np.where(np.isnan(mask), 0.0, mask)
    nrows, ncols = data.shape
    result = data.copy()

    missing = np.argwhere(np.isnan(data))
    total = len(missing)
    print(f"  raster_nn: filling {total} missing pixels...")

    for k, (j, i) in enumerate(missing):
        max_search = max_mask if mask0[j, i] > 0 else max_non
        found_val = np.nan
        for dist in range(1, max_search):
            # cardinal directions
            for jj, ii in (
                (j - dist, i), (j + dist, i),
                (j, i - dist), (j, i + dist),
            ):
                if 0 <= jj < nrows and 0 <= ii < ncols:
                    v = data[jj, ii]
                    if v > 0.0:
                        found_val = v
                        break
            if not np.isnan(found_val):
                break
            # ring (centers to corners)
            top, bot = j - dist, j + dist
            lef, rit = i - dist, i + dist
            stop = False
            for d in range(1, dist + 1):
                lc, rc = i - d, i + d
                tr, br = j - d, j + d
                candidates = [
                    (top, lc), (top, rc), (bot, lc), (bot, rc),
                    (tr, lef), (br, lef), (tr, rit), (br, rit),
                ]
                for jj, ii in candidates:
                    if 0 <= jj < nrows and 0 <= ii < ncols:
                        v = data[jj, ii]
                        if v > 0.0:
                            found_val = v
                            stop = True
                            break
                if stop:
                    break
            if stop:
                break
        result[j, i] = found_val

        if total and (k + 1) % max(1, total // 20) == 0:
            print(f"    {100 * (k + 1) // total}%")

    _write_raster(raster_out, result, profile)


def raster_median(raster_in: str, raster_mask: str, raster_out: str,
                  max_mask: int, max_non: int) -> None:
    """
    Fill no-data pixels in ``raster_in`` with the median of positive values
    inside a square neighborhood. Window half-size depends on ``raster_mask``:

    - mask > 0  -> half-size ``max_mask``
    - mask <= 0 -> half-size ``max_non``
    """
    data, profile = _read_raster(raster_in)
    mask, _ = _read_raster(raster_mask)
    mask0 = np.where(np.isnan(mask), 0.0, mask)
    nrows, ncols = data.shape
    result = data.copy()

    missing = np.argwhere(np.isnan(data))
    total = len(missing)
    print(f"  raster_median: filling {total} missing pixels...")

    for k, (j, i) in enumerate(missing):
        r = max_mask if mask0[j, i] > 0 else max_non
        r0 = max(0, j - r)
        r1 = min(nrows, j + r + 1)
        c0 = max(0, i - r)
        c1 = min(ncols, i + r + 1)
        window = data[r0:r1, c0:c1]
        valid = window[window > 0]
        if valid.size:
            result[j, i] = float(np.median(valid))
        else:
            result[j, i] = np.nan

        if total and (k + 1) % max(1, total // 20) == 0:
            print(f"    {100 * (k + 1) // total}%")

    _write_raster(raster_out, result, profile)


def building_merge(raster_bldg: str, raster_fill: str, raster_nn_path: str,
                   raster_out: str) -> None:
    """
    Merge the MSBF residential height raster with the fill-presence raster and
    the neighbor-derived height raster.

    - If the MSBF pixel has a valid height, keep it.
    - Else if the fill raster indicates a building is present, use the
      neighbor-derived height (nearest-neighbor or median raster); set to -1
      if no neighbor height is available (not in an urban area).
    - Else leave as no-data.
    """
    bldg, profile = _read_raster(raster_bldg)
    fill, _ = _read_raster(raster_fill)
    nn, _ = _read_raster(raster_nn_path)

    result = bldg.copy()
    missing = np.isnan(bldg)
    fill0 = np.where(np.isnan(fill), 0.0, fill)

    has_fill = missing & (fill0 > 0)
    nn_vals = np.where(np.isnan(nn), -1.0, nn)
    nn_vals = np.where(nn_vals <= 0, -1.0, nn_vals)
    result[has_fill] = nn_vals[has_fill]

    _write_raster(raster_out, result, profile)


# ---------------------------------------------------------------------------
# Block-level pipeline steps
# ---------------------------------------------------------------------------
def mask_merge(block: str) -> None:
    print(f"[{block}] Merging non-residential parcel and building masks...")
    _raster_math(
        _p("parcel_nr", block),
        _p("building_nr", block),
        _p("mask_nr", block),
        lambda m1, m2: ((m1 > 0) | (m2 > 0)).astype(np.float32),
        output_no_data_value=0.0,
    )


def mask_building_old(block: str) -> None:
    print(f"[{block}] Masking old residential buildings...")
    _raster_math(
        _p("buildings_all", block),
        _p("mask_nr", block),
        _p("buildings_res", block),
        lambda bldg, parc: np.where(parc > 0, 0.0, bldg),
        output_no_data_value=0.0,
    )


def mask_building_new(block: str) -> None:
    print(f"[{block}] Masking new residential buildings (MSBF)...")
    _raster_math(
        _p("msbf_ht", block),
        _p("mask_nr", block),
        _p("msbf_res", block),
        lambda bldg, parc: np.where(parc > 0, 0.0, bldg),
        output_no_data_value=0.0,
    )


def nearest_neighbor(block: str) -> None:
    print(f"[{block}] Nearest-neighbor fill for MSBF residential buildings...")
    raster_nn(
        _p("msbf_res", block),
        _p("urban_mask", block),
        _p("msbf_nn", block),
        max_mask=95, max_non=5,
    )


def median_neighbor(block: str) -> None:
    print(f"[{block}] Median-neighbor fill for MSBF residential buildings...")
    raster_median(
        _p("msbf_res", block),
        _p("urban_mask", block),
        _p("msbf_med", block),
        max_mask=80, max_non=5,
    )


def merge_block(block: str) -> None:
    print(f"[{block}] Merging final filled building raster...")
    building_merge(
        _p("msbf_res", block),
        _p("buildings_res", block),
        _p("msbf_med", block),  # or msbf_nn
        _p("buildings_filled", block),
    )



def fill_building(block: str) -> None:
    """Full pipeline for a single block."""
    mask_merge(block)
    mask_building_old(block)
    mask_building_new(block)
    median_neighbor(block)   # or nearest_neighbor(block)
    merge_block(block)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    blocks = [f"{i:02d}" for i in range(1, 16)]
    for b in blocks:
        fill_building(b)
