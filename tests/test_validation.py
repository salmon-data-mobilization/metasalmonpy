"""Tests for validation module."""

import pandas as pd
import pytest
from validation import validate_semantics


def test_validate_semantics_basic():
    """Test basic semantic validation."""
    dict_df = pd.DataFrame({
        'table_id': ['table1', 'table1'],
        'column_name': ['species', 'abundance'],
        'column_role': ['identifier', 'measurement'],
        'column_description': ['Species name', 'Total abundance'],
        'term_iri': ['http://example.org/species', '']  # Missing IRI for measurement
    })

    result = validate_semantics(dict_df, require_iris=False)

    assert 'dict' in result
    assert 'issues' in result
    assert 'missing_terms' in result

    # Should have added 'required' column
    assert 'required' in result['dict'].columns

    # Should identify 1 missing term (abundance)
    assert len(result['missing_terms']) == 1
    missing = result['missing_terms'].iloc[0]
    assert missing['term_label'] == 'Abundance'
    assert 'table1' in missing['notes']


def test_validate_semantics_no_missing():
    """Test validation with all term_iri present."""
    dict_df = pd.DataFrame({
        'table_id': ['table1', 'table1'],
        'column_name': ['species', 'abundance'],
        'column_role': ['identifier', 'measurement'],
        'column_description': ['Species name', 'Total abundance'],
        'term_iri': ['http://example.org/species', 'http://example.org/abundance']
    })

    result = validate_semantics(dict_df, require_iris=False)

    # Should have no missing terms
    assert len(result['missing_terms']) == 0


def test_validate_semantics_adds_required_column():
    """Test that required column is added if missing."""
    dict_df = pd.DataFrame({
        'table_id': ['table1'],
        'column_name': ['species'],
        'column_role': ['identifier'],
        'column_description': ['Species name'],
        'term_iri': ['http://example.org/species']
    })

    # Ensure no 'required' column initially
    assert 'required' not in dict_df.columns

    result = validate_semantics(dict_df)

    # Should add required column
    assert 'required' in result['dict'].columns
    assert all(result['dict']['required'] == False)


def test_validate_semantics_multiple_missing():
    """Test with multiple missing term IRIs."""
    dict_df = pd.DataFrame({
        'table_id': ['table1', 'table1', 'table2'],
        'column_name': ['spawners_age_3', 'catch_terminal', 'run_size'],
        'column_role': ['measurement', 'measurement', 'measurement'],
        'column_description': ['Spawners age 3', 'Terminal catch', 'Run size'],
        'term_iri': ['', '', ''],
        'constraint_iri': ['http://ex.org/age3', '', '']
    })

    result = validate_semantics(dict_df, require_iris=False)

    # Should identify all 3 missing terms
    assert len(result['missing_terms']) == 3

    # Check that term labels are title-cased with underscores replaced
    labels = set(result['missing_terms']['term_label'])
    assert 'Spawners Age 3' in labels
    assert 'Catch Terminal' in labels
    assert 'Run Size' in labels

    # Check that notes include constraint info for first term
    notes_with_constraint = result['missing_terms'][
        result['missing_terms']['notes'].str.contains('age3')
    ]
    assert len(notes_with_constraint) == 1


def test_validate_semantics_reports_missing_semantics_fields():
    """Test reporting of all semantic fields, including optional constraint/method."""
    dict_df = pd.DataFrame({
        'table_id': ['table1'],
        'column_name': ['age'],
        'column_role': ['measurement'],
        'column_description': ['Age'],
        'term_iri': [''],
        'property_iri': [''],
        'entity_iri': [''],
        'unit_iri': ['m'],
        'constraint_iri': [''],
        'statistical_modifier_iri': [''],
    })

    result = validate_semantics(dict_df, require_iris=False)
    missing = result['missing_semantics']

    assert set(missing['field'].tolist()) == {
        'term_iri', 'property_iri', 'entity_iri',
        'constraint_iri', 'statistical_modifier_iri'
    }
    assert missing.shape[0] == 5
    assert missing.loc[missing['field'] == 'term_iri', 'column_name'].iloc[0] == 'age'


def test_validate_semantics_empty_dict():
    """Test with empty dictionary."""
    dict_df = pd.DataFrame({
        'table_id': [],
        'column_name': [],
        'column_role': [],
        'column_description': [],
        'term_iri': []
    })

    result = validate_semantics(dict_df)

    assert len(result['dict']) == 0
    assert len(result['missing_terms']) == 0


def test_validate_semantics_preserves_original():
    """Test that original dictionary is not modified."""
    dict_df = pd.DataFrame({
        'table_id': ['table1'],
        'column_name': ['abundance'],
        'column_role': ['measurement'],
        'column_description': ['Total abundance'],
        'term_iri': ['']
    })

    original_columns = dict_df.columns.tolist()
    result = validate_semantics(dict_df)

    # Original should not have 'required' added
    assert 'required' not in original_columns
    # But result should have it
    assert 'required' in result['dict'].columns
