"""B-441 evidence: the semantic targets metasalmonpy discovers for B-440's frames.

Prints one tab-separated `Q column field query` row per measurement target,
for the four frames of metasalmon's tests/testthat/test-create-sdp-inference-cues.R
and, after a `--` row, for a list of further header shapes (term and unit
slots only). B-441-discovery-probe.R prints the same rows from metasalmon, so
the two outputs can be compared with `diff` after `sort`.

Run with the interpreter of an environment that has this checkout installed:
    python .hub/evidence/B-441-discovery-probe.py
"""

import pandas as pd

from metasalmonpy.dictionary import infer_dictionary
from metasalmonpy.metadata import normalize_codes, normalize_dictionary
from metasalmonpy.semantics import _semantic_discover_targets

EDGE_NAMES = (
    "fishWeightKg", "forkLengthCm", "body_mass_kg", "water_depth_meters",
    "fork_length_cm", "length_mm", "mm", "total_weight_kg", "mean_length_mm",
    "temp_c", "water_temp_degC", "flow_cms", "area_m2", "length_in_metres",
    "egg_diameter_mm", "girth_cm", "smolt_weight_g", "fish_number_length_mm",
    "catch_kg", "gauge_height_m",
)


def rows(df, force=(), fields=None, all_measurement=False):
    dictionary = infer_dictionary(df, dataset_id="d1", table_id="trawl")
    if all_measurement:
        dictionary["column_role"] = "measurement"
    dictionary.loc[dictionary["column_name"].isin(list(force)), "column_role"] = "measurement"
    dictionary = normalize_dictionary(dictionary)
    targets = pd.DataFrame(
        _semantic_discover_targets(dictionary, normalize_codes(None), pd.DataFrame(), pd.DataFrame())
    )
    for column, field, query in targets[
        ["column_name", "target_sdp_field", "search_query"]
    ].itertuples(index=False):
        if fields is None or field in fields:
            print(f"Q\t{column}\t{field}\t{query}")


rows(pd.DataFrame({
    "fish_length_mm": [512, 600.5, 455],
    "fish_weight_g": [1500.2, 2100, 980],
    "depth_m": [12.5, 30, 45.1],
    "fish_count": [3, 5, 2],
}))
rows(pd.DataFrame({"fish_length": [512, 600.5, 455]}))
rows(pd.DataFrame({
    "spawners_m": [10, 12, 9],
    "catch_g": [4.5, 2.1, 3.3],
    "weight_m": [1.2, 3.4, 5.6],
}))
compound = ("mercury_mg/kg", "mercury_mg_kg", "eggs_per_kg", "fish_length_per_m")
rows(pd.DataFrame({
    "mercury_mg/kg": [0.12, 0.30, 0.08],
    "mercury_mg_kg": [0.12, 0.30, 0.08],
    "eggs_per_kg": [1500, 2100, 1800],
    "fish_length_per_m": [0.2, 0.4, 0.3],
}), force=compound)
print("--")
rows(
    pd.DataFrame({name: [1.5, 2.5, 3.5] for name in EDGE_NAMES}),
    fields=("term_iri", "unit_iri"),
    all_measurement=True,
)
