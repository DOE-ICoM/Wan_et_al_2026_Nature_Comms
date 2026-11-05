# Wan et al. (2026) — Coastal Flood Impact Analysis

Code and sample data accompanying **Wan et al., 2026 (*Nature Communications*)**.
This repository provides the end-to-end workflow for estimating **building-level flood
damage losses, inundated population, and census-tract aggregated impacts** from storm
surge scenarios across coastal U.S. study blocks.

The pipeline combines:

- 10 m raster inputs for building footprints/heights (Microsoft Building Footprints,
  MSBF), per-building IDs, census tracts, population, and modeled surge depths; and
- Census-tract median home prices and household counts

to produce building-level damage CSVs, tract-level property-value tables, and a merged
multi-scenario wide-format dataset used for the figures in the paper.

---

## Repository structure

```
Wan_et_al_2026_Nature_Comms/
├── Data/
│   ├── Input_data/
│   │   ├── Building_IDs/            # BlockXX-Building_IDs.tif (per-building integer IDs)
│   │   ├── MSBF/                    # BlockXX-Buildings.tif    (building heights, m)
│   │   ├── Census_tract/            # CTXX.tif  + CTXX.csv     (tract raster + attrs)
│   │   ├── Pop/                     # BlockXX-Building-Pop-YYYY_10m.tif  (2020/2050/2100)
│   │   ├── Surge/Block-XX/          # Surge-XX_HH-AttAA.tif    (surge depth rasters)
│   │   └── CBSA/
│   │       ├── cbsa_to_county_fips.json
│   │       └── tl_2025_us_cbsa/     # East-coast MSA boundary shapefile
│   └── Output_data/
│       ├── Building_level_impact_results/     # building_damage_<block>_<surge>_Att<att>.csv
│       ├── Census_tract_aggregation/          # Property_Value_and_Pop_CT_<surge>_dm_by_type.csv
│       ├── total_property_value_by_census_tract.csv
│       ├── total_flooded_area_all_msas.csv
│       └── Damage_and_Pop_by_CT_WIDE_FORMAT_by_type.csv
├── Scripts/
│   ├── Building_level_impact.py              # Pass 1 — building-level damage & population
│   ├── Total_property_value_by_CT.py         # Tract-level total property value
│   ├── Building_aggregated_to_census_tract.R # Aggregate building CSVs to tract level
│   ├── Merge_all_surge_height_results.R      # Merge all surge-height tract files to wide format
│   ├── MSA_inundated_area_calculation.R      # Per-MSA inundated area from merged surge mosaics
│   └── Figure_plot.ipynb                     # Figures used in the paper
├── renv/                                     # R environment library (managed by renv)
├── requirements.txt                          # Python dependencies
├── renv.lock                                 # R package lockfile
├── Wan_et_al_2026_Nature_Comms.Rproj         # RStudio project (activates renv)
└── .Rprofile                                 # Activates renv on R startup
```

Study blocks are numbered `01`–`15` (block `06`, Key West FL, is intentionally skipped).
Surge heights are encoded in decimeters (`05`, `10`, …, `50` → 0.5 m to 5.0 m).
Attenuation scenarios are named `Att00` and `Att50`.

---

## Data availability

Large raster input datasets (building heights, building IDs, census-tract
rasters, 10 m population rasters, and per-block storm-surge rasters) are **not
stored directly in this GitHub repository** due to file-size limits. They are
archived on **Zenodo**:

> Wan, H. et al. *Input data for Wan et al. (2026), Nature Communications.*
> Zenodo. https://doi.org/10.5281/zenodo.XXXXXXX  *(DOI will be added upon publication)*

After downloading the Zenodo archive, extract it into the repository root so
that the folder layout matches the structure shown above, i.e.:

```
Data/Input_data/Building_IDs/
Data/Input_data/MSBF/
Data/Input_data/Census_tract/
Data/Input_data/Pop/
Data/Input_data/Surge/Block-XX/
Data/Input_data/CBSA/
```

Small metadata files (e.g. `CTXX.csv`, `cbsa_to_county_fips.json`) and all
outputs under `Data/Output_data/` are produced by the scripts in this
repository and are not required to be downloaded separately.

---

## Inputs

All rasters within a given block are expected to be **co-registered on the same 10 m grid**
(the Python pipeline validates this at load time).

| Layer | File | Description |
|---|---|---|
| Building height | `MSBF/Block<BB>-Buildings.tif` | Building height in meters (0 outside buildings) |
| Building IDs | `Building_IDs/Block<BB>-Building_IDs.tif` | Unique integer ID per building |
| Census tracts | `Census_tract/CT<BB>.tif` | Local tract ID raster |
| Census attrs | `Census_tract/CT<BB>.csv` | Columns: `tract_id, GEOID, median_home_price, total_homes` |
| Population | `Pop/Block<BB>-Building-Pop-<YYYY>_10m.tif` | Per-pixel population (2020/2050/2100) |
| Surge depth | `Surge/Block-<BB>/Surge-<BB>_<HH>-Att<AA>.tif` | Flood depth (m) for surge/attenuation scenario |

Building height thresholds used throughout the pipeline:

- **LowRise** (`LowStory`): `< 5.5 m`
- **MedRise** (`MedStory`): `5.5 – 22.86 m`
- **HighRise** (`HighStory`): `> 22.86 m`

Depth–damage curves (percent damage vs. inundation depth) are hard-coded in
`Scripts/Building_level_impact.py` for single-story, two-story, and high-rise
buildings and are evaluated with a natural cubic spline, clamped at the knot range.

---

## Installation

### Python (3.12)

