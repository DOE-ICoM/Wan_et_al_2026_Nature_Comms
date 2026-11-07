"""
Building-level impact assessment: inundated population & flood damage loss

"""

from __future__ import annotations

import csv
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Set, Tuple
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
from scipy.interpolate import CubicSpline

SCRIPT_DIR = Path(__file__).resolve().parent
PATH_BASE = SCRIPT_DIR/"data/input_data"

# Height thresholds (metres) for building classes
LOW_RISE_MAX = 5.5
MED_RISE_MAX = 22.86

TYPE_NAMES = ("LowRise", "MedRise", "HighRise")
LOW, MED, HIGH = 0, 1, 2

STORY_LABEL = {LOW: "LowStory", MED: "MedStory", HIGH: "HighStory"}


# ---------------------------------------------------------------------------
# Stats container (mirrors BuildingDamageStats + Base.:+)
# ---------------------------------------------------------------------------
@dataclass
class BuildingDamageStats:
    tract_id: float = 0.0
    building_type: str = ""
    damage_sum: float = 0.0
    inundated_pixel_count: int = 0
    is_inundated: bool = False
    inundated_population: float = 0.0

    def __add__(self, other: "BuildingDamageStats") -> "BuildingDamageStats":
        return BuildingDamageStats(
            tract_id=max(self.tract_id, other.tract_id),
            building_type=self.building_type or other.building_type,
            damage_sum=self.damage_sum + other.damage_sum,
            inundated_pixel_count=self.inundated_pixel_count + other.inundated_pixel_count,
            is_inundated=self.is_inundated or other.is_inundated,
            inundated_population=self.inundated_population + other.inundated_population,
        )


IDENTITY_STATS = BuildingDamageStats()


# ---------------------------------------------------------------------------
# Damage percentage curves
# ---------------------------------------------------------------------------
def _make_damage_curve(ys, x_min: float, x_max: float) -> Callable[[np.ndarray], np.ndarray]:
    ys = np.asarray(ys, dtype=np.float64)
    xs = np.linspace(x_min, x_max, len(ys))
    spline = CubicSpline(xs, ys, bc_type="natural")

    def curve(depth) -> np.ndarray:
        d = np.asarray(depth, dtype=np.float64)
        out = spline(np.clip(d, x_min, x_max))
        return np.where(np.isnan(d) | (d < 0.0), 0.0, out)

    return curve


damage_percentage_single_story = _make_damage_curve(
    [0.0, 2.5, 13.4, 23.3, 32.1, 40.1,
     47.1, 53.2, 58.6, 63.2, 67.2,
     70.5, 73.2, 75.4, 77.2, 78.5,
     79.5, 80.2, 80.7],
    0.0, 18.0,
)

damage_percentage_two_story = _make_damage_curve(
    [0.0, 3.0, 9.3, 15.2, 20.9, 26.3,
     31.4, 36.2, 40.7, 44.9, 48.8,
     52.4, 55.7, 58.7, 61.4, 63.8,
     65.9, 67.7, 69.2],
    0.0, 18.0,
)

damage_percentage_high_rise = _make_damage_curve(
    [0.0, 14.25, 15.5, 17.5,
     19.0, 21.5, 22.5, 23.5],
    0.0, 10.0,
)

DAMAGE_CURVES = {
    LOW: damage_percentage_single_story,
    MED: damage_percentage_two_story,
    HIGH: damage_percentage_high_rise,
}


