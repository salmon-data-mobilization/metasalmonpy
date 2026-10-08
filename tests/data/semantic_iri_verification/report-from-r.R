# Rebuild the expected report from the R B-130 implementation. First load an
# R checkout containing verify_sdp_semantic_iris(), then source this script
# from the metasalmonpy root. Requests/delays are injected; no network evidence.
root <- tempfile("semantic-iri-byte-fixture-")
dir.create(file.path(root, "metadata"), recursive = TRUE)
readr::write_csv(tibble::tibble(dataset_id = "byte-test"), file.path(root, "metadata/dataset.csv"))
readr::write_csv(tibble::tibble(table_id = "obs"), file.path(root, "metadata/tables.csv"))
readr::write_csv(tibble::tibble(term_iri = c(
  "https://example.org/z#ok", "https://example.org/A#redirect",
  "https://example.org/é#missing", "https://example.org/a#transport"
)), file.path(root, "metadata/column_dictionary.csv"))
requester <- function(iri) {
  if (grepl("/a#", iri, fixed = TRUE)) stop("timeout; Authorization: Bearer test-credential")
  if (grepl("/é#", iri, fixed = TRUE)) return(list(status = 404L, final_url = iri))
  list(status = 200L, final_url = if (grepl("/A#", iri, fixed = TRUE)) "https://example.org/A" else iri)
}
tryCatch(verify_sdp_semantic_iris(root, requester = requester, sleep_fn = function(...) NULL),
         error = function(error) message(conditionMessage(error)))
file.copy(file.path(root, "reproducibility/provenance/semantic-iri-dereference.csv"),
          "tests/data/semantic_iri_verification/report-from-r.csv", overwrite = TRUE)
unlink(root, recursive = TRUE)
