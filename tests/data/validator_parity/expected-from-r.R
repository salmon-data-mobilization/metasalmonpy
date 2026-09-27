#!/usr/bin/env Rscript
# The R half of the bundle-validator parity pin (hub queue B-360).
#
# Reads cases.json beside this script and scores every case with metasalmon's
# own validators in R/semantic-bundle-validators.R -- the dimension classifier,
# the three evidence predicates, the role-hint splitter, the role-type check,
# the field-anchored evidence builder and the whole
# `.ms_semantic_apply_bundle_validators()` driver -- and writes the verdicts as
# JSON. `expected.json` beside this script is what this produced;
# tests/test_validator_parity.py holds this package to it offline, and re-runs
# this script against an installed metasalmon wherever R is available (the
# `parity` job of .github/workflows/parity.yml), so a change to R's validators
# turns that job red rather than drifting.
#
# Usage:
#   Rscript expected-from-r.R [<fixture-dir>] [<output-path>]
# With no output path the JSON goes to stdout. Needs metasalmon on the library
# path (R_LIBS=/tmp/metasalmon-lib in the parity job).
suppressPackageStartupMessages(library(metasalmon))

args <- commandArgs(trailingOnly = TRUE)
script_dir <- local({
  file_arg <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
  if (length(file_arg) == 1L) dirname(sub("^--file=", "", file_arg)) else "."
})
fixture_dir <- normalizePath(if (length(args) >= 1L) args[[1L]] else script_dir)
output_path <- if (length(args) >= 2L) args[[2L]] else NULL

cases <- jsonlite::fromJSON(
  file.path(fixture_dir, "cases.json"),
  simplifyVector = FALSE
)

as_text <- function(value) {
  if (is.null(value)) NA_character_ else as.character(value)
}
as_values <- function(values) {
  vapply(values, as_text, character(1))
}
one_row <- function(fields) {
  tibble::as_tibble(lapply(fields, as_text))
}
na_to_null <- function(value) {
  if (length(value) == 0L || is.na(value)) NULL else value
}

# --- the pieces --------------------------------------------------------------

dimension <- lapply(cases$dimension, function(values) {
  na_to_null(metasalmon:::.ms_semantic_validator_dimension(as_values(values)))
})

method_evidence <- lapply(cases$method_evidence, function(text) {
  metasalmon:::.ms_semantic_validator_has_method_evidence(as_text(text))
})
constraint_evidence <- lapply(cases$constraint_evidence, function(text) {
  metasalmon:::.ms_semantic_validator_has_constraint_evidence(as_text(text))
})
modifier_evidence <- lapply(cases$modifier_evidence, function(text) {
  metasalmon:::.ms_semantic_validator_has_modifier_evidence(as_text(text))
})

# The splitter alone hands an NA input back as one NA hint;
# .ms_validate_semantic_role_type() drops NA and empty hints before it reads
# them, and that filtered view is the one the validators see, so it is the one
# recorded here.
role_hints <- lapply(cases$role_hints, function(value) {
  hints <- metasalmon:::.ms_semantic_split_role_hints(as_text(value))
  as.list(hints[!is.na(hints) & nzchar(hints)])
})

role_type <- list()
for (case in cases$role_type) {
  out <- metasalmon:::.ms_validate_semantic_role_type(
    case$role,
    one_row(case$candidate)
  )
  # `x[[name]] <- NULL` would drop the element; a null verdict must be kept.
  role_type[case$name] <- list(if (nrow(out) == 0L) NULL else out$message[[1L]])
}

evidence <- list()
for (case in cases$evidence) {
  chunks <- if (length(case$chunks) == 0L) {
    tibble::tibble()
  } else {
    tibble::tibble(chunk_text = as_values(case$chunks))
  }
  evidence[[case$name]] <- metasalmon:::.ms_semantic_bundle_validator_evidence(
    one_row(case$target),
    one_row(case$dict_row),
    chunks
  )
}

# --- whole bundles through the driver ----------------------------------------

