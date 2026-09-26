---
title: Escapement monitoring notes
season: 2024
---

# Escapement monitoring notes

These notes describe how spawner abundance was estimated for the Nechako
and Stuart systems in 2024.  Two spaces follow that full stop on purpose,
and	this line carries a tab, because the excerpt contract keeps whitespace
exactly as the document has it.

## Spawner counts

Spawner counts were made by visual survey from the bank and by mark-recapture
at the counting fence. The SpawnerCount column holds the daily total; the
camelCase name is deliberate, because R lowercases before it would split it.

Adult sockeye were enumerated at the weir every morning. Peak spawner
abundance was reached in the second week of September, and the run was
judged complete when three consecutive surveys found no live fish.

## Water temperature

Water temperature was logged hourly with a submerged sensor near the weir.
Temperatures above 18 degrees Celsius were flagged for the migration
delay analysis, and the sensor was calibrated against a hand-held
thermometer each week.

```r
summary(fish$fork_length_mm)
```

The fence above is kept for a Markdown file: only .rmd and .qmd files have
their fences and front matter dropped.

## Juvenile sampling

Juvenile abundance was estimated at a rotary screw trap operated from
April to June. Smolt counts were expanded by daily trap efficiency,
measured by releasing marked fish upstream of the trap.

Fork length was measured with a measuring board to the nearest millimetre
and weight with a spring scale to the nearest gram. Fish were held in a
recovery bucket before release.

## Field crew notes

The crew stopped for café au lait at the Río Fraser lookout; those two
words carry accented letters so the fixture pins ASCII tokenisation.

Escapement estimates were reconciled against the hatchery broodstock
collection records at the end of the season, and any discrepancy larger
than five percent was investigated before the estimate was finalised.

## Data handling

Counts were entered into the field database each evening and checked
against the paper tally sheets the following morning. Missing days were
interpolated with the area-under-the-curve method used since 1998.

The final spawner abundance estimate is the sum of the daily counts
adjusted for observer efficiency and residence time, and it is reported
with its bootstrap confidence interval.

## Methods in detail

Visual surveys followed the regional escapement protocol: two observers
walked opposite banks, each recording live fish, carcasses and redds in a
waterproof notebook, and the pair reconciled their tallies at the end of
every reach. Polarised glasses were worn on bright days, and a survey was
abandoned when visibility fell below one metre.

Mark-recapture at the counting fence used a two-event design. Fish were
marked with an opercular punch on the first event and examined for the
mark on the second; the Petersen estimator with the Chapman correction
gave the abundance and its variance.

The rotary screw trap was fished for the full migration window except on
nights when debris loads made it unsafe, and those nights were flagged in
the effort table so the expansion could account for them.

## Quality assurance

Every tally sheet was photographed before it left the field, and the
photographs were archived with the season's data package. A second
technician re-entered ten percent of the sheets to estimate the keying
error rate, which was below one percent in 2024.

Temperature loggers were downloaded fortnightly and the series inspected
for drift; a logger whose readings diverged from the hand-held reference
by more than half a degree was replaced and its series truncated at the
last agreeing check.

## Reporting

The season summary reports spawner abundance by stock and by week, the
juvenile abundance by trap with its efficiency, and the water temperature
series as daily means with the number of hours above the 18 degree
threshold. All three are delivered as CSV files with a column dictionary.

The narrative closes with the crew's recommendations for the next season,
which in 2024 were to move the counting fence fifty metres upstream, to
add a third observer on the widest reach, and to replace the oldest
temperature logger before the pre-season calibration.
