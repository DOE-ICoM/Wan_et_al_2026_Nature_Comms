#-------------------------------------------------------------------------------
# SCRIPT TO CALCULATE FLOODED AREA FROM SURGE RASTERS
# (All EAST COAST MSAs - Merge-Then-Reproject, Parallelized)
#-------------------------------------------------------------------------------

#-------------------------------------------------------------------------------
# 1. SET TEMP DIR AND DEFINE PROJ PATHS (CRITICAL FOR GDAL/PROJ ERRORS)
#-------------------------------------------------------------------------------
# Load libraries
library(terra)
library(sf)
library(stringr)
library(dplyr)
library(foreach)
library(doParallel)
library(here)

# Repo-relative temp directory (keeps all scratch files inside the project)
custom_temp_dir <- here::here("Data", "Temp", "R_temp")

# Create the directory if it doesn't exist
if (!dir.exists(custom_temp_dir)) {
  dir.create(custom_temp_dir, recursive = TRUE)
}

# Set R's TMP environment variables BEFORE heavy raster work
Sys.setenv(TMP = custom_temp_dir)
Sys.setenv(TEMP = custom_temp_dir)
Sys.setenv(TMPDIR = custom_temp_dir)

# *** CRITICAL: Locate and set PROJ database paths to fix "Cannot find proj.db" ***
set_proj_paths <- function() {
  proj_path <- system.file("proj", package = "sf")
  if (proj_path == "") {
    proj_path <- system.file("proj", package = "terra")
  }
  if (proj_path != "") {
    Sys.setenv(PROJ_LIB = proj_path)
    Sys.setenv(PROJ_DATA = proj_path)
    options(sf_proj_network = "FALSE")
    return(proj_path)
  }
  return(NULL)
}
active_proj_path <- set_proj_paths()
cat("PROJ database set to:", active_proj_path, "\n")

# Set terra's tempdir specifically (in master session)
terra::terraOptions(tempdir = custom_temp_dir)

#-------------------------------------------------------------------------------
# 2. CONFIGURATION - - - - 
#-------------------------------------------------------------------------------
# --- Main Flood Inundation Data Path (surge rasters, organized as Block-XX/) ---
base_path <- here::here("Data", "Input_data", "Surge")

# --- MSA Shapefile Path ---
msa_shapefile_path <- here::here(
  "Data", "Input_data", "CBSA", "tl_2025_us_cbsa", "East_coast_msa.shp"
)

# Column with human-readable MSA name
MSA_NAME_FIELD_NAME <- "NAMELSAD"

# Storm surge heights (YY values)
surge_heights_yy <- seq(from = 5, to = 50, by = 5)

# Attenuation value (ZZ)
attenuation_zz <- 50

# --- Parallelization Settings ---
num_cores <- parallel::detectCores() - 4
if (num_cores < 1) num_cores <- 1
cat(paste0("Using ", num_cores, " cores for parallel processing.\n"))

# --- Output CSV Path ---
output_csv_path <- here::here("Data", "Output_data", "total_flooded_area_all_msas.csv")
if (!dir.exists(dirname(output_csv_path))) {
  dir.create(dirname(output_csv_path), recursive = TRUE)
}

#-------------------------------------------------------------------------------
# 3. LOAD ALL MSAS, DISCOVER BLOCKS, AND SET CRS
#-------------------------------------------------------------------------------
if (!file.exists(msa_shapefile_path)) {
  stop(paste("Comprehensive MSA shapefile not found at:", msa_shapefile_path))
}

# Read the shapefile (master session)
all_msas_sf <- sf::st_read(msa_shapefile_path, quiet = TRUE)

# Validate required field
if (!MSA_NAME_FIELD_NAME %in% names(all_msas_sf)) {
  stop(paste("Required MSA_NAME_FIELD_NAME '", MSA_NAME_FIELD_NAME, 
             "' not found in '", basename(msa_shapefile_path), "' attributes.", sep=""))
}

# Discover all available Block IDs
cat("Discovering Block IDs from base path subdirectories...\n")
block_dirs <- list.dirs(base_path, full.names = FALSE, recursive = FALSE)
block_pattern <- "^Block-(\\d+)$"
all_available_block_ids <- stringr::str_extract(block_dirs, block_pattern, group = 1) %>%
  na.omit() %>%
  unique() %>%
  sort()

if (length(all_available_block_ids) == 0) {
  stop("No 'Block-XX' directories found in the specified base_path.")
} else {
  cat(paste("Found Block IDs:", paste(all_available_block_ids, collapse = ", "), "\n"))
}

# *** BYPASS LOOKUP: Hardcode PROJ.4 representation of Albers Equal Area (EPSG:5070) ***
albers_crs_wkt <- "+proj=aea +lat_1=29.5 +lat_2=45.5 +lat_0=23 +lon_0=-96 +x_0=0 +y_0=0 +ellps=GRS80 +datum=NAD83 +units=m +no_defs"
cat("Albers CRS PROJ.4 definition loaded successfully.\n")

