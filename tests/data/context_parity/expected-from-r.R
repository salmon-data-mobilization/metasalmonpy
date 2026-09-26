#!/usr/bin/env Rscript
# The R half of the context-excerpt parity pin (hub queue B-364).
#
# Reads cases.json beside this script, builds the context chunk pool and scores
# it for every target case with metasalmon's own internals --
# `.ms_collect_context_chunks()` and `.ms_score_context_chunks()` -- and writes
# the result as JSON. `expected.json` beside this script is what this produced;
# tests/test_context_parity.py holds this package to it offline, and re-runs
# this script against an installed metasalmon wherever R is available (the
# `parity` job of .github/workflows/parity.yml) so that a change to R's
# algorithm turns that job red rather than drifting silently.
#
# Usage:
#   Rscript expected-from-r.R [<fixture-dir>] [<output-path>]
# With no output path the JSON goes to stdout. Needs metasalmon on the library
# path (R_LIBS=/tmp/metasalmon-lib in the parity job).
#
# Collation, stated because it decides tie order. `.ms_score_context_chunks()`
# orders ties by source label with `order()`, which follows the session locale
# until hub item B-326 makes it radix (C collation). This script sets
# LC_COLLATE to C so that what it records is that radix order -- the one the
# parity job's Linux runner already produces and the one metasalmonpy sorts by
# -- rather than the en_CA / en_US order a developer's shell would give R.
# Retire the setlocale() call when B-326 has landed; the output will not change.
suppressPackageStartupMessages(library(metasalmon))

args <- commandArgs(trailingOnly = TRUE)
script_dir <- local({
  file_arg <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
  if (length(file_arg) == 1L) dirname(sub("^--file=", "", file_arg)) else "."
})
fixture_dir <- normalizePath(if (length(args) >= 1L) args[[1L]] else script_dir)
output_path <- if (length(args) >= 2L) args[[2L]] else NULL

invisible(Sys.setlocale("LC_COLLATE", "C"))

cases <- jsonlite::fromJSON(
  file.path(fixture_dir, "cases.json"),
  simplifyVector = FALSE
)

as_text <- function(value) {
  if (is.null(value)) NA_character_ else as.character(value)
}

files <- file.path(fixture_dir, vapply(cases$files, as.character, character(1)))
inline <- vapply(cases$inline_text, as.character, character(1))

# The unsupported and empty fixtures are skipped with a warning, which is the
# behaviour under test; the pool simply lacks them.
pool <- suppressWarnings(
  metasalmon:::.ms_collect_context_chunks(
    context_files = files,
    context_text = inline
  )
)

pool_records <- lapply(seq_len(nrow(pool)), function(i) {
  list(
    source = pool$source[[i]],
    chunk_id = pool$chunk_id[[i]],
    text = pool$chunk_text[[i]]
  )
})

target_results <- list()
for (case in cases$targets) {
  target_row <- tibble::as_tibble(lapply(case$target, as_text))
  candidates <- if (length(case$candidates) == 0L) {
    tibble::tibble(label = character(), definition = character())
  } else {
    dplyr::bind_rows(lapply(case$candidates, function(candidate) {
      tibble::tibble(
        label = as_text(candidate$label),
        definition = as_text(candidate$definition)
      )
    }))
  }
  scored <- metasalmon:::.ms_score_context_chunks(
    pool,
    target_row = target_row,
    candidate_rows = candidates,
    max_chunks = as.integer(case$max_chunks)
  )
  target_results[[case$name]] <- lapply(seq_len(nrow(scored)), function(i) {
    list(
      source = scored$source[[i]],
      chunk_id = scored$chunk_id[[i]],
      context_score = if ("context_score" %in% names(scored)) {
        as.integer(scored$context_score[[i]])
      } else {
        NULL
      }
    )
  })
}

out <- jsonlite::toJSON(
  list(pool = pool_records, targets = target_results),
  auto_unbox = TRUE,
  pretty = TRUE,
  null = "null",
  na = "null"
)
if (is.null(output_path)) {
  cat(out, "\n", sep = "")
} else {
  writeLines(out, output_path, useBytes = TRUE)
}
