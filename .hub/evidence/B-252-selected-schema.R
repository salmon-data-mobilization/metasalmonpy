# Run from the Python task checkout, passing the R checkout as the first arg.
# Only temporary packages are written; the selected remote loader is stubbed.
args <- commandArgs(trailingOnly = TRUE)
stopifnot(length(args) == 1L)
pkgload::load_all(args[[1L]], quiet = TRUE)
configured <- .ms_vendored_sdp_schema()
for (pair in list(c("dataset", "update_frequency"), c("column_dictionary", "constraint_iri"), c("tables", "method_iri"), c("codes", "vocabulary_iri"))) {
  fields <- configured$metadata_tables[[pair[[1L]]]]$fields
  keep <- purrr::map_chr(fields, "name") != pair[[2L]]
  stopifnot(sum(!keep) == 1L)
  configured$metadata_tables[[pair[[1L]]]]$fields <- fields[keep]
}
root <- tempfile("b252-r-probe-")
dir.create(root)
resources <- list(observations = data.frame(stream_name = c("Goldstream", "Craigflower"), spawner_count = c(1L, 2L)))
cases <- list()
capture_fields <- function(path) {
  list(
    dataset = names(readr::read_csv(file.path(path, "metadata", "dataset.csv"), show_col_types = FALSE)),
    dictionary = names(readr::read_csv(file.path(path, "metadata", "column_dictionary.csv"), show_col_types = FALSE)),
    tables = names(readr::read_csv(file.path(path, "metadata", "tables.csv"), show_col_types = FALSE)),
    codes = names(readr::read_csv(file.path(path, "metadata", "codes.csv"), show_col_types = FALSE))
  )
}
loader_calls <- 0L
withr::with_options(list(metasalmon.sdp_schema_source = "remote"), {
  testthat::with_mocked_bindings({
    stopifnot(!"update_frequency" %in% .ms_dataset_meta_cols())
    stopifnot(!"constraint_iri" %in% .ms_dictionary_cols())
    # Deliberately unfilled semantic slots produce authoring warnings. These
    # diagnostic suppressions retire when the fixture supplies reviewed IRIs.
    path <- file.path(root, "create")
    suppressWarnings(suppressMessages(create_sdp(
      resources, path = path, dataset_id = "probe", seed_semantics = FALSE,
      seed_verbose = FALSE, check_updates = FALSE
    )))
    cases$create <- capture_fields(path)
    artifacts <- infer_salmon_datapackage_artifacts(
      resources, dataset_id = "probe", seed_semantics = FALSE, seed_verbose = FALSE
    )
    dataset <- artifacts$dataset_meta
    dictionary <- artifacts$dict
    dataset$update_frequency <- NULL
    dictionary$constraint_iri <- NULL
    tables <- artifacts$table_meta
    codes <- artifacts$codes
    tables$method_iri <- NULL
    codes$vocabulary_iri <- NULL
    dataset$caller_extra <- "preserve caller data"
    dictionary$caller_extra <- "preserve caller data"
    stopifnot(!"update_frequency" %in% names(dataset))
    stopifnot(!"constraint_iri" %in% names(dictionary))
    path <- file.path(root, "direct")
    suppressWarnings(suppressMessages(write_salmon_datapackage(
      resources, dataset_meta = dataset, table_meta = tables,
      dict = dictionary, codes = codes, path = path
    )))
    cases$direct_writer <- capture_fields(path)
    suppressMessages(set_sdp_dataset(path, title = "Updated title", quiet = TRUE))
    cases$after_setter <- capture_fields(path)
  }, .ms_load_sdp_schema = function(...) {
    loader_calls <<- loader_calls + 1L
    configured
  }, .package = "metasalmon")
})
stopifnot(loader_calls > 0L)
result <- list(
  R = R.version.string, readr = as.character(packageVersion("readr")),
  commit = system2("git", c("-C", args[[1L]], "rev-parse", "HEAD"), stdout = TRUE),
  selected_loader_calls = loader_calls, cases = cases
)
cat(jsonlite::toJSON(result, auto_unbox = TRUE, pretty = TRUE), "\n")
unlink(root, recursive = TRUE)