build_bundle <- function(case) {
  dict_row <- one_row(case$dict_row)
  identity <- function() {
    list(
      dataset_id = dict_row$dataset_id[[1L]],
      table_id = dict_row$table_id[[1L]],
      column_name = dict_row$column_name[[1L]],
      code_value = NA_character_,
      target_scope = "column",
      target_sdp_file = "column_dictionary.csv"
    )
  }
  targets <- dplyr::bind_rows(lapply(case$targets, function(target) {
    tibble::as_tibble(c(identity(), list(
      dictionary_role = as_text(target$dictionary_role),
      target_sdp_field = as_text(target$target_sdp_field),
      search_query = as_text(target$search_query),
      target_label = as_text(target$target_label),
      target_description = as_text(target$target_description),
      target_query_context = as_text(target$target_query_context),
      column_label = as_text(target$column_label),
      column_description = as_text(target$column_description)
    )))
  }))
  query_for <- stats::setNames(targets$search_query, targets$dictionary_role)
  field_for <- stats::setNames(targets$target_sdp_field, targets$dictionary_role)

  suggestions <- dplyr::bind_rows(lapply(names(case$candidates), function(role) {
    dplyr::bind_rows(lapply(case$candidates[[role]], function(candidate) {
      tibble::as_tibble(c(identity(), list(
        dictionary_role = role,
        target_sdp_field = field_for[[role]],
        search_query = query_for[[role]],
        label = as_text(candidate$label),
        iri = as_text(candidate$iri),
        source = as_text(candidate$source),
        ontology = as_text(candidate$ontology),
        role = role,
        role_hints = as_text(candidate$role_hints),
        match_type = "label",
        definition = as_text(candidate$definition),
        score = if (is.null(candidate$score)) NA_real_ else as.numeric(candidate$score),
        term_type = as_text(candidate$term_type),
        resource_kind = as_text(candidate$resource_kind),
        type_iris = as_text(candidate$type_iris),
        native_type = as_text(candidate$native_type)
      )))
    }))
  }))
  candidate_groups <- metasalmon:::.ms_semantic_bundle_candidate_groups(
    targets,
    suggestions
  )

  assessments <- dplyr::bind_rows(lapply(case$assessments, function(row) {
    role <- as_text(row$dictionary_role)
    tibble::as_tibble(c(identity(), list(
      dictionary_role = role,
      target_sdp_field = field_for[[role]],
      search_query = query_for[[role]],
      llm_decision = as_text(row$llm_decision),
      llm_selected_candidate_index = if (is.null(row$llm_selected_candidate_index)) {
        NA_integer_
      } else {
        as.integer(row$llm_selected_candidate_index)
      },
      llm_selected_iri = as_text(row$llm_selected_iri),
      llm_selected_label = as_text(row$llm_selected_label),
      llm_rationale = as_text(row$llm_rationale),
      llm_confidence = if (is.null(row$llm_confidence)) {
        NA_real_
      } else {
        as.numeric(row$llm_confidence)
      }
    )))
  }))

  chunks <- if (length(case$chunks) == 0L) {
    tibble::tibble()
  } else {
    tibble::tibble(
      source = "inline_context",
      chunk_id = sprintf("inline_context[%d]#1", seq_along(case$chunks)),
      chunk_text = as_values(case$chunks)
    )
  }

  out <- metasalmon:::.ms_semantic_apply_bundle_validators(
    assessments,
    candidate_groups,
    targets,
    dict_row,
    chunks
  )
  list(
    findings = lapply(seq_len(nrow(out$findings)), function(i) {
      as.list(out$findings[i, , drop = FALSE])
    }),
    rows = lapply(seq_len(nrow(out$assessments)), function(i) {
      row <- out$assessments[i, , drop = FALSE]
      list(
        dictionary_role = row$dictionary_role[[1L]],
        llm_decision = row$llm_decision[[1L]],
        llm_selected_candidate_index = na_to_null(row$llm_selected_candidate_index[[1L]]),
        llm_selected_iri = na_to_null(row$llm_selected_iri[[1L]]),
        llm_selected_label = na_to_null(row$llm_selected_label[[1L]]),
        llm_rationale = na_to_null(row$llm_rationale[[1L]]),
        llm_confidence = na_to_null(row$llm_confidence[[1L]])
      )
    })
  )
}

bundles <- list()
for (case in cases$bundles) {
  bundles[[case$name]] <- build_bundle(case)
}

out <- jsonlite::toJSON(
  list(
    dimension = dimension,
    method_evidence = method_evidence,
    constraint_evidence = constraint_evidence,
    modifier_evidence = modifier_evidence,
    role_hints = role_hints,
    role_type = role_type,
    evidence = evidence,
    bundles = bundles
  ),
  auto_unbox = TRUE,
  pretty = TRUE,
  null = "null",
  na = "null",
  digits = NA
)
if (is.null(output_path)) {
  cat(out, "\n", sep = "")
} else {
  writeLines(out, output_path, useBytes = TRUE)
}
