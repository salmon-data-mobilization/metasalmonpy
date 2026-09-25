"""Tests for term_deduplication module."""

import pandas as pd
import pytest
from metasalmonpy.term_deduplication import deduplicate_proposed_terms, suggest_facet_schemes


def test_deduplicate_empty_dataframe():
    """Test deduplication with empty DataFrame."""
    result = deduplicate_proposed_terms(pd.DataFrame())
    assert isinstance(result, pd.DataFrame)
    assert len(result) == 0
    assert 'dedup_notes' in result.columns


def test_deduplicate_age_variants():
    """Test collapsing age-stratified variants."""
    proposed = pd.DataFrame({
        'term_label': ['Spawners Age 1', 'Spawners Age 2', 'Spawners Age 3', 'Catch'],
        'term_definition': ['Spawners age 1', 'Spawners age 2', 'Spawners age 3', 'Total catch'],
        'term_type': ['skos_concept'] * 4,
        'suggested_parent_iri': ['http://example.org/Abundance'] * 4
    })

    result = deduplicate_proposed_terms(proposed, warn_threshold=10)

    # Should collapse to 2 base terms (Spawners + Catch)
    assert len(result) == 2

    # Find the spawners base term
    spawners = result[result['term_label'] == 'Spawners Age 1'].iloc[0]
    assert spawners['is_base_term'] is True
    assert spawners['needs_age_facet'] is True
    assert spawners['collapsed_from'] == 3
    assert 'age' in spawners['dedup_notes'].lower()


def test_deduplicate_phase_variants():
    """Test collapsing phase-stratified variants."""
    proposed = pd.DataFrame({
        'term_label': ['Ocean Catch', 'Terminal Catch', 'Mainstem Run'],
        'term_definition': ['Ocean catch', 'Terminal catch', 'Mainstem run'],
        'term_type': ['skos_concept'] * 3,
        'suggested_parent_iri': ['http://example.org/Abundance'] * 3
    })

    result = deduplicate_proposed_terms(proposed, warn_threshold=10)

    # Should collapse Ocean/Terminal Catch to 1 base term, Mainstem Run stays separate
    assert len(result) == 2

    # Check that one is a phase variant base term
    phase_bases = result[result['needs_phase_facet']]
    assert len(phase_bases) >= 1


def test_deduplicate_exact_duplicates():
    """Test removal of exact duplicate term labels."""
    proposed = pd.DataFrame({
        'term_label': ['Abundance', 'abundance', 'ABUNDANCE', 'Catch'],
        'term_definition': ['Total abundance', 'Total abundance 2', 'Total abundance 3', 'Catch'],
        'term_type': ['skos_concept'] * 4,
        'suggested_parent_iri': ['http://example.org/Abundance'] * 4
    })

    result = deduplicate_proposed_terms(proposed, warn_threshold=10)

    # Should have only 2 unique terms (normalized case)
    assert len(result) == 2


def test_deduplicate_warning_threshold():
    """Test that warning is issued for large inputs."""
    # Create DataFrame with 35 unique terms (> threshold of 30)
    proposed = pd.DataFrame({
        'term_label': [f'Term_{i}' for i in range(35)],
        'term_definition': [f'Definition {i}' for i in range(35)],
        'term_type': ['skos_concept'] * 35,
        'suggested_parent_iri': ['http://example.org/Term'] * 35
    })

    with pytest.warns(UserWarning, match='over-engineering'):
        deduplicate_proposed_terms(proposed, warn_threshold=30)


def test_suggest_facet_schemes_empty():
    """Test facet scheme suggestion with empty DataFrame."""
    result = suggest_facet_schemes(pd.DataFrame())
    assert isinstance(result, pd.DataFrame)
    assert len(result) == 0


def test_suggest_facet_schemes_age():
    """Test detection of age class facet scheme."""
    proposed = pd.DataFrame({
        'term_label': ['Spawners Age 1', 'Spawners Age 2', 'Spawners Age 3', 'Catch Age 2']
    })

    result = suggest_facet_schemes(proposed)

    assert len(result) == 1
    assert result.iloc[0]['scheme_name'] == 'AgeClassScheme'
    assert 'Age1Class' in result.iloc[0]['suggested_concepts']
    assert 'Age2Class' in result.iloc[0]['suggested_concepts']
    assert 'Age3Class' in result.iloc[0]['suggested_concepts']


def test_suggest_facet_schemes_phase():
    """Test detection of life phase facet scheme."""
    proposed = pd.DataFrame({
        'term_label': ['Ocean Catch', 'Terminal Catch', 'Marine Abundance']
    })

    result = suggest_facet_schemes(proposed)

    assert len(result) == 1
    assert result.iloc[0]['scheme_name'] == 'LifePhaseScheme'
    concepts = result.iloc[0]['suggested_concepts']
    assert 'OceanPhase' in concepts or any('ocean' in str(c).lower() for c in concepts)


def test_suggest_facet_schemes_benchmark():
    """Test detection of benchmark level facet scheme."""
    proposed = pd.DataFrame({
        'term_label': ['Lower Benchmark', 'Upper Benchmark', 'Some Other Term']
    })

    result = suggest_facet_schemes(proposed)

    assert len(result) == 1
    assert result.iloc[0]['scheme_name'] == 'BenchmarkLevelScheme'
    assert 'LowerBenchmark' in result.iloc[0]['suggested_concepts']
    assert 'UpperBenchmark' in result.iloc[0]['suggested_concepts']


def test_suggest_facet_schemes_multiple():
    """Test detection of multiple facet schemes."""
    proposed = pd.DataFrame({
        'term_label': [
            'Spawners Age 1', 'Spawners Age 2', 'Spawners Age 3',
            'Ocean Catch', 'Terminal Catch',
            'Lower Benchmark', 'Upper Benchmark'
        ]
    })

    result = suggest_facet_schemes(proposed)

    # Should detect all three schemes
    assert len(result) == 3
    scheme_names = set(result['scheme_name'])
    assert 'AgeClassScheme' in scheme_names
    assert 'LifePhaseScheme' in scheme_names
    assert 'BenchmarkLevelScheme' in scheme_names