# ---------------------------------------------------------------------------
# Raster helpers
# ---------------------------------------------------------------------------
class RasterStack:
    """Opens a set of co-registered rasters and iterates them in windows."""

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

    def windows(self, rows_per_chunk: int = 512):
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
# Pass 1: global volume / pixel-count statistics
# ---------------------------------------------------------------------------
@dataclass
class GlobalVolumeStats:
    bldg_total_pixels: np.ndarray                      # indexed by building id
    tract_total_pixels: Dict[Tuple[int, int], float] = field(default_factory=dict)
    tract_sum_height: Dict[Tuple[int, int], float] = field(default_factory=dict)
    tract_total_homes: Dict[Tuple[int, int], float] = field(default_factory=dict)

    def pixels(self, type_code: int, tract: int) -> float:
        return self.tract_total_pixels.get((type_code, tract), 0.0)

    def sum_height(self, type_code: int, tract: int) -> float:
        return self.tract_sum_height.get((type_code, tract), 0.0)

    def homes(self, type_code: int, tract: int) -> float:
        return self.tract_total_homes.get((type_code, tract), 0.0)

    def mean_volume(self, type_code: int, tract: int) -> float:
        px = self.pixels(type_code, tract)
        homes = self.homes(type_code, tract)
        if px <= 0 or homes <= 0:
            return 0.0
        mean_height = self.sum_height(type_code, tract) / px
        return (px * mean_height) / homes


def compute_global_stats(stack: RasterStack, max_building_id: int) -> GlobalVolumeStats:
    bldg_pixels = np.zeros(max_building_id + 1, dtype=np.float64)
    tract_pixels: Dict[Tuple[int, int], float] = defaultdict(float)
    tract_heights: Dict[Tuple[int, int], float] = defaultdict(float)
    unique_homes: Dict[Tuple[int, int], Set[int]] = defaultdict(set)

    for window in stack.windows():
        height = stack.read("building_height", window)
        build_id = stack.read("building_id", window)
        tract = stack.read("census_tract", window)

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
        tracts = np.floor(tract[valid]).astype(np.int64)
        codes = classify_height(h)

        # Building-level total pixel count
        bldg_pixels += np.bincount(ids, minlength=bldg_pixels.size)

        # Tract/type-level pixel count, height sum, unique building ids
        for code in (LOW, MED, HIGH):
            sel = codes == code
            if not sel.any():
                continue
            t_sel, h_sel, id_sel = tracts[sel], h[sel], ids[sel]
            for t in np.unique(t_sel):
                m = t_sel == t
                key = (code, int(t))
                tract_pixels[key] += float(m.sum())
                tract_heights[key] += float(h_sel[m].sum())
                unique_homes[key].update(id_sel[m].tolist())

    return GlobalVolumeStats(
        bldg_total_pixels=bldg_pixels,
        tract_total_pixels=dict(tract_pixels),
        tract_sum_height=dict(tract_heights),
        tract_total_homes={k: float(len(v)) for k, v in unique_homes.items()},
    )


# ---------------------------------------------------------------------------
# Pass 2: per-building damage accumulation
# ---------------------------------------------------------------------------
@dataclass
class DamageAccumulator:
    """Array-backed equivalent of the `results["Bldg-<id>"]` dictionary."""
    tract_id: np.ndarray
    damage_sum: np.ndarray
    pixel_count: np.ndarray
    is_inundated: np.ndarray
    population: np.ndarray
    type_code: np.ndarray  # -1 == unset; first inundated pixel wins

    @classmethod
    def allocate(cls, n: int) -> "DamageAccumulator":
        return cls(
            tract_id=np.zeros(n, dtype=np.float64),
            damage_sum=np.zeros(n, dtype=np.float64),
            pixel_count=np.zeros(n, dtype=np.int64),
            is_inundated=np.zeros(n, dtype=bool),
            population=np.zeros(n, dtype=np.float64),
            type_code=np.full(n, -1, dtype=np.int8),
        )

    def stats_for(self, bid: int) -> BuildingDamageStats:
        code = int(self.type_code[bid])
        return BuildingDamageStats(
            tract_id=float(self.tract_id[bid]),
            building_type=TYPE_NAMES[code] if code >= 0 else "",
            damage_sum=float(self.damage_sum[bid]),
            inundated_pixel_count=int(self.pixel_count[bid]),
            is_inundated=bool(self.is_inundated[bid]),
            inundated_population=float(self.population[bid]),
        )