#-------------------------------------------------------------------------------
# 4. MAIN PROCESSING LOOP (Parallelized per Surge Height)
#-------------------------------------------------------------------------------

# Register the parallel backend
my_cluster <- parallel::makeCluster(num_cores)
doParallel::registerDoParallel(my_cluster)

# Export variables to all workers
parallel::clusterExport(my_cluster, c("msa_shapefile_path", "MSA_NAME_FIELD_NAME",
                                      "albers_crs_wkt", "custom_temp_dir", "active_proj_path",
                                      "base_path"))

# Set up each worker
parallel::clusterEvalQ(my_cluster, {
  Sys.setenv(TMP = custom_temp_dir)
  Sys.setenv(TEMP = custom_temp_dir)
  Sys.setenv(TMPDIR = custom_temp_dir)
  
  if (!is.null(active_proj_path)) {
    Sys.setenv(PROJ_LIB = active_proj_path)
    Sys.setenv(PROJ_DATA = active_proj_path)
  }
  
  library(terra)
  library(sf)
  library(dplyr)
  library(stringr)
  
  # Create unique subdirectory for THIS worker
  worker_pid <- Sys.getpid()
  worker_tempdir <- file.path(custom_temp_dir, paste0("worker_", worker_pid))
  if (!dir.exists(worker_tempdir)) {
    dir.create(worker_tempdir, recursive = TRUE)
  }
  
  # Configure terra for this worker
  terra::terraOptions(tempdir = worker_tempdir, memfrac = 0.5)
  worker_tempdir <<- worker_tempdir
  
  # Load shapefile on each worker (avoids external pointer issues)
  local_all_msas_sf <<- sf::st_read(msa_shapefile_path, quiet = TRUE)
  
  cat(paste0("[Worker ", worker_pid, "] Setup complete. Temp dir: ", worker_tempdir, "\n"))
})

