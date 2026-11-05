"""
Tract-level total property value.
"""

from __future__ import annotations

import csv
import os
import time
import warnings
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterator, Set, Tuple
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window

SCRIPT_DIR = Path(__file__).resolve().parent
PATH_BASE = SCRIPT_DIR/"data/input_data"

BLOCKS = range(2, 3)

# Height thresholds (metres)
LOW_RISE_MAX = 5.5
MED_RISE_MAX = 22.86

TYPE_NAMES = ("LowRise", "MedRise", "HighRise")
LOW, MED, HIGH = 0, 1, 2

ROWS_PER_CHUNK = 512


# ---------------------------------------------------------------------------
# Raster helpers
# ---------------------------------------------------------------------------
class RasterStack:
    """Opens a set of co-registered rasters and iterates them in row windows."""

    def __init__(self, paths: Dict[str, str]):
        self.paths = paths
        self._handles: Dict[str, rasterio.DatasetReader] = {}

    def __enter__(self) -> "RasterStack":
        for name, path in self.paths.items():
            self._handles[name] = rasterio.open(path)
        shapes = {n: (h.height, h.width) for n, h in self._handles.items()}
        if len(set(shapes.values())) != 1:
            raise ValueError(f"Rasters are not on a common grid: {shapes}")
        self.height, self.width = next(iter(shapes.values()))
        return self

    def __exit__(self, *exc) -> None:
        for h in self._handles.values():
            h.close()
        self._handles.clear()

    def windows(self, rows_per_chunk: int = ROWS_PER_CHUNK) -> Iterator[Window]:
        for row_off in range(0, self.height, rows_per_chunk):
            n_rows = min(rows_per_chunk, self.height - row_off)
            yield Window(0, row_off, self.width, n_rows)

    def read(self, name: str, window: Window) -> np.ndarray:
        """Read a window as float64 with nodata converted to NaN."""
        ds = self._handles[name]
        arr = ds.read(1, window=window).astype(np.float64)
        if ds.nodata is not None and not np.isnan(ds.nodata):
            arr[arr == ds.nodata] = np.nan
        return arr


def classify_height(height: np.ndarray) -> np.ndarray:
    """Vectorized LowRise / MedRise / HighRise classification."""
    codes = np.full(height.shape, HIGH, dtype=np.int8)
    codes[height <= MED_RISE_MAX] = MED
    codes[height < LOW_RISE_MAX] = LOW
    return codes


# ---------------------------------------------------------------------------
# Pass 1: seed the study-area tract universe from the CT CSVs
# ---------------------------------------------------------------------------
@dataclass
class TractUniverse:
    all_geoids: Set[str]
    geoid_median: Dict[str, float]
    geoid_csv_homes: Dict[str, float]
    block_local_to_geoid: Dict[Tuple[str, str], str]


def _safe_float(text: str) -> float:
    try:
        return float(text.strip())
    except (TypeError, ValueError):
        return 0.0


def _try_int(text: str):
    try:
        return int(text.strip())
    except (TypeError, ValueError):
        return None


def seed_tract_universe(blocks=BLOCKS) -> TractUniverse:
    """
    CT CSV columns: 1=tract_id(local), 2=GEOID, 3=median_home_price, 4=total_homes
    """
    all_geoids: Set[str] = set()
    geoid_median: Dict[str, float] = {}
    geoid_csv_homes: Dict[str, float] = {}
    block_local_to_geoid: Dict[Tuple[str, str], str] = {}

    for block_num in blocks:
        block = f"{block_num:02d}"
        csv_path = f"{PATH_BASE}Census_tract/CT{block}.csv"

        if not os.path.isfile(csv_path):
            warnings.warn(f"Missing census CSV, skipping: {csv_path}")
            continue

        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            for cols in csv.reader(fh):
                if not cols or not any(c.strip() for c in cols):
                    continue
                if len(cols) < 2:
                    continue

                # Skip header / malformed rows: column 1 must be an integer
                local_int = _try_int(cols[0])
                if local_int is None:
                    continue

                geoid = cols[1].strip()
                if not geoid:
                    continue

                local_id = str(local_int)

                # Authoritative local-id -> GEOID mapping (per block)
                block_local_to_geoid[(block, local_id)] = geoid
                all_geoids.add(geoid)

                if len(cols) >= 3:
                    geoid_median[geoid] = _safe_float(cols[2])
                if len(cols) >= 4:
                    geoid_csv_homes[geoid] = _safe_float(cols[3])

    return TractUniverse(
        all_geoids=all_geoids,
        geoid_median=geoid_median,
        geoid_csv_homes=geoid_csv_homes,
        block_local_to_geoid=block_local_to_geoid,
    )


