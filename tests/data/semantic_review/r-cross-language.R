#!/usr/bin/env Rscript
# One half of a review session in metasalmon, for tests/test_semantic_review_parity.py
# (hub item B-327): the packet built in R and ingested here, and the reverse,
# on the shared conformance cases in v1/ beside this script. A packet either
# package writes is one the other's ingester accepts, and a session can change
# hands between its passes.
#
#   Rscript r-cross-language.R build  <case_dir> <review_dir>
#   Rscript r-cross-language.R ingest <case_dir> <review_dir> <harness_csv> <out_json>
#
# `build` writes the case's pass-1 packet into <review_dir>. `ingest` ingests
# <harness_csv> against the latest packet in <review_dir> with the fixtures'
# fake search function and writes the result -- status, summary, the number of
# search calls, and the record, findings and suggestions with every cell as the
# fixture README compares it -- to <out_json>.
#
# It drives the installed metasalmon, or the source tree METASALMON_SRC names
# (never a primary checkout being edited). It needs a metasalmon with
# write_semantic_review_packet(), which B-326 added; the Python test probes for
# it and skips otherwise.
suppressPackageStartupMessages(library(jsonlite))
src <- Sys.getenv("METASALMON_SRC", unset = NA_character_)
if (!is.na(src) && nzchar(src)) {
  suppressPackageStartupMessages(pkgload::load_all(src, quiet = TRUE, export_all = FALSE))
} else {
  suppressPackageStartupMessages(library(metasalmon))
}
options(metasalmon.llm_deprecation_quiet = TRUE)
format_number <- get(".ms_format_number_token", envir = asNamespace("metasalmon"))

args <- commandArgs(trailingOnly = TRUE)
mode <- args[[1]]
case_dir <- args[[2]]
review_dir <- args[[3]]

read_json <- function(path) fromJSON(path, simplifyVector = FALSE)

# helper-semantic-review.R's semantic_review_rows_to_tibble().
rows_to_tibble <- function(rows, numeric = character(), integer = character(), logical = character()) {
  if (length(rows) == 0L) {
    return(tibble::tibble())
  }
  cols <- unique(unlist(lapply(rows, names)))
  frame <- lapply(cols, function(col) {
    values <- lapply(rows, function(row) row[[col]])
    cell <- function(value, convert) if (is.null(value)) NA else convert(value[[1]])
    if (col %in% numeric) {
      vapply(values, cell, numeric(1), convert = as.numeric)
    } else if (col %in% integer) {
      vapply(values, cell, integer(1), convert = as.integer)
    } else if (col %in% logical) {
      vapply(values, cell, logical(1), convert = as.logical)
    } else {
      vapply(values, function(v) if (is.null(v)) NA_character_ else as.character(v[[1]]), character(1))
    }
  })
  tibble::as_tibble(stats::setNames(frame, cols))
}

input <- read_json(file.path(case_dir, "input.json"))
dict <- rows_to_tibble(input$dictionary)
attr(dict, "semantic_targets") <- rows_to_tibble(input$targets)
attr(dict, "semantic_suggestions") <- rows_to_tibble(
  input$candidates,
  numeric = c("score", "role_hint_bonus"),
  integer = "retrieval_pass",
  logical = c("alignment_only", "role_collision")
)

if (identical(mode, "build")) {
  built <- write_semantic_review_packet(
    dict,
    context_text = unlist(input$context_text),
    review_dir = review_dir,
    quiet = TRUE
  )
  cat(built$packet_id, "\n")
  quit(status = 0L)
}

harness <- args[[4]]
out <- args[[5]]
responses <- read_json(file.path(dirname(case_dir), "search-responses.json"))
calls <- 0L
search_fn <- function(query, role, sources) {
  calls <<- calls + 1L
  rows <- responses[[paste(query, role, sep = "|")]]
  if (is.null(rows)) {
    return(tibble::tibble())
  }
  rows_to_tibble(rows, numeric = "score")
}
result <- suppressWarnings(ingest_semantic_assessments(
  dict,
  assessments = harness,
  review_dir = review_dir,
  search_fn = search_fn,
  quiet = TRUE
))

# helper-semantic-review.R's to_text(): every cell as text, numbers through the
# number-token formatter, NA and the empty field one value.
frame_text <- function(frame) {
  frame <- tibble::as_tibble(frame)
  rows <- lapply(seq_len(nrow(frame)), function(i) {
    lapply(names(frame), function(col) {
      value <- frame[[col]][[i]]
      if (length(value) == 0L || is.na(value)) {
        return(NULL)
      }
      text <- if (is.logical(value)) {
        if (value) "TRUE" else "FALSE"
      } else if (is.numeric(value) && !is.integer(value)) {
        format_number(value)
      } else {
        as.character(value)
      }
      if (nzchar(text)) text else NULL
    })
  })
  list(columns = I(names(frame)), rows = rows)
}

writeLines(
  toJSON(
    list(
      status = result$status,
      pass = result$pass,
      packet_id = result$packet_id,
      has_next_packet = !is.null(result$next_packet),
      summary = list(
        decisions = as.list(result$summary$decisions),
        errors = result$summary$errors,
        downgrades = result$summary$downgrades,
        escalations = result$summary$escalations,
        retries = result$summary$retries,
        awaiting_pass_2 = result$summary$awaiting_pass_2,
        kept_pass_1 = result$summary$kept_pass_1
      ),
      search_calls = calls,
      record = frame_text(result$assessments),
      findings = frame_text(result$findings),
      suggestions = frame_text(result$suggestions)
    ),
    auto_unbox = TRUE,
    null = "null",
    na = "null",
    digits = NA
  ),
  out
)
