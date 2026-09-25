#!/usr/bin/env Rscript
# Drive metasalmon's second-pass merge over merge-candidates-cases.json and
# write what R gives to r-merge-candidates.json, the fixture
# tests/test_semantic_retrieval.py pins this package's port against.
#
# The functions driven are internal: .ms_merge_semantic_target_candidates()
# (R/semantics-helpers.R) and .ms_semantic_candidate_identity()
# (R/semantic-suggestions.R). For each case the fixture records the merged
# rows in R's order with R's columns, the identity of every merged row, the
# identity of every existing row, and the candidate gain counted as
# .ms_semantic_bundle_retry() counts it: merged identities absent from the
# existing ones. Run it against the metasalmon tree the fixture should
# describe -- never a primary checkout being edited:
#
#   METASALMON_SRC=/path/to/metasalmon-export Rscript r-merge-candidates.R
#
# With METASALMON_SRC unset it uses the installed metasalmon. Then record the
# commit and R version in the fixture's "provenance" member and in the test
# module's docstring. Regenerate whenever metasalmon changes either function.
suppressPackageStartupMessages(library(jsonlite))
src <- Sys.getenv("METASALMON_SRC", unset = NA_character_)
if (!is.na(src) && nzchar(src)) {
  suppressPackageStartupMessages(pkgload::load_all(src, quiet = TRUE, export_all = TRUE))
} else {
  suppressPackageStartupMessages(library(metasalmon))
}
ns <- asNamespace("metasalmon")
merge_candidates <- get(".ms_merge_semantic_target_candidates", envir = ns)
identity_of <- get(".ms_semantic_candidate_identity", envir = ns)

args <- commandArgs(trailingOnly = FALSE)
file_arg <- sub("^--file=", "", args[grepl("^--file=", args)])
here <- if (length(file_arg)) dirname(normalizePath(file_arg[[1]])) else getwd()
inputs <- fromJSON(file.path(here, "merge-candidates-cases.json"), simplifyVector = FALSE)

# Rows arrive as lists of named lists; a null is NA. Build the tibble column by
# column so a column that is null in every row is still a column.
rows_to_tibble <- function(rows) {
  if (length(rows) == 0L) {
    return(tibble::tibble())
  }
  columns <- unique(unlist(lapply(rows, names)))
  values <- lapply(columns, function(column) {
    cells <- lapply(rows, function(row) if (is.null(row[[column]])) NA else row[[column]])
    unlist(cells)
  })
  names(values) <- columns
  tibble::as_tibble(values)
}

records_of <- function(frame) {
  list(
    columns = names(frame),
    rows = lapply(seq_len(nrow(frame)), function(i) {
      cells <- lapply(names(frame), function(column) {
        value <- frame[[column]][[i]]
        if (length(value) == 0L || is.na(value)) NULL else value
      })
      names(cells) <- names(frame)
      cells
    })
  )
}

cases <- lapply(inputs$cases, function(case) {
  existing <- rows_to_tibble(case$existing)
  extra <- rows_to_tibble(case$extra)
  merged <- merge_candidates(existing, extra, case$max_per_role)
  existing_ids <- identity_of(existing)
  merged_ids <- identity_of(merged)
  list(
    id = case$id,
    exercises = case$exercises,
    max_per_role = case$max_per_role,
    existing_columns = names(existing),
    existing = case$existing,
    extra_columns = names(extra),
    extra = case$extra,
    merged = records_of(merged),
    existing_identity = as.list(existing_ids),
    merged_identity = as.list(merged_ids),
    gain = sum(!merged_ids %in% existing_ids),
    generic_path_gain = {
      # .ms_llm_explore_record() counts with paste(source, iri, sep = "::");
      # recorded for the reader, equal to gain whenever every iri is present.
      keys_of <- function(frame) {
        if (nrow(frame) == 0L) character() else unique(paste(frame$source, frame$iri, sep = "::"))
      }
      sum(!keys_of(merged) %in% keys_of(existing))
    }
  )
})

out <- list(
  provenance = list(
    generator = "tests/data/semantics/r-merge-candidates.R",
    metasalmon_commit = Sys.getenv("METASALMON_COMMIT", unset = "unrecorded"),
    metasalmon_version = as.character(utils::packageVersion("metasalmon")),
    r_version = R.version.string,
    locale = Sys.getlocale("LC_COLLATE")
  ),
  cases = cases
)
writeLines(
  toJSON(out, auto_unbox = TRUE, null = "null", na = "null", digits = NA, pretty = 2),
  file.path(here, "r-merge-candidates.json"),
  useBytes = TRUE
)
cat("wrote", length(cases), "cases\n")