def accumulate_damage(stack: RasterStack, max_building_id: int) -> DamageAccumulator:
    acc = DamageAccumulator.allocate(max_building_id + 1)

    for window in stack.windows():
        height = stack.read("building_height", window)
        build_id = stack.read("building_id", window)
        tract = stack.read("census_tract", window)
        flood = stack.read("flood_depth", window)
        pop = stack.read("inundated_pop", window)

        # predicate(): structural fields must be valid, tract must be positive
        valid = (
            ~np.isnan(height)
            & ~np.isnan(build_id)
            & ~np.isnan(tract)
            & (tract > 0.0)
        )
        # Only inundated pixels contribute anything but the identity element
        wet = valid & ~np.isnan(flood) & (flood > 0.0)
        if not wet.any():
            continue

        ids = np.floor(build_id[wet]).astype(np.int64)
        tracts = tract[wet]
        codes = classify_height(height[wet])
        depth = flood[wet].astype(np.float32)
        pop_vals = np.nan_to_num(pop[wet], nan=0.0)

        damage_pct = np.zeros(depth.shape, dtype=np.float64)
        for code, curve in DAMAGE_CURVES.items():
            sel = codes == code
            if sel.any():
                damage_pct[sel] = curve(depth[sel])
        damage_pct = np.maximum(damage_pct, 0.0)

        n = acc.damage_sum.size
        acc.damage_sum += np.bincount(ids, weights=damage_pct, minlength=n)
        acc.population += np.bincount(ids, weights=pop_vals, minlength=n)
        acc.pixel_count += np.bincount(
            ids, weights=(damage_pct > 0).astype(np.float64), minlength=n
        ).astype(np.int64)

        touched = np.unique(ids)
        acc.is_inundated[touched] = True
        np.maximum.at(acc.tract_id, ids, tracts)  # tract_id = max(...)

        # building_type: first non-empty contribution wins (scan order)
        first_ids, first_pos = np.unique(ids, return_index=True)
        unset = acc.type_code[first_ids] < 0
        acc.type_code[first_ids[unset]] = codes[first_pos[unset]]

    return acc


# ---------------------------------------------------------------------------
# Census tract dictionary
# ---------------------------------------------------------------------------
def load_census_dictionary(csv_path: str) -> Dict[str, list]:
    """Key = first column (tract id), as in Dictionary(..., row -> row[1], true)."""
    table: Dict[str, list] = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # header
        for row in reader:
            if row:
                table[row[0].strip()] = row
    return table


