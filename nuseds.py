from __future__ import annotations

import pandas as pd


def nuseds_enumeration_method_crosswalk() -> pd.DataFrame:
    """
    Return the curated NuSEDS enumeration-method crosswalk.

    Returns
    -------
    pandas.DataFrame
        Legacy value, method family, candidate ontology term, and review notes.
    """
    term_family = {
        "Bank Walk": "V",
        "Stream Walk": "V",
        "Walk": "V",
        "Boat": "V",
        "Float": "V",
        "Snorkel": "V",
        "Snorkel Swim": "V",
        "Strip Counts": "V",
        "Spot Checks": "V",
        "Dead Pitch": "V",
        "Peak Live and Dead Count": "V",
        "Fence": "FS",
        "Electronic Counters": "FS",
        "Enumeration by Hatchery": "FS",
        "Broodstock Removal": "FS",
        "Fixed Wing Aircraft": "A",
        "Helicopter": "A",
        "Hydroacoustic Station": "S",
        "Trap": "T",
        "Redd Counts": "R",
        "Electroshocking": "P",
        "Tag Recovery": "M",
        "Based on Angling Catch": "P",
        "Biologist/Working Group": "unknown",
        "Other": "unknown",
    }
    ontology_term = {
        "Bank Walk": "gcdfo:VisualGroundCount",
        "Stream Walk": "gcdfo:VisualGroundCount",
        "Walk": "gcdfo:VisualGroundCount",
        "Boat": "gcdfo:VisualGroundCount",
        "Float": "gcdfo:VisualGroundCount",
        "Snorkel": "gcdfo:VisualSnorkelCount",
        "Snorkel Swim": "gcdfo:VisualSnorkelCount",
        "Strip Counts": "gcdfo:VisualGroundCount",
        "Spot Checks": "gcdfo:VisualGroundCount",
        "Dead Pitch": "gcdfo:VisualGroundCount",
        "Peak Live and Dead Count": "gcdfo:VisualGroundCount",
        "Fence": "gcdfo:FixedSiteCensusManual",
        "Electronic Counters": "gcdfo:FixedSiteCensusElectronic",
        "Enumeration by Hatchery": "gcdfo:FixedSiteCensusManual",
        "Broodstock Removal": "gcdfo:FixedSiteCensusManual",
        "Fixed Wing Aircraft": "gcdfo:AerialSurveyCount",
        "Helicopter": "gcdfo:AerialSurveyCount",
        "Hydroacoustic Station": "gcdfo:HydroacousticSonarCount",
        "Trap": "gcdfo:TrapCount",
        "Redd Counts": "gcdfo:ReddCount",
        "Electroshocking": "gcdfo:ElectrofishingCount",
        "Tag Recovery": "gcdfo:MarkRecaptureFieldProgram",
        "Based on Angling Catch": "gcdfo:EnumerationMethod",
        "Biologist/Working Group": pd.NA,
        "Other": pd.NA,
    }
    notes = {
        "Dead Pitch": "Carcass-based visual surveys; often paired with peak/cumulative dead estimation methods.",
        "Peak Live and Dead Count": "Value is analysis-like; prefer capturing peak/cumulative variants under ESTIMATE_METHOD.",
        "Trap": "If trap is non-spanning or efficiency-corrected, use T; fully constraining traps may behave more like fixed-site counting.",
        "Based on Angling Catch": "Catch-based index; treat as a CPUE-style index unless more detail is provided.",
        "Biologist/Working Group": "Not a method. Treat as method-unknown unless a specific field/analysis method is documented elsewhere.",
        "Other": "Treat as method-unknown unless a specific field/analysis method is documented elsewhere.",
    }
    rows = [
        {
            "nuseds_value": value,
            "method_family": family,
            "ontology_term": ontology_term[value],
            "notes": notes.get(value, ""),
        }
        for value, family in term_family.items()
    ]
    return pd.DataFrame(rows).sort_values(["method_family", "nuseds_value"]).reset_index(drop=True)


