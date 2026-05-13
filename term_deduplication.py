"""
Term deduplication and facet scheme suggestion for I-ADOPT ontology patterns.

This module prevents term proliferation by:
1. Removing duplicates across tables (same term_label)
2. Collapsing age-stratified variants (X Age 1..7) into one base term
3. Collapsing phase-stratified variants (Ocean/Terminal/Mainstem X) into one base term
4. Identifying terms that should use constraint_iri instead of new term_iri
"""

import re
import warnings
from typing import Optional
import pandas as pd


def deduplicate_proposed_terms(
    proposed_terms: pd.DataFrame,
    warn_threshold: int = 30
) -> pd.DataFrame:
    """
    Apply I-ADOPT compositional deduplication to a gpt_proposed_terms dataframe.

    This prevents term proliferation by collapsing age and phase variants into
    base terms and suggesting when to use constraint_iri instead.

    Parameters
    ----------
    proposed_terms : pd.DataFrame
        DataFrame with columns: term_label, term_definition, term_type, suggested_parent_iri.
        Typically loaded from gpt_proposed_terms.csv.
    warn_threshold : int, default=30
        If the input has more than this many rows, issue a warning about potential
        over-engineering.

    Returns
    -------
    pd.DataFrame
        Deduplicated terms with additional columns:
        - is_base_term: True if this is the canonical base term for a pattern
        - needs_age_facet: True if age variants should use constraint_iri
        - needs_phase_facet: True if phase variants should use constraint_iri
        - collapsed_from: Count of how many variants were collapsed into this term
        - dedup_notes: Explanation of deduplication applied

    Examples
    --------
    >>> import pandas as pd
    >>> proposed = pd.read_csv("work/semantics/gpt_proposed_terms.csv")
    >>> deduped = deduplicate_proposed_terms(proposed)
    >>> # Review collapsed terms
    >>> print(deduped[deduped['collapsed_from'] > 1])
    >>> # Write cleaned output
    >>> deduped.to_csv("work/semantics/gpt_proposed_terms_deduped.csv", index=False)

    Notes
    -----
    Target ratio: For a dictionary with N measurement columns, expect ~N/10 to N/5
    distinct base terms, NOT N terms. If the output still has >30 rows, consider
    further manual review.

    Anti-patterns detected:
    - "Spawners Age 1", "Spawners Age 2", ... patterns → collapsed to "SpawnerCount"
    - Duplicate term_labels across different tables → deduplicated
    - Phase-stratified variants (Ocean X, Terminal X) → collapsed to base term
    """
    if not isinstance(proposed_terms, pd.DataFrame) or len(proposed_terms) == 0:
        return pd.DataFrame({
            'term_label': [],
            'term_definition': [],
            'term_type': [],
            'suggested_parent_iri': [],
            'is_base_term': [],
            'needs_age_facet': [],
            'needs_phase_facet': [],
            'collapsed_from': [],
            'dedup_notes': []
        })

    # Warn if input is suspiciously large
    if len(proposed_terms) > warn_threshold:
        warnings.warn(
            f"gpt_proposed_terms has {len(proposed_terms)} rows (threshold: {warn_threshold}). "
            f"This may indicate over-engineering. Expected: 15-25 base terms for a typical dataset. "
            f"Review for: duplicate terms across tables, age/phase variants that should use constraint_iri.",
            UserWarning
        )

    df = proposed_terms.copy()

    # Normalize term_label for comparison
    df['label_normalized'] = df['term_label'].str.strip().str.lower()
    # Pattern-friendly normalization for regex detection
    df['label_pattern'] = df['label_normalized'].str.replace(r'[^a-z0-9]+', ' ', regex=True).str.strip().str.replace(r'\s+', ' ', regex=True)

    # Detect age-stratified patterns (e.g., "Spawners Age 1", "Catch Age 3")
    df['is_age_variant'] = df['label_pattern'].str.contains(r'\bage\s*\d+\b', case=False, regex=True, na=False)
    df['age_base_label'] = df['label_pattern'].str.replace(r'\s*age\s*\d+\s*', ' ', case=False, regex=True).str.strip().str.replace(r'\s+', ' ', regex=True)

    # Detect phase-stratified patterns (e.g., "Ocean Catch", "Terminal Run")
    phase_prefixes = ['ocean', 'terminal', 'mainstem', 'marine', 'freshwater', r'in[-\s]?river']
    phase_pattern = r'^(?:' + '|'.join(phase_prefixes) + r')\s+'
    df['is_phase_variant'] = df['label_pattern'].str.contains(phase_pattern, case=False, regex=True, na=False)
    df['phase_base_label'] = df['label_pattern'].str.replace(phase_pattern, '', case=False, regex=True).str.strip()

    # Step 1: Remove exact duplicates by term_label (keep first occurrence)
    df = df.drop_duplicates(subset=['label_normalized'], keep='first').reset_index(drop=True)

    # Step 2: Collapse age-stratified variants
    age_groups = (
        df[df['is_age_variant']]
        .groupby('age_base_label')
        .agg({
            'term_label': ['count', 'first'],
            'term_definition': 'first'
        })
        .reset_index()
    )
    age_groups.columns = ['age_base_label', 'count', 'representative_label', 'representative_def']
    age_groups = age_groups[age_groups['count'] > 1]

    # Mark age variants for removal (keep one representative per group)
    df['should_collapse_age'] = False
    for _, row in age_groups.iterrows():
        base = row['age_base_label']
        matching_idx = df[(df['age_base_label'] == base) & df['is_age_variant']].index
        if len(matching_idx) > 1:
            # Keep first, mark rest for removal
            df.loc[matching_idx[1:], 'should_collapse_age'] = True

    # Step 3: Collapse phase-stratified variants (similar logic)
    phase_groups = (
        df[df['is_phase_variant'] & ~df['should_collapse_age']]
        .groupby('phase_base_label')
        .agg({
            'term_label': ['count', 'first']
        })
        .reset_index()
    )
    phase_groups.columns = ['phase_base_label', 'count', 'representative_label']
    phase_groups = phase_groups[phase_groups['count'] > 1]

    df['should_collapse_phase'] = False
    for _, row in phase_groups.iterrows():
        base = row['phase_base_label']
        matching_idx = df[(df['phase_base_label'] == base) & df['is_phase_variant'] & ~df['should_collapse_age']].index
        if len(matching_idx) > 1:
            df.loc[matching_idx[1:], 'should_collapse_phase'] = True

    # Build output
    df['is_base_term'] = ~df['should_collapse_age'] & ~df['should_collapse_phase']
    df['needs_age_facet'] = df['is_age_variant']
    df['needs_phase_facet'] = df['is_phase_variant']

    # Calculate collapsed_from counts
    df['collapsed_from'] = 1
    for _, row in age_groups.iterrows():
        base = row['age_base_label']
        first_idx = df[(df['age_base_label'] == base) & df['is_age_variant'] & df['is_base_term']].index
        if len(first_idx) > 0:
            df.loc[first_idx[0], 'collapsed_from'] = row['count']

    for _, row in phase_groups.iterrows():
        base = row['phase_base_label']
        first_idx = df[(df['phase_base_label'] == base) & df['is_phase_variant'] & df['is_base_term']].index
        if len(first_idx) > 0:
            # Add to existing count (may have both age and phase collapsing)
            current = df.loc[first_idx[0], 'collapsed_from']
            df.loc[first_idx[0], 'collapsed_from'] = current + row['count'] - 1

    # Add deduplication notes
    df['dedup_notes'] = ''
    df.loc[df['needs_age_facet'] & df['is_base_term'], 'dedup_notes'] = \
        'Base term for age variants; use an age-class constraint scheme (propose one if missing)'
    df.loc[df['needs_phase_facet'] & df['is_base_term'] & (df['dedup_notes'] == ''), 'dedup_notes'] = \
        'Base term for phase variants; use a life-phase constraint scheme (propose one if missing)'
    df.loc[~df['is_base_term'], 'dedup_notes'] = 'Collapsed into base term'

    # Filter to base terms only and clean up
    result = df[df['is_base_term']].copy()

    # Select and reorder columns
    columns_to_keep = [
        'term_label', 'term_definition', 'term_type', 'suggested_parent_iri',
        'is_base_term', 'needs_age_facet', 'needs_phase_facet', 'collapsed_from', 'dedup_notes'
    ]
    # Add any extra columns from original that aren't internal working columns
    internal_cols = [
        'label_normalized', 'label_pattern', 'is_age_variant', 'age_base_label',
        'is_phase_variant', 'phase_base_label', 'should_collapse_age', 'should_collapse_phase'
    ]
    extra_cols = [col for col in result.columns if col not in columns_to_keep and col not in internal_cols]
    result = result[columns_to_keep + extra_cols]
    for bool_col in ['is_base_term', 'needs_age_facet', 'needs_phase_facet']:
        if bool_col in result.columns:
            result[bool_col] = result[bool_col].apply(lambda value: bool(value)).astype(object)

    # Report results
    n_removed = len(proposed_terms) - len(result)
    if n_removed > 0:
        print(f"✓ Deduplicated {len(proposed_terms)} -> {len(result)} terms ({n_removed} collapsed/removed).")
        print("ℹ Review 'dedup_notes' column for facet handling guidance.")

    if len(result) > warn_threshold:
        warnings.warn(
            f"After deduplication, still have {len(result)} terms (threshold: {warn_threshold}). "
            f"Consider manual review for additional consolidation opportunities.",
            UserWarning
        )

    return result.reset_index(drop=True)


