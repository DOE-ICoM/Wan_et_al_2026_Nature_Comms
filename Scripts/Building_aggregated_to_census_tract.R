# This script is to aggregate flood damage loss and inundated population 
# from building-level to census tract-level

#-------------------------------------------------------------------------------
# 1. SETUP: Load libraries and define parameters
#-------------------------------------------------------------------------------
# Load necessary libraries
library(dplyr)
library(data.table)
library(purrr)
library(here)

# --- Configuration ---
input_dir <- here("Data", "Input_data")
output_dir <- here("Data", "Output_data")

damage_dir <- file.path(output_dir, "Building_level_impact_results")
agg_dir    <- file.path(output_dir, "Census_tract_aggregation")
ct_dir     <- file.path(input_dir,  "Census_tract")

# Create the output directory if it doesn't exist to avoid errors
if (!dir.exists(agg_dir)) {
  dir.create(agg_dir, recursive = TRUE)
}

# Create a vector of block numbers (01 to 15, excluding 06)
blocks <- sprintf("%02d", setdiff(1:15, 6))

# Create a vector of storm surge heights to process (05 to 50 dm)
surge_heights <- sprintf("%02d", seq(from = 5, to = 50, by = 5))

#-------------------------------------------------------------------------------
# 2. MAIN PROCESSING: Outer loop for each surge height
#-------------------------------------------------------------------------------
purrr::walk(surge_heights, function(current_height) {
  
  cat(paste0("\n--- Processing All Blocks for Surge Height: ", current_height, " dm ---\n"))
  
  # --- 2a. Inner loop: Process all blocks for THIS surge height ---
  data_for_one_height <- map_dfr(blocks, function(block_id) {
    
    # Construct file names dynamically
    damage_file <- file.path(damage_dir, paste0("building_damage_", block_id, "_", current_height, "_Att50.csv"))
    ct_file     <- file.path(ct_dir, paste0("CT", block_id, ".csv"))
    
    # Check for files
    if (!file.exists(damage_file) | !file.exists(ct_file)) {
      warning(paste("  - Files missing for block", block_id, "- skipping."))
      return(NULL)
    }
    
    # Read damage data
    damage_df <- fread(damage_file)
    
    # Check if damage_df is empty after reading
    if (nrow(damage_df) == 0) {
      return(NULL)
    }
    
    # Group by census tract and perform conditional aggregation for each building type.
    aggregated_damage_df <- damage_df %>%
      group_by(Census_Tract_ID) %>%
      summarise(
        # --- Damage Loss Columns ---
        A_loss = sum(Damage_Loss_Dollars, na.rm = TRUE),
        L_loss = sum(Damage_Loss_Dollars[Building_Type == "LowStory"], na.rm = TRUE),
        M_loss = sum(Damage_Loss_Dollars[Building_Type == "MedStory"], na.rm = TRUE),
        H_loss = sum(Damage_Loss_Dollars[Building_Type == "HighStory"], na.rm = TRUE),
        
        # --- Exposed Population Columns ---
        A_pop = sum(Inundated_Population, na.rm = TRUE),
        L_pop = sum(Inundated_Population[Building_Type == "LowStory"], na.rm = TRUE),
        M_pop = sum(Inundated_Population[Building_Type == "MedStory"], na.rm = TRUE),
        H_pop = sum(Inundated_Population[Building_Type == "HighStory"], na.rm = TRUE),
        
        .groups = "drop"
      )
    
    # Read CT file and join
    ct_df <- fread(ct_file)
    joined_df <- aggregated_damage_df %>%
      left_join(ct_df, by = c("Census_Tract_ID" = "tract_id")) %>%
      # Select GEOID and all the new columns we just created
      select(
        GEOID,
        A_loss, L_loss, M_loss, H_loss,
        A_pop, L_pop, M_pop, H_pop
      )
    
    return(joined_df)
  })
  
  # --- 2b. Save the combined data for the current surge height ---
  if (nrow(data_for_one_height) > 0) {
    # Construct a dynamic output file name
    output_filename <- paste0("Property_Value_and_Pop_CT_", current_height, "_dm_by_type.csv") # Updated name
    output_path <- file.path(agg_dir, output_filename)
    
    # Write the data frame to its own CSV file
    fwrite(data_for_one_height, output_path)
    
    cat(paste0(" -> Successfully saved ", nrow(data_for_one_height), " rows to: ", output_filename, "\n"))
  } else {
    cat(paste0(" -> No data found for surge height ", current_height, " dm. No file written.\n"))
  }
  
}) # End of the primary purrr::walk loop

cat("\n--- All processing complete. ---\n")