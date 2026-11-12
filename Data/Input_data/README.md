# Input data

The input datasets used by this repository are **not stored in
GitHub**. They are archived on the **MSD-LIVE Data Repository**:

> Wan, H. et al. (2026) *Wan_et_al_2026_Nature_Comms (Version v1) [Data set].*
> MSD-LIVE Data Repository. https://doi.org/10.57931/17427421

After downloading the archive, extract it into this folder so that the
contents are organized as follows:

```
Data/Input_data/
├── Building_IDs/            # Per-block building-ID rasters (Block<BB>-Building_IDs.tif)
├── MSBF/                    # Per-block height-filled building-height rasters (Block<BB>-Buildings.tif)
├── Census_tract/            # Per-block census-tract ID rasters (CT<BB>.tif) and attribute CSVs (CT<BB>.csv)
├── Pop/                     # Per-block 10 m population rasters (Block<BB>-Building-Pop-<YYYY>_10m.tif)
├── Surge/
│   └── Block-<BB>/          # Per-block storm-surge inundation-depth rasters
│                            # (Surge-<BB>_<HH>-Att<AA>.tif), 10 surge heights (05–50 dm)
└── CBSA/
    ├── cbsa_to_county_fips.json
    └── tl_2025_us_cbsa/     # East-coast MSA polygons (East_coast_msa.*)
```

Study blocks are numbered `01`–`15`; block `06` (Florida Keys) is excluded
because MSBF building-height coverage is too sparse there to support
reliable damage/exposure estimates (see the main `README.md` in the
repository root).

All rasters within a given block are co-registered on the same 10 m grid;
the Python pipeline validates this at load time.

Please refer to the **Data availability** section of the main repository
`README.md` for a full description of each sub-folder (contents, formats,
and usage).