def median_price_of(row: Optional[list]) -> float:
    try:
        return float(row[2])  # Julia row[3] -> Python index 2
    except (TypeError, IndexError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Finish function: volume-adjusted values + CSV writer
# ---------------------------------------------------------------------------
def write_results(
    output_filename: str,
    acc: DamageAccumulator,
    globals_: GlobalVolumeStats,
    census: Dict[str, list],
) -> None:
    os.makedirs(os.path.dirname(output_filename), exist_ok=True)

    with open(output_filename, "w", newline="", encoding="utf-8") as io:
        io.write(
            "Building_ID,Census_Tract_ID,Building_Type,Estimated_Building_Value,"
            "Damage_Loss_Dollars,Inundated_Population\n"
        )

        building_ids = np.flatnonzero(acc.is_inundated | (acc.tract_id != 0.0))

        for bid in building_ids:
            stats = acc.stats_for(int(bid))
            if stats.tract_id == 0.0:
                continue

            tract_int = int(np.floor(stats.tract_id))
            total_building_pixels = float(globals_.bldg_total_pixels[bid])

            # LowRise reference volume for this tract
            mean_vol_low = globals_.mean_volume(LOW, tract_int)

            median_price = median_price_of(census.get(str(tract_int)))
            adjusted_median_price = median_price

            if stats.building_type in ("MedRise", "HighRise"):
                code = MED if stats.building_type == "MedRise" else HIGH
                mean_vol = globals_.mean_volume(code, tract_int)
                if mean_vol_low > 0.0:
                    adjusted_median_price = median_price * (mean_vol / mean_vol_low)

            # Damage = mean damage % over ALL pixels of the building
            damage_dollars = 0.0
            if stats.is_inundated and total_building_pixels > 0:
                mean_damage_pct = stats.damage_sum / total_building_pixels
                damage_dollars = (mean_damage_pct / 100.0) * adjusted_median_price

            story = STORY_LABEL.get(
                TYPE_NAMES.index(stats.building_type) if stats.building_type else HIGH,
                "HighStory",
            )

            io.write(
                f"{int(bid)},{tract_int},{story},{adjusted_median_price},"
                f"{damage_dollars},{stats.inundated_population}\n"
            )

    print(f"Building-level damage analysis saved as CSV: {output_filename}")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def max_building_id(path: str) -> int:
    """Largest building id in the raster (defines accumulator array size)."""
    with rasterio.open(path) as ds:
        nodata = ds.nodata
        best = 0
        for _, window in ds.block_windows(1):
            arr = ds.read(1, window=window).astype(np.float64)
            if nodata is not None and not np.isnan(nodata):
                arr[arr == nodata] = np.nan
            if np.isfinite(arr).any():
                best = max(best, int(np.floor(np.nanmax(arr))))
        return best


def go(blocks=range(1, 16), attenuations=range(2, 3), surges=range(1, 11)): # Only testing for one study block
    for block_num in blocks:

        # Skip block 6 related to key west, Florida
        if block_num == 6:
            continue

        block = f"{block_num:02d}"
        print("\n" + "=" * 40)
        print(f"--- Starting processing for Block: {block} ---")
        print("=" * 40 + "\n")

        building_path = f"{PATH_BASE}MSBF/Block{block}-Buildings.tif"
        building_id_path = f"{PATH_BASE}Building_IDs/Block{block}-Building_IDs.tif"
        census_path = f"{PATH_BASE}Census_tract/CT{block}.tif"
        census_path_csv = f"{PATH_BASE}Census_tract/CT{block}.csv"
        pop_path = f"{PATH_BASE}Pop/Block{block}-Building-Pop-2020_10m.tif"

        census = load_census_dictionary(census_path_csv)
        n_ids = max_building_id(building_id_path)

        # Global volume stats depend only on static layers, so compute once
        # per block instead of once per (surge, attenuation) scenario.
        with RasterStack({
            "building_height": building_path,
            "building_id": building_id_path,
            "census_tract": census_path,
        }) as static_stack:
            global_stats = compute_global_stats(static_stack, n_ids)

        for att in attenuations:
            atten = str(att * 25)

            for s in surges:
                surge = str(s * 5 + 100)[1:]  # "05", "10", ... "50"

                surge_path = (
                    f"{PATH_BASE}Surge/Block-{block}/Surge-{block}_{surge}-Att{atten}.tif"
                )
                output_filename = (
                        Path(PATH_BASE).parent
                        / "Output_data"/"Building_level_impact_results"
                        / f"building_damage_{block}_{surge}_Att{atten}.csv"
                )

                if not os.path.exists(surge_path):
                    print(f"Missing surge raster, skipping: {surge_path}")
                    continue

                print(
                    f"Starting evaluation for Block {block}, "
                    f"surge {surge}, attenuation {atten}..."
                )
                t0 = time.perf_counter()

                with RasterStack({
                    "flood_depth": surge_path,
                    "building_height": building_path,
                    "building_id": building_id_path,
                    "census_tract": census_path,
                    "inundated_pop": pop_path,
                }) as stack:
                    acc = accumulate_damage(stack, n_ids)

                write_results(output_filename, acc, global_stats, census)
                print(f"Evaluation finished in {time.perf_counter() - t0:.3f} seconds.")


if __name__ == "__main__":
    go()