def nuseds_estimate_method_crosswalk() -> pd.DataFrame:
    """
    Return the curated NuSEDS estimate-method crosswalk.

    Returns
    -------
    pandas.DataFrame
        Legacy value, method family, interpretation, ontology term, and notes.
    """
    rows = [
        ("Fixed Site Census", "FS", "Enumeration device/mode (often stored as estimate method)", "gcdfo:FixedStationTally", ""),
        ("Resistivity Counter", "FS", "Enumeration device/mode (often stored as estimate method)", "gcdfo:FixedStationTally", "Enumerated as a device/mode; ensure bypass/coverage/QA metadata are captured."),
        ("Video Counter", "FS", "Enumeration device/mode (often stored as estimate method)", "gcdfo:FixedStationTally", "Enumerated as a device/mode; ensure QA review rate and uptime/coverage metadata are captured."),
        ("Sonar-ARIS", "S", "Hydroacoustic modelling pipeline", "gcdfo:HydroacousticModelling", ""),
        ("Sonar-DIDSON", "S", "Hydroacoustic modelling pipeline", "gcdfo:HydroacousticModelling", ""),
        ("Mark & Recapture: Petersen", "M", "Mark-recapture estimation", "gcdfo:MarkRecaptureAnalysis", ""),
        ("Mark & Recapture: Jolly-Seber", "M", "Mark-recapture estimation", "gcdfo:MarkRecaptureAnalysis", ""),
        ("Mark & Recapture: Bayesian", "M", "Mark-recapture estimation", "gcdfo:MarkRecaptureAnalysis", ""),
        ("Mark & Recapture: Open Model", "M", "Mark-recapture estimation", "gcdfo:MarkRecaptureAnalysis", ""),
        ("Area Under the Curve", "V", "Visual-series estimation (AUC/peak variants and expansions)", "gcdfo:AreaUnderTheCurve", ""),
        ("Peak Live + Dead", "V", "Visual-series estimation (AUC/peak variants and expansions)", "gcdfo:PeakCountAnalysis", ""),
        ("Peak Live + Cumulative Dead", "V", "Visual-series estimation (AUC/peak variants and expansions)", "gcdfo:PeakCountAnalysis", ""),
        ("(Peak Live+Cum Dead)*Expansion", "V", "Visual-series estimation (AUC/peak variants and expansions)", "gcdfo:ExpansionMathematicalOperations", "Legacy label with known ambiguity in operator precedence; confirm component order before interpretation."),
        ("Peak Live * Expansion", "V", "Visual-series estimation (AUC/peak variants and expansions)", "gcdfo:ExpansionMathematicalOperations", "Legacy label with known ambiguity in operator precedence; confirm component order before interpretation."),
        ("Redd Count", "R", "Redd-based estimation (requires spawners-per-redd conversion)", "gcdfo:ReddExpansionAnalysis", ""),
        ("Cumulative CPUE", "P", "CPUE index", "gcdfo:EstimateMethod", "No specific CPUE estimate concept is currently defined in this scheme; linked at EstimateMethod scheme level."),
        ("Addition/Subtraction", "depends", "Math/expansion operations (depends on base method)", "gcdfo:ExpansionMathematicalOperations", "Use explicit companion logic when combining methods; requires base-method context."),
        ("Multiplication/Division", "depends", "Math/expansion operations (depends on base method)", "gcdfo:ExpansionMathematicalOperations", "Use explicit companion logic when combining methods; requires base-method context."),
        ("Lake Expansion", "depends", "Math/expansion operations (depends on base method)", "gcdfo:ExpansionMathematicalOperations", "Use explicit companion logic when combining methods; requires base-method context."),
        ("Calibrated Time Series", "depends", "Calibrated time series (requires calibration source + diagnostics)", "gcdfo:CalibratedTimeSeries", "Record calibration source years, diagnostics, and revision history."),
        ("Combined Methods", "depends", "Combined-method workflow (requires explicit component listing)", "gcdfo:EstimateMethod", "Decompose into components (e.g., sonar + visual apportionment) and apply conservative classification."),
        ("Insufficient Information", "unknown", "Method unknown/administrative label", pd.NA, ""),
        ("Unknown Estimate Method", "unknown", "Method unknown/administrative label", pd.NA, ""),
        ("Other Estimate Method", "unknown", "Method unknown/administrative label", pd.NA, ""),
        ("Not Applicable", "unknown", "Method unknown/administrative label", pd.NA, ""),
        ("Expert Opinion", "unknown", "Method unknown/administrative label", pd.NA, ""),
        ("Cumulative New", "V", "Visual-series estimation (AUC/peak variants and expansions)", "gcdfo:PeakCountAnalysis", "Legacy dictionary label; mapping is provisional pending local confirmation."),
    ]
    return pd.DataFrame(
        rows,
        columns=["nuseds_value", "method_family", "guidance_interpretation", "ontology_term", "notes"],
    ).sort_values(["method_family", "nuseds_value"]).reset_index(drop=True)