```bash
python -m venv .venv
# Windows:   .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Key packages: `rasterio`, `numpy`, `scipy`, plus `pandas`, `matplotlib`, `seaborn`,
`adjustText` for the figure notebook.

### R

The R scripts use `dplyr`, `data.table`, `purrr`, `tidyr`, `stringr`, `here`,
`terra`, `sf`, `foreach`, and `doParallel`. A reproducible environment is
pinned via `renv.lock` (recreated to include the geospatial/parallel packages
required by `MSA_inundated_area_calculation.R`). From the repository root:

```r
install.packages("renv")
renv::restore()
```

Opening `Wan_et_al_2026_Nature_Comms.Rproj` in RStudio will activate `renv`
automatically via `.Rprofile`. Run R scripts from the project root so that
`here::here()` resolves input/output paths correctly.

---

## Workflow

### 1. Building-level damage and inundated population

```bash
python Scripts/Building_level_impact.py
```

For each block × attenuation × surge-height combination, this:

1. Computes **global per-tract/per-type pixel counts, mean heights, and unique
   building counts** from the static layers (building height, building ID, tract
   raster).
2. Streams the surge raster in windows, classifies buildings by height,
   evaluates the appropriate depth–damage curve pixel-by-pixel, and accumulates
   per-building `damage_sum`, `pixel_count`, `inundated_population`, and a flag
   indicating whether the building was inundated.
3. Converts the damage percent to dollars using the tract's median home price,
   **volume-adjusted** for mid- and high-rise buildings relative to the tract's
   low-rise mean building volume.

Output: `Data/Output_data/Building_level_impact_results/building_damage_<block>_<surge>_Att<att>.csv`

with columns:

```
Building_ID, Census_Tract_ID, Building_Type,
Estimated_Building_Value, Damage_Loss_Dollars, Inundated_Population
```

> ⚠️ Running every block × scenario is time- and I/O-intensive. The default
> `go()` call in the script is set to a small test range. Edit the `blocks`,
> `attenuations`, and `surges` arguments at the bottom of the file to control
> what is processed.

### 2. Tract-level total property value

```bash
python Scripts/Total_property_value_by_CT.py
```

Produces `Data/Output_data/total_property_value_by_census_tract.csv` with
LowRise/MedRise/HighRise home counts, volume-adjusted unit values, and total
property value per GEOID.

### 3. Aggregate building-level results to census tracts (R)

```r
source("Scripts/Building_aggregated_to_census_tract.R")
```

For each surge height, this reads all per-block building damage CSVs,
sums damage dollars and inundated population by tract and by building type
(All / Low / Med / High), joins to each block's `CTXX.csv` for the GEOID, and
writes:

```
Data/Output_data/Census_tract_aggregation/Property_Value_and_Pop_CT_<HH>_dm_by_type.csv
```

### 4. Merge all surge-height scenarios into a wide-format table (R)

```r
source("Scripts/Merge_all_surge_height_results.R")
```

Combines every `Property_Value_and_Pop_CT_<HH>_dm_by_type.csv`, pivots to a
GEOID-indexed wide format, and renames columns (e.g. `A_loss_25` →
`A_25_50`, `A_pop_25` → `Pop_25_2020`) to match the format used by the figure
notebook.

Output: `Data/Output_data/Damage_and_Pop_by_CT_WIDE_FORMAT_by_type.csv`

> ⚠️ `Merge_all_surge_height_results.R` currently contains hard-coded Windows
> paths (`E:/Alfred/Flood_damage/...`). Update `input_dir` and `output_file`
> at the top of the script to point to your local `Data/Output_data/` tree
> before running.

### 5. Per-MSA inundated area (R, parallelized)

```r
source("Scripts/MSA_inundated_area_calculation.R")
```

For each surge height, this script mosaics all per-block surge rasters in
`Data/Input_data/Surge/Block-XX/`, reprojects the mosaic to Albers Equal Area
(EPSG:5070), and computes the total flooded area (km²) within each East-coast
MSA polygon from `Data/Input_data/CBSA/tl_2025_us_cbsa/East_coast_msa.shp`.
Results are written to:

```
Data/Output_data/total_flooded_area_all_msas.csv
```

The script is parallelized across surge heights with `foreach` + `doParallel`
(default `parallel::detectCores() - 4`) and caches intermediate rasters under
`Data/Temp/R_temp/`. All paths are resolved relative to the project root via
`here::here()`.

### 6. Figures

Open `Scripts/Figure_plot.ipynb` in Jupyter/PyCharm and run top-to-bottom to
reproduce the paper figures from the merged wide-format CSV and ancillary
tables in `Data/Output_data/`.

---

## Notes and assumptions

- **Block 06 (Key West, FL)** is deliberately excluded from all processing loops.
- **Volume adjustment**: mid- and high-rise unit values are scaled by the ratio
  of their mean per-building volume to the low-rise mean per-building volume
  within the same tract, so damage dollars reflect building-type differences
  while anchoring to the tract's median single-family home price.
- **Paths**: the Python scripts resolve `PATH_BASE` relative to each script's
  own directory (`Scripts/data/input_data`). If your data lives at
  `Data/Input_data/` (as laid out above), adjust the `PATH_BASE` constant at
  the top of `Building_level_impact.py` and `Total_property_value_by_CT.py`
  accordingly, e.g.:
  ```python
  PATH_BASE = SCRIPT_DIR.parent / "Data" / "Input_data"
  ```

---

## Citation

If you use this code or data, please cite both the paper and the archived
dataset / code release:

> Wan, H. et al. *Title*. **Nature Communications** (2026).
>
> Wan, H. et al. *Input data for Wan et al. (2026), Nature Communications.*
> Zenodo. https://doi.org/10.5281/zenodo.XXXXXXX

Full citations (DOI, BibTeX) will be added upon publication.

---

## Contact

For questions about the code or data, please open an issue on the GitHub
repository or contact the corresponding author.