# ---------------------------------------------------------------------------
# Cross-block accumulators, all keyed by (type_code, GEOID)
# ---------------------------------------------------------------------------
Key = Tuple[int, str]


@dataclass
class VolumeAccumulators:
    tract_pixels: Dict[Key, float]
    tract_height: Dict[Key, float]
    tract_homes: Dict[Key, Set[str]]   # globally unique "block-bldgid" strings

    @classmethod
    def empty(cls) -> "VolumeAccumulators":
        return cls(defaultdict(float), defaultdict(float), defaultdict(set))

    def type_stats(self, type_code: int, geoid: str) -> Tuple[float, float]:
        """Return (homes, mean_volume) for a type/GEOID pair; zeros if absent."""
        key = (type_code, geoid)
        px = self.tract_pixels.get(key, 0.0)
        hsum = self.tract_height.get(key, 0.0)
        homes = float(len(self.tract_homes.get(key, ())))
        mean_h = hsum / px if px > 0 else 0.0
        mean_vol = (px * mean_h) / homes if homes > 0 else 0.0
        return homes, mean_vol


# ---------------------------------------------------------------------------
# Pass 2: accumulate per-block building/volume stats
# ---------------------------------------------------------------------------
def accumulate_block(
    block: str,
    stack: RasterStack,
    universe: TractUniverse,
    acc: VolumeAccumulators,
) -> None:
    # Local tract id -> GEOID lookup for this block only
    local_map = {
        int(local_id): geoid
        for (blk, local_id), geoid in universe.block_local_to_geoid.items()
        if blk == block
    }
    if not local_map:
        warnings.warn(f"No GEOID mapping for block {block}; nothing accumulated.")
        return

    for window in stack.windows():
        height = stack.read("building_height", window)
        build_id = stack.read("building_id", window)
        tract = stack.read("census_tract", window)

        # predicate()
        valid = (
            ~np.isnan(height)
            & ~np.isnan(build_id)
            & ~np.isnan(tract)
            & (tract > 0.0)
        )
        if not valid.any():
            continue

        h = height[valid]
        ids = np.floor(build_id[valid]).astype(np.int64)
        locals_ = np.floor(tract[valid]).astype(np.int64)
        codes = classify_height(h)

        for local_id in np.unique(locals_):
            geoid = local_map.get(int(local_id))
            if not geoid:
                continue  # unmapped local tract id -> skipped, as in Julia

            tract_mask = locals_ == local_id
            for code in (LOW, MED, HIGH):
                sel = tract_mask & (codes == code)
                n = int(sel.sum())
                if n == 0:
                    continue

                key = (code, geoid)
                acc.tract_pixels[key] += float(n)
                acc.tract_height[key] += float(h[sel].sum())
                acc.tract_homes[key].update(
                    f"{block}-{int(b)}" for b in np.unique(ids[sel])
                )

    print(f"Block {block} building stats accumulated.")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_combined_csv(
    output_filename: str,
    universe: TractUniverse,
    acc: VolumeAccumulators,
) -> None:
    os.makedirs(os.path.dirname(output_filename), exist_ok=True)

    with open(output_filename, "w", newline="", encoding="utf-8") as io:
        io.write(
            "GEOID,Median_Home_Price,CSV_Total_Homes,"
            "Total_Homes_LowRise,Total_Homes_MedRise,Total_Homes_HighRise,"
            "Unit_Value_LowRise,Unit_Value_MedRise,Unit_Value_HighRise,"
            "Total_Value_LowRise,Total_Value_MedRise,Total_Value_HighRise,"
            "Total_Property_Value\n"
        )

        # Iterate over the FULL study-area GEOID list (CSV-seeded)
        for geoid in sorted(universe.all_geoids):
            homes_low, vol_low = acc.type_stats(LOW, geoid)
            homes_med, vol_med = acc.type_stats(MED, geoid)
            homes_high, vol_high = acc.type_stats(HIGH, geoid)

            median_price = universe.geoid_median.get(geoid, 0.0)
            csv_homes = universe.geoid_csv_homes.get(geoid, 0.0)

            # Volume-adjusted per-unit values (relative to LowRise)
            unit_low = median_price
            if vol_low > 0.0:
                unit_med = median_price * (vol_med / vol_low)
                unit_high = median_price * (vol_high / vol_low)
            else:
                unit_med = median_price
                unit_high = median_price

            value_low = unit_low * homes_low
            value_med = unit_med * homes_med
            value_high = unit_high * homes_high
            total_value = value_low + value_med + value_high

            io.write(
                f"{geoid},{median_price},{csv_homes},"
                f"{homes_low},{homes_med},{homes_high},"
                f"{unit_low},{unit_med},{unit_high},"
                f"{value_low},{value_med},{value_high},"
                f"{total_value}\n"
            )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def go(blocks=BLOCKS) -> None:
    # ---- Pass 1: seed the GEOID universe ----
    universe = seed_tract_universe(blocks)

    # ---- Pass 2: accumulate building/volume stats from the rasters ----
    acc = VolumeAccumulators.empty()

    # Skip block 6 related to key west, Florida
    for block_num in blocks:
        if block_num==6:
            continue

        block = f"{block_num:02d}"
        print("\n" + "=" * 40)
        print(f"--- Accumulating building stats for Block: {block} ---")
        print("=" * 40 + "\n")

        paths = {
            "building_height": f"{PATH_BASE}MSBF/Block{block}-Buildings.tif",
            "building_id": f"{PATH_BASE}Building_IDs/Block{block}-Building_IDs.tif",
            "census_tract": f"{PATH_BASE}Census_tract/CT{block}.tif",
        }

        missing = [p for p in paths.values() if not os.path.isfile(p)]
        if missing:
            warnings.warn(f"Missing raster(s) for block {block}, skipping: {missing}")
            continue

        t0 = time.perf_counter()
        with RasterStack(paths) as stack:
            accumulate_block(block, stack, universe, acc)
        print(f"Block {block} evaluated in {time.perf_counter() - t0:.3f} seconds.")

    # ---- Diagnostic: confirm accumulated keys match seeded GEOIDs ----
    acc_geoids = {geoid for (_code, geoid) in acc.tract_pixels}
    print("\n--- Key match diagnostic ---")
    print(f"accumulated GEOIDs: {len(acc_geoids)}")
    print(f"seeded GEOIDs:      {len(universe.all_geoids)}")
    print(f"overlap:            {len(acc_geoids & universe.all_geoids)}")

    # ---- Write ONE combined CSV covering EVERY GEOID in the study area ----
    output_filename = (
            Path(PATH_BASE).parent
            / "Output_data"
            / f"total_property_value_by_census_tract.csv"
    )
    write_combined_csv(output_filename, universe, acc)

    print(f"\nCombined tract-level property value saved to: {output_filename}")
    print(f"Total unique GEOIDs written: {len(universe.all_geoids)}")


if __name__ == "__main__":
    go()