def nuseds_estimate_classification_crosswalk() -> pd.DataFrame:
    """
    Return the NuSEDS ``ESTIMATE_CLASSIFICATION`` crosswalk.

    Maps the classification strings to the released gcdfo Hyatt (1997)
    estimate-type concepts (``gcdfo:Type1``–``gcdfo:Type6``, ``skos:Concept``s
    under ``gcdfo:EstimateType``).

    Two families of values deliberately map to no Type concept, and the
    distinction is recorded here rather than forced:

    * ``NO SURVEY THIS YEAR`` is an absence-of-observation marker, not an
      estimate type — assigning any Hyatt type would assert a survey quality
      for a survey that did not happen. It maps to missing with a note.
      ``UNKNOWN`` likewise stays unmapped as an administrative label.
    * ``RELATIVE: CONSTANT MULTI-YEAR METHODS`` / ``RELATIVE: VARYING
      MULTI-YEAR METHODS`` are real classifications with no released concept
      of their own; they link at scheme level (``gcdfo:EstimateType``), the
      same convention :func:`nuseds_estimate_method_crosswalk` uses for
      ``Cumulative CPUE``.

    Returns
    -------
    pandas.DataFrame
        Columns ``nuseds_value``, ``estimate_type``, ``ontology_term``, and
        ``notes``.
    """
    multi_year_note = (
        "No released concept for the multi-year relative classifications; "
        "linked at EstimateType scheme level. Mint a specific term before "
        "asserting more."
    )
    rows = [
        ("TRUE ABUNDANCE (TYPE-1)", "Type-1", "gcdfo:Type1", ""),
        ("TRUE ABUNDANCE (TYPE-2)", "Type-2", "gcdfo:Type2", ""),
        ("RELATIVE ABUNDANCE (TYPE-3)", "Type-3", "gcdfo:Type3", ""),
        ("RELATIVE ABUNDANCE (TYPE-4)", "Type-4", "gcdfo:Type4", ""),
        ("RELATIVE ABUNDANCE (TYPE-5)", "Type-5", "gcdfo:Type5", ""),
        ("PRESENCE-ABSENCE (TYPE-6)", "Type-6", "gcdfo:Type6", ""),
        (
            "RELATIVE: CONSTANT MULTI-YEAR METHODS",
            pd.NA,
            "gcdfo:EstimateType",
            multi_year_note,
        ),
        (
            "RELATIVE: VARYING MULTI-YEAR METHODS",
            pd.NA,
            "gcdfo:EstimateType",
            multi_year_note,
        ),
        (
            "NO SURVEY THIS YEAR",
            pd.NA,
            pd.NA,
            "Absence-of-observation marker, not an estimate type: no survey "
            "happened, so no Hyatt classification applies. Deliberately "
            "unmapped.",
        ),
        (
            "UNKNOWN",
            pd.NA,
            pd.NA,
            "Administrative unknown label. Treat as classification-unknown "
            "unless documented elsewhere.",
        ),
    ]
    return (
        pd.DataFrame(
            rows,
            columns=["nuseds_value", "estimate_type", "ontology_term", "notes"],
        )
        # Mirrors R's radix (C-collation) order with missing estimate types
        # last, sorted among themselves by nuseds_value.
        .sort_values(["estimate_type", "nuseds_value"], na_position="last")
        .reset_index(drop=True)
    )


__all__ = [
    "nuseds_enumeration_method_crosswalk",
    "nuseds_estimate_classification_crosswalk",
    "nuseds_estimate_method_crosswalk",
]
