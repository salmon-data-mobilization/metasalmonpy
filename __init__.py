"""
metasalmonpy: Python helpers for Salmon Data Packages (SDPs).

This mirrors the metasalmon R package at a feature level so Python users can
infer dictionaries, validate metadata, search ontology terms, and build/read
Frictionless-style Salmon Data Packages.
"""

__version__ = "0.1.6"

from .dictionary import (
    apply_salmon_dictionary,
    infer_column_role,
    infer_dictionary,
    infer_value_type,
    validate_dictionary,
)
from .github_io import github_raw_url, ms_setup_github, read_github_csv, read_github_csv_dir
from .ices_vocab import ices_code_types, ices_codes, ices_find_code_types, ices_find_codes
from .package_io import (
    create_sdp,
    create_salmon_datapackage,
    create_salmon_datapackage_from_data,
    infer_salmon_datapackage_artifacts,
    read_salmon_datapackage,
    validate_salmon_datapackage,
    write_salmon_datapackage,
)
from .dwc_dp import suggest_dwc_mappings
from .dwc_dp_export import dwc_dp_build_descriptor
from .edh_xml import (
    edh_build_hnap_xml,
    edh_build_iso19139_xml,
    write_edh_xml_from_sdp,
)
from .eml import write_eml_from_sdp
from .knb_publication import publish_sdp_to_knb
from .measurement_decompositions import (
    read_sdp_measurement_decompositions,
    validate_sdp_measurement_decompositions,
    write_sdp_measurement_decompositions,
)
from .semantics import apply_semantic_suggestions, suggest_semantics
from .sssom import (
    SssomMappingSet,
    read_sssom_mapping_set,
    validate_sdp_sssom,
    write_sdp_sssom,
)
from .term_search import benchmark_term_ranking_fixtures, find_terms, sources_for_role
from .term_deduplication import deduplicate_proposed_terms, suggest_facet_schemes
from .validation import validate_semantics
from .ontology_fetch import fetch_salmon_ontology
from .nuseds import nuseds_enumeration_method_crosswalk, nuseds_estimate_method_crosswalk
from .term_requests import detect_semantic_term_gaps, render_ontology_term_request, submit_term_request_issues
from .version_check import check_for_updates
from .chat_decomposition import chat_decomposition

__all__ = [
    "__version__",
    "apply_salmon_dictionary",
    "apply_semantic_suggestions",
    "benchmark_term_ranking_fixtures",
    "check_for_updates",
    "chat_decomposition",
    "create_sdp",
    "create_salmon_datapackage",
    "create_salmon_datapackage_from_data",
    "deduplicate_proposed_terms",
    "detect_semantic_term_gaps",
    "dwc_dp_build_descriptor",
    "edh_build_hnap_xml",
    "edh_build_iso19139_xml",
    "fetch_salmon_ontology",
    "find_terms",
    "infer_column_role",
    "infer_dictionary",
    "infer_salmon_datapackage_artifacts",
    "infer_value_type",
    "github_raw_url",
    "ices_code_types",
    "ices_codes",
    "ices_find_code_types",
    "ices_find_codes",
    "ms_setup_github",
    "publish_sdp_to_knb",
    "nuseds_enumeration_method_crosswalk",
    "nuseds_estimate_method_crosswalk",
    "read_salmon_datapackage",
    "read_github_csv",
    "read_github_csv_dir",
    "read_sdp_measurement_decompositions",
    "read_sssom_mapping_set",
    "render_ontology_term_request",
    "SssomMappingSet",
    "suggest_dwc_mappings",
    "suggest_facet_schemes",
    "suggest_semantics",
    "sources_for_role",
    "submit_term_request_issues",
    "validate_dictionary",
    "validate_salmon_datapackage",
    "validate_sdp_measurement_decompositions",
    "validate_sdp_sssom",
    "validate_semantics",
    "write_edh_xml_from_sdp",
    "write_eml_from_sdp",
    "write_salmon_datapackage",
    "write_sdp_measurement_decompositions",
    "write_sdp_sssom",
]
