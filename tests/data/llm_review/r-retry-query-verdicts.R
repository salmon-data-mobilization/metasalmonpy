#!/usr/bin/env Rscript
# Drive metasalmon's retry-query classifier over retry-query-corpus.json and
# write its verdicts to r-retry-query-verdicts.json, the fixture
# tests/test_retry_query_classifier.py pins this package's port against.
#
# The functions driven are internal: .ms_llm_normalize_query_text(),
# .ms_llm_query_looks_like_identifier() and .ms_llm_classify_retry_query()
# (R/llm-semantic-helpers.R). Run it against the metasalmon tree the fixture
# should describe -- never a primary checkout being edited:
#
#   METASALMON_SRC=/path/to/metasalmon-export Rscript r-retry-query-verdicts.R
#
# With METASALMON_SRC unset it uses the installed metasalmon. Run under a UTF-8
# locale: tolower() and \s are locale-dependent beyond ASCII, and the fixture
# records the UTF-8 verdicts (see the "non-ascii-case" and "*-collapsed" cases).
# Then record the commit and R version in the fixture's "provenance" member and
# in the test module's docstring.
#
# Regenerate whenever metasalmon changes one of the three functions; the
# "curie-lowercase-s", "curie-space-allowed" and "curie-backslash" cases exist
# to flip the day metasalmon rewrites its bracket expression.
suppressPackageStartupMessages(library(jsonlite))
src <- Sys.getenv("METASALMON_SRC", unset = NA_character_)
if (!is.na(src) && nzchar(src)) {
  suppressPackageStartupMessages(pkgload::load_all(src, quiet = TRUE, export_all = TRUE))
} else {
  suppressPackageStartupMessages(library(metasalmon))
}
ns <- asNamespace("metasalmon")
normalize <- get(".ms_llm_normalize_query_text", envir = ns)
looks_like_identifier <- get(".ms_llm_query_looks_like_identifier", envir = ns)
classify <- get(".ms_llm_classify_retry_query", envir = ns)

here <- if (!is.na(src <- Sys.getenv("CORPUS_DIR", unset = NA_character_)) && nzchar(src)) src else {
  args <- commandArgs(trailingOnly = FALSE)
  file_arg <- sub("^--file=", "", args[grepl("^--file=", args)])
  if (length(file_arg)) dirname(normalizePath(file_arg[[1]])) else getwd()
}
corpus <- fromJSON(file.path(here, "retry-query-corpus.json"), simplifyVector = FALSE)

as_json_value <- function(x) if (is.null(x) || length(x) == 0 || is.na(x)) NULL else x
verdicts <- lapply(corpus$cases, function(case) {
  retry <- if (is.null(case$retry)) NA_character_ else case$retry
  original <- if (is.null(case$original)) NA_character_ else case$original
  verdict <- classify(retry, original)
  list(
    id = case$id,
    retry = case$retry,
    original = case$original,
    exercises = case$exercises,
    normalized_retry = as_json_value(normalize(retry)),
    looks_like_identifier = looks_like_identifier(retry),
    query = as_json_value(verdict$query),
    original_query = as_json_value(verdict$original_query),
    disposition = verdict$disposition,
    rejection_reason = as_json_value(verdict$rejection_reason)
  )
})

out <- list(
  provenance = list(
    generator = "tests/data/llm_review/r-retry-query-verdicts.R",
    metasalmon_commit = Sys.getenv("METASALMON_COMMIT", unset = "unrecorded"),
    metasalmon_version = as.character(utils::packageVersion("metasalmon")),
    r_version = R.version.string,
    locale = Sys.getlocale("LC_CTYPE")
  ),
  cases = verdicts
)
writeLines(
  toJSON(out, auto_unbox = TRUE, null = "null", na = "null", pretty = 2),
  file.path(here, "r-retry-query-verdicts.json"),
  useBytes = TRUE
)
cat("wrote", length(verdicts), "verdicts\n")