# Parallelized loop over surge heights (variables implicitly exported)
all_results_list <- foreach(surge_val = surge_heights_yy,
                            .combine = 'bind_rows',
                            .packages = c("terra", "sf", "stringr", "dplyr")) %dopar% {
                              
                              temp_results_for_surge <- data.frame(
                                MSA = character(),
                                SurgeHeight = integer(),
                                FloodedArea_ksq = numeric(),
                                stringsAsFactors = FALSE
                              )
                              
                              surge_str_yy <- sprintf("%02d", surge_val)
                              worker_pid <- Sys.getpid()
                              cat(paste0("[Worker ", worker_pid, "] --- Processing Surge Height: ", surge_val, " ---\n"))
                              
                              # ==========================================================================
                              # Step 4a: Collect all native rasters (They align perfectly out of the box)
                              # ==========================================================================
                              rasters_to_merge_list <- list()
                              valid_rasters_found_for_surge <- FALSE
                              
                              for (block_id in all_available_block_ids) {
                                tif_name <- paste0("Surge-", block_id, "_", surge_str_yy, "-Att", attenuation_zz, ".tif")
                                tif_path <- file.path(base_path, paste0("Block-", block_id), tif_name)
                                
                                if (file.exists(tif_path)) {
                                  rasters_to_merge_list[[length(rasters_to_merge_list) + 1]] <- terra::rast(tif_path)
                                  valid_rasters_found_for_surge <- TRUE
                                }
                              }
                              
                              # ==========================================================================
                              # Step 4b: Merge, then Reproject the single merged mosaic
                              # ==========================================================================
                              merged_surge_raster_albers <- NULL
                              
                              if (valid_rasters_found_for_surge) {
                                
                                # 1. Merge native rasters (safe, fast, and mathematically aligned)
                                merged_raster_native <- if (length(rasters_to_merge_list) > 1) {
                                  cat(paste0("[Worker ", worker_pid, "]   - Merging ", length(rasters_to_merge_list), " native rasters...\n"))
                                  
                                  mosaic_tempfile <- file.path(worker_tempdir, 
                                                               paste0("mosaic_s", surge_str_yy, "_p", worker_pid, ".tif"))
                                  
                                  tryCatch({
                                    rast_collection <- terra::sprc(rasters_to_merge_list)
                                    terra::mosaic(rast_collection, fun = "max", filename = mosaic_tempfile, overwrite = TRUE)
                                  }, error = function(e) {
                                    cat(paste0("[Worker ", worker_pid, "]   - MOSAIC ERROR: ", e$message, "\n"))
                                    NULL
                                  })
                                } else {
                                  rasters_to_merge_list[[1]] # Only 1 block found
                                }
                                
                                # 2. Reproject the single merged raster to Albers CRS (no alignment issues)
                                if (!is.null(merged_raster_native)) {
                                  cat(paste0("[Worker ", worker_pid, "]   - Reprojecting integrated mosaic to Albers CRS...\n"))
                                  
                                  reproj_tempfile <- file.path(worker_tempdir, 
                                                               paste0("reproj_s", surge_str_yy, "_p", worker_pid, ".tif"))
                                  
                                  merged_surge_raster_albers <- tryCatch({
                                    terra::project(merged_raster_native, albers_crs_wkt, method = "near",
                                                   filename = reproj_tempfile, overwrite = TRUE)
                                  }, error = function(e) {
                                    cat(paste0("[Worker ", worker_pid, "]   - REPROJECTION ERROR: ", e$message, "\n"))
                                    NULL
                                  })
                                }
                                
                              } else {
                                cat(paste0("[Worker ", worker_pid, "]  - No valid flood rasters found for Surge Height ", surge_val, ". Setting flooded area to 0.\n"))
                              }
                              
                              # Fail-safe check: If merging or projecting failed, record NA and proceed to next task
                              if (is.null(merged_surge_raster_albers)) {
                                for (j in 1:nrow(local_all_msas_sf)) {
                                  msa_name <- as.character(local_all_msas_sf[j, MSA_NAME_FIELD_NAME, drop = TRUE])
                                  new_row <- data.frame(
                                    MSA = msa_name,
                                    SurgeHeight = surge_val,
                                    FloodedArea_ksq = NA,
                                    stringsAsFactors = FALSE
                                  )
                                  temp_results_for_surge <- dplyr::bind_rows(temp_results_for_surge, new_row)
                                }
                                return(temp_results_for_surge)
                              }
                              
                              # ==========================================================================
                              # Step 4c: Calculate Flooded Area for each MSA
                              # ==========================================================================
                              for (i in 1:nrow(local_all_msas_sf)) {
                                current_msa_feature <- local_all_msas_sf[i, ]
                                msa_name <- as.character(current_msa_feature[[MSA_NAME_FIELD_NAME]])
                                
                                total_flood_area_ksq <- 0
                                
                                tryCatch({
                                  # Transform current MSA feature to Albers CRS
                                  study_area_sf_albers <- sf::st_transform(current_msa_feature, crs = albers_crs_wkt)
                                  
                                  # Crop raster to bounds
                                  raster_cropped_to_msa <- terra::crop(merged_surge_raster_albers, study_area_sf_albers)
                                  
                                  # Mask raster to geometry
                                  raster_masked_to_msa <- terra::mask(raster_cropped_to_msa, study_area_sf_albers)
                                  
                                  # Identify flooded pixels
                                  flooded_cells <- raster_masked_to_msa > 0.001
                                  
                                  # Calculate resolution dimensions
                                  pixel_dims <- terra::res(flooded_cells)
                                  area_per_pixel <- pixel_dims[1] * pixel_dims[2]
                                  
                                  # Calculate count of active flooded pixels
                                  flooded_pixel_count <- terra::global(flooded_cells, fun = "sum", na.rm = TRUE)
                                  
                                  # Compute area in square kilometers
                                  total_flood_area_ksq <- flooded_pixel_count[[1]] * area_per_pixel / 1000000
                                  
                                }, error = function(e) {
                                  cat(paste0("[Worker ", worker_pid, "]     ERROR calculating area for MSA ", 
                                             msa_name, " at Surge Height ", surge_val, ": ", e$message, "\n"))
                                  total_flood_area_ksq <- 0
                                })
                                
                                new_row <- data.frame(
                                  MSA = msa_name,
                                  SurgeHeight = surge_val,
                                  FloodedArea_ksq = total_flood_area_ksq,
                                  stringsAsFactors = FALSE
                                )
                                temp_results_for_surge <- dplyr::bind_rows(temp_results_for_surge, new_row)
                              }
                              
                              # ==========================================================================
                              # Step 4d: Cleanup temporary files for this worker thread to save space
                              # ==========================================================================
                              tmp_files_to_delete <- list.files(worker_tempdir, 
                                                                pattern = paste0("_s", surge_str_yy, "_p", worker_pid), 
                                                                full.names = TRUE)
                              if (length(tmp_files_to_delete) > 0) {
                                file.remove(tmp_files_to_delete)
                              }
                              
                              return(temp_results_for_surge)
                            }

# Stop the parallel cluster when completed
parallel::stopCluster(my_cluster)

# Assign combined list results to the main results dataframe
results_df <- all_results_list

#-------------------------------------------------------------------------------
# 5. VIEW AND SAVE RESULTS
#-------------------------------------------------------------------------------
cat("\n--- Processing Complete! ---\n\n")

# Print the final results data frame to the console
print(results_df)

# Save the consolidated table to the specified E: drive path
write.csv(results_df, output_csv_path, row.names = FALSE)

cat(paste0("\nResults have been saved to: ", output_csv_path, "\n"))