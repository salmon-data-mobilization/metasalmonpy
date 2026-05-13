from __future__ import annotations

import pandas as pd


def nuseds_enumeration_method_crosswalk() -> pd.DataFrame:
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


__all__ = ["nuseds_enumeration_method_crosswalk", "nuseds_estimate_method_crosswalk"]
