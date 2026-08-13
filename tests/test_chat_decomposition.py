import pandas as pd

from metasalmonpy import chat_decomposition


def _dictionary():
    return pd.DataFrame(
        {
            "dataset_id": ["demo"],
            "table_id": ["fish"],
            "column_name": ["fork_length"],
            "column_label": ["Fork length"],
            "column_description": ["Fork length measured with callipers."],
            "column_role": ["measurement"],
            "value_type": ["number"],
            "required": [False],
            "unit_label": ["millimetre"],
            "unit_iri": [pd.NA],
            "term_iri": [pd.NA],
            "term_type": [pd.NA],
            "property_iri": [pd.NA],
            "entity_iri": [pd.NA],
            "constraint_iri": [pd.NA],
            "method_iri": [pd.NA],
        }
    )


def _suggestions():
    return pd.DataFrame(
        {
            "dataset_id": ["demo"],
            "table_id": ["fish"],
            "column_name": ["fork_length"],
            "code_value": [pd.NA],
            "dictionary_role": ["variable"],
            "target_scope": ["column"],
            "target_sdp_file": ["column_dictionary.csv"],
            "target_sdp_field": ["term_iri"],
            "search_query": ["fork length"],
            "label": ["Fork length"],
            "iri": ["https://example.org/ForkLength"],
            "source": ["smn"],
            "ontology": ["smn"],
            "role": ["variable"],
            "match_type": ["label"],
            "definition": ["Length from snout to tail fork."],
        }
    )


def test_scripted_chat_can_approve_a_candidate(tmp_path):
    result = chat_decomposition(
        _dictionary(),
        column_name="fork_length",
        suggestions=_suggestions(),
        session_root=tmp_path,
        commands=["/choose 1", "/approve"],
        output_fn=lambda message: None,
    )

    assert result["approval_status"] == "approved"
    assert result["approved_patch"]["result"] == "patch"
    assert (
        result["approved_patch"]["value"]
        == "https://example.org/ForkLength"
    )
    assert result["session_dir"].exists()


def test_scripted_chat_maps_internal_gap_to_public_new_term_result(tmp_path):
    result = chat_decomposition(
        _dictionary(),
        column_name="fork_length",
        suggestions=_suggestions(),
        session_root=tmp_path,
        commands=["/newterm", "/approve"],
        output_fn=lambda message: None,
    )

    assert result["state"]["decision"] == "request_new_term"
    assert result["approved_patch"]["result"] == "propose_new_term"
