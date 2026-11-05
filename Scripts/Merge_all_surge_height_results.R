# This script is to merge all storm surge height scenario results into one data
# frame, now accounting for building-type breakdowns.
#-------------------------------------------------------------------------------
# 1. SETUP: Load libraries and define paths
#-------------------------------------------------------------------------------
library(dplyr)
library(data.table)
library(purrr)
library(tidyr)
library(stringr)

# --- Configuration ---
# Your new input files should be in this directory
input_dir <- "E:/Alfred/Flood_damage/Results/Census_tract_aggregation_att50"
output_file <- "E:/Alfred/Flood_damage/Results/Damage_and_Pop_by_CT_WIDE_FORMAT_by_type.csv"

#-------------------------------------------------------------------------------
# 2. READ AND COMBINE: Load all CSVs into one "long" data frame
#-------------------------------------------------------------------------------
cat("--- Step 1: Finding and reading all individual CSV files... ---\n")
csv_files <- list.files(
  path = input_dir,
  pattern = "^Property_Value_and_Pop_CT_.*_by_type\\.csv$", # Updated regex still applies
  full.names = TRUE
)

# Read all files, extract surge height from the filename, and stack them.
long_data <- map_dfr(csv_files, function(file_path) {
  surge_height <- str_extract(file_path, "(?<=_CT_)\\d{2}(?=_dm_by_type\\.csv)")
  fread(file_path) %>%
    mutate(Surge_Height = surge_height)
})
cat(paste(" -> Successfully loaded and combined", length(csv_files), "files.\n"))

#-------------------------------------------------------------------------------
# 3. CLEAN AND RESHAPE: Aggregate, complete, and pivot
#-------------------------------------------------------------------------------
cat("\n--- Step 2: Cleaning and Reshaping data... ---\n")

# STEP 3a: AGGREGATE DUPLICATES BY SUMMING
aggregated_long_data <- long_data %>%
  group_by(GEOID, Surge_Height) %>%
  summarise(across(where(is.numeric), sum, na.rm = TRUE), .groups = "drop")
cat(" -> Aggregated any duplicate rows by summing values.\n")

# STEP 3b: PIVOT TO A "SUPER-LONG" FORMAT
super_long_data <- aggregated_long_data %>%
  pivot_longer(
    cols = -c(GEOID, Surge_Height), # Select all columns except GEOID and Surge_Height
    names_to = "metric_name",
    values_to = "value"
  )
cat(" -> Transformed data into a 'super-long' format for robust pivoting.\n")

# STEP 3c: PIVOT TO THE FINAL WIDE FORMAT
systematic_wide_df <- super_long_data %>%
  pivot_wider(
    id_cols = GEOID,
    names_from = c(metric_name, Surge_Height),
    values_from = value,
    values_fill = 0 # Fill any missing combinations with 0
  )
cat(" -> Pivoted data to a wide format with systematic column names.\n")

#-------------------------------------------------------------------------------
# 4. RENAME COLUMNS to match the final desired format
#-------------------------------------------------------------------------------
cat("\n--- Step 3: Renaming columns to final format and saving... ---\n")

final_wide_df <- systematic_wide_df %>%
  rename_with(
    .fn = ~ .x %>%
      # Rule 1: For all loss columns (e.g., "A_loss_05", "L_loss_25")
      str_replace("^(A|L|M|H)_loss_(\\d{2})$", "\\1_\\2_50") %>%
      
      # Rule 2: For Total Population ("A_pop_25")
      str_replace("^A_pop_(\\d{2})$", "Pop_\\1_2020") %>%
      
      # Rule 3: For other population columns ("L_pop_05")
      str_replace("^(L|M|H)_pop_(\\d{2})$", "\\1_pop_\\2_2020"),
    .cols = -GEOID # Apply these renaming rules to all columns except GEOID
  )

# A final check to ensure all NAs (if any sneaked through) are 0
final_wide_df[is.na(final_wide_df)] <- 0

# Write the final data frame to a CSV
fwrite(final_wide_df, output_file)
cat(paste(" -> SUCCESS! Final data frame saved to:\n", output_file, "\n"))