def suggest_facet_schemes(proposed_terms: pd.DataFrame) -> pd.DataFrame:
    """
    Analyze proposed terms and suggest facet schemes instead of proliferating individual terms.

    Identifies patterns indicating when constraint schemes (age class, life phase, etc.)
    should be created rather than creating many individual terms.

    Parameters
    ----------
    proposed_terms : pd.DataFrame
        DataFrame with term_label column

    Returns
    -------
    pd.DataFrame
        Suggested facet schemes with columns:
        - scheme_name: Name of the proposed scheme
        - scheme_definition: Description of the scheme
        - suggested_concepts: List of concepts for this scheme

    Examples
    --------
    >>> proposed = pd.read_csv("work/semantics/gpt_proposed_terms.csv")
    >>> facets = suggest_facet_schemes(proposed)
    >>> print(facets)
    """
    if not isinstance(proposed_terms, pd.DataFrame) or len(proposed_terms) == 0:
        return pd.DataFrame({
            'scheme_name': [],
            'scheme_definition': [],
            'suggested_concepts': []
        })

    labels = proposed_terms['term_label'].str.lower()
    schemes = []

    # Check for age patterns
    age_matches = labels[labels.str.contains(r'(?:\bage\s*\d+\b|\bage\d+class\b|\bage\d+\b)', case=False, regex=True, na=False)]
    if len(age_matches) >= 3:
        # Extract age numbers
        age_nums = []
        for label in age_matches:
            matches = re.findall(r'age\s*(\d+)', label, re.IGNORECASE)
            age_nums.extend([int(m) for m in matches])

        ages_found = sorted(set(age_nums))
        schemes.append({
            'scheme_name': 'AgeClassScheme',
            'scheme_definition': 'Proposed age-class facets for age-stratified salmon measurements (e.g., Age1Class through Age7Class)',
            'suggested_concepts': [f'Age{age}Class' for age in ages_found]
        })

    # Check for phase patterns
    phase_patterns = ['ocean', 'terminal', 'mainstem', 'marine', 'freshwater']
    phases_found = []
    for phase in phase_patterns:
        # Accept both "ocean" and "oceanphase" style tokens
        if labels.str.contains(rf'\b{phase}(?:phase)?\b', case=False, regex=True, na=False).any():
            phases_found.append(f'{phase.capitalize()}Phase')

    if len(phases_found) >= 2:
        schemes.append({
            'scheme_name': 'LifePhaseScheme',
            'scheme_definition': 'Proposed life-phase facets for location/phase-stratified salmon measurements',
            'suggested_concepts': list(set(phases_found))
        })

    # Check for benchmark level patterns (lower/upper)
    benchmark_levels = []
    if labels.str.contains(r'\blowerbenchmark\b|\blower\s+benchmark\b', case=False, regex=True, na=False).any():
        benchmark_levels.append('LowerBenchmark')
    if labels.str.contains(r'\bupperbenchmark\b|\bupper\s+benchmark\b', case=False, regex=True, na=False).any():
        benchmark_levels.append('UpperBenchmark')

    if len(set(benchmark_levels)) >= 2:
        schemes.append({
            'scheme_name': 'BenchmarkLevelScheme',
            'scheme_definition': 'Proposed benchmark-level facets (lower/upper) used to qualify benchmark values',
            'suggested_concepts': list(set(benchmark_levels))
        })

    return pd.DataFrame(schemes)


__all__ = [
    'deduplicate_proposed_terms',
    'suggest_facet_schemes'
]
