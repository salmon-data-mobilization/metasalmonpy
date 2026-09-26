#!/usr/bin/env Rscript
# Drive metasalmon's shortlist retriever at retrieval passes 1 and 2 over
# retrieve-candidates-cases.json and write what R gives to
# r-retrieve-candidates.json, the fixture tests/test_semantic_retrieval.py
# pins both of this package's passes against (pass 2 since hub B-363, pass 1
# since B-382). R's retriever has one rule for both passes; the pass is
# recorded on every row and changes nothing else, which the fixture shows
# rather than assumes.
#
# The function driven is internal: .ms_retrieve_semantic_target_candidates()
# (R/semantics-helpers.R), with a search function that returns the case's
# rows whatever it is asked, and a source policy built by
# .ms_semantic_source_policy() -- role defaults when the case's sources are
# null, an explicit allowlist otherwise. Run it against the metasalmon tree
# the fixture should describe -- never a primary checkout being edited:
#
#   METASALMON_SRC=/path/to/metasalmon-export METASALMON_COMMIT=<sha> \
#     Rscript r-retrieve-candidates.R
#
# With METASALMON_SRC unset it uses the installed metasalmon. The commit and R
# version land in the fixture's "provenance" member; record them in the test
# module's provenance test too. Regenerate whenever metasalmon changes the
# retriever.
suppressPackageStartupMessages(library(jsonlite))
src <- Sys.getenv("METASALMON_SRC", unset = NA_character_)
if (!is.na(src) && nzchar(src)) {
  suppressPackageStartupMessages(pkgload::load_all(src, quiet = TRUE, export_all = TRUE))
} else {
  suppressPackageStartupMessages(library(metasalmon))
}
ns <- asNamespace("metasalmon")
retrieve <- get(".ms_retrieve_semantic_target_candidates", envir = ns)
source_policy <- get(".ms_semantic_source_policy", envir = ns)

args <- commandArgs(trailingOnly = FALSE)
file_arg <- sub("^--file=", "", args[grepl("^--file=", args)])
here <- if (length(file_arg)) dirname(normalizePath(file_arg[[1]])) else getwd()
inputs <- fromJSON(file.path(here, "retrieve-candidates-cases.json"), simplifyVector = FALSE)

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
  target <- rows_to_tibble(list(case$target))
  results <- rows_to_tibble(case$results)
  calls <- list()
  search_fn <- function(query, role, sources) {
    calls[[length(calls) + 1L]] <<- list(query = query, role = role, sources = as.character(sources))
    results
  }
  policy <- if (is.null(case$sources)) {
    source_policy(character(), omitted = TRUE)
  } else {
    source_policy(unlist(case$sources), omitted = FALSE)
  }
  # One entry per pass, each with the calls that pass made, so a difference
  # between the passes would show in the fixture instead of being assumed away.
  passes <- lapply(c(1L, 2L), function(pass) {
    calls <<- list()
    retrieved <- retrieve(
      target = target,
      sources = policy,
      max_per_role = case$max_per_role,
      search_fn = search_fn,
      query = case$query,
      retrieval_pass = pass
    )
    list(
      retrieval_pass = pass,
      search_calls = lapply(calls, function(call) list(query = call$query, role = call$role)),
      retrieved = records_of(retrieved)
    )
  })
  list(
    id = case$id,
    exercises = case$exercises,
    sources = case$sources,
    max_per_role = case$max_per_role,
    query = case$query,
    target = case$target,
    result_columns = names(results),
    results = case$results,
    passes = passes
  )
})

out <- list(
  provenance = list(
    generator = "tests/data/semantics/r-retrieve-candidates.R",
    metasalmon_commit = Sys.getenv("METASALMON_COMMIT", unset = "unrecorded"),
    metasalmon_version = as.character(utils::packageVersion("metasalmon")),
    r_version = R.version.string,
    locale = Sys.getlocale("LC_COLLATE")
  ),
  cases = cases
)
writeLines(
  toJSON(out, auto_unbox = TRUE, null = "null", na = "null", digits = I(17), pretty = 2),
  file.path(here, "r-retrieve-candidates.json"),
  useBytes = TRUE
)
cat("wrote", length(cases), "cases\n")
