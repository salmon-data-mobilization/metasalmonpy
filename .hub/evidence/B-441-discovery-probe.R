# B-441 evidence: the semantic targets metasalmon discovers for B-440's frames.
#
# The R twin of B-441-discovery-probe.py, printing the same tab-separated
# `Q column field query` rows. Pass a metasalmon checkout:
#     Rscript .hub/evidence/B-441-discovery-probe.R <metasalmon checkout>
args <- commandArgs(trailingOnly = TRUE)
suppressMessages(pkgload::load_all(args[[1]], quiet = TRUE))

edge_names <- c(
  "fishWeightKg", "forkLengthCm", "body_mass_kg", "water_depth_meters",
  "fork_length_cm", "length_mm", "mm", "total_weight_kg", "mean_length_mm",
  "temp_c", "water_temp_degC", "flow_cms", "area_m2", "length_in_metres",
  "egg_diameter_mm", "girth_cm", "smolt_weight_g", "fish_number_length_mm",
  "catch_kg", "gauge_height_m"
)

rows <- function(df, force = character(), fields = NULL, all_measurement = FALSE) {
  dict <- infer_dictionary(df, dataset_id = "d1", table_id = "trawl")
  if (all_measurement) dict$column_role <- "measurement"
  dict$column_role[dict$column_name %in% force] <- "measurement"
  targets <- metasalmon:::.ms_semantic_discover_targets(
    dict = dict, codes = tibble::tibble(), table_meta = tibble::tibble(),
    dataset_meta = tibble::tibble(), resource_lookup = list(trawl = df)
  )
  for (i in seq_len(nrow(targets))) {
    if (is.null(fields) || targets$target_sdp_field[[i]] %in% fields) {
      cat("Q", targets$column_name[[i]], targets$target_sdp_field[[i]],
          targets$search_query[[i]], sep = "\t")
      cat("\n")
    }
  }
}

rows(tibble::tibble(
  fish_length_mm = c(512, 600.5, 455),
  fish_weight_g = c(1500.2, 2100, 980),
  depth_m = c(12.5, 30, 45.1),
  fish_count = c(3L, 5L, 2L)
))
rows(tibble::tibble(fish_length = c(512, 600.5, 455)))
rows(tibble::tibble(
  spawners_m = c(10L, 12L, 9L),
  catch_g = c(4.5, 2.1, 3.3),
  weight_m = c(1.2, 3.4, 5.6)
))
compound <- c("mercury_mg/kg", "mercury_mg_kg", "eggs_per_kg", "fish_length_per_m")
rows(tibble::tibble(
  `mercury_mg/kg` = c(0.12, 0.30, 0.08),
  mercury_mg_kg = c(0.12, 0.30, 0.08),
  eggs_per_kg = c(1500, 2100, 1800),
  fish_length_per_m = c(0.2, 0.4, 0.3)
), force = compound)
cat("--\n")
rows(
  tibble::as_tibble(stats::setNames(lapply(edge_names, function(n) c(1.5, 2.5, 3.5)), edge_names)),
  fields = c("term_iri", "unit_iri"),
  all_measurement = TRUE
)
