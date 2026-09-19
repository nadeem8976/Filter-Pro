# Changelog

All notable changes to Filter Pro are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/)
(`MAJOR.MINOR.PATCH`).

## [2.2.0] - 2026-09-15

### Added

- **Criteria lists now work like Revit's own Filter dialog.** Category,
  Family, Level and parameter values are tickable rows with a right-aligned
  count column, an **All** / **None** pair under each list, and a running
  `3 of 8 ticked - 2576 element(s)` total.
- **Sort control** for all four lists: by **Name** or by **Count (most
  first)**.
- **Numeric-aware ordering.** When a parameter's values are all numeric, name
  sorting sorts numerically, so an Offset or Thickness list reads
  `20, 100, 1000` rather than the `100, 1000, 20` a text sort gives. Negative
  offsets order correctly. One non-numeric value (`Varies`) falls the whole
  list back to text ordering rather than guessing.

### Changed

- Category, Family and Level open **fully ticked**, matching the native
  dialog: you untick to narrow rather than hunting for what to tick. The
  practical effect is that the default action is "find everything like what I
  picked". Click **None** on a list to drop that axis from the filter
  entirely.
- Parameter values open **unticked**, since they are a refinement you opt
  into rather than a description of the pick you already made.
- Rows are real `CheckBox` controls carrying their key on `Tag`, replacing
  the count-suffixed text labels of 2.1.0. WPF data binding reflects over CLR
  properties that IronPython objects do not expose, so the controls are built
  directly - and the displayed text no longer has to be reconciled with the
  lookup key at all.

## [2.1.0] - 2026-09-15

### Added

- **Element counts throughout the criteria lists.** Category, Family, Level
  and every parameter value now show how many of the picked elements sit
  behind them, e.g. `Walls   (24)`. A value present on both an element and
  its type is counted once, not twice.
- **Count Matching** button and a persistent match readout beside the output
  buttons. It reports how many elements in the active view satisfy the
  current criteria without selecting them, isolating them or disturbing
  whatever you already had selected - so criteria can be tuned without Revit
  repeatedly highlighting and zooming. Select and Isolate update the same
  readout.

### Changed

- List entries are now display labels rather than raw names, so every lookup
  goes through an explicit label-to-key map. Names that themselves contain
  brackets and digits round-trip correctly instead of being mis-parsed.

## [2.0.0] - 2026-09-11

Major, because the execution model changed: the tool now requires a
persistent pyRevit engine and routes every Revit call through an
`ExternalEvent`. Anyone with v1.0.0 installed must reload pyRevit after
updating.

### Fixed

- **Modeless window could not touch Revit at all.** Every button other than
  the initial load ran outside Revit's API context, so transactions and
  `PickObjects` raised *"Starting a transaction from an external application
  running outside of API context is not allowed"*. All Revit work is now
  queued onto an `IExternalEventHandler` and executed by Revit on its own
  main thread. `__persistentengine__ = True` keeps the engine (and therefore
  the handler) alive after the command returns.
- **Family criterion silently did nothing for system families.** Walls,
  floors, ceilings, pipes and ducts have no `Family` element, so the
  `ELEM_FAMILY_PARAM` rule could never be built for them and the criterion
  was dropped with a vague warning. System families are now matched on the
  `Family Name` string parameter instead.
- **Solid fill override could pick a model pattern.** The first solid
  `FillPatternElement` in the document is not necessarily the drafting one;
  a model pattern scales with the model and looked wrong at some view
  scales. Drafting patterns are now preferred.
- **Rule creation was pinned to one set of API overloads.** Revit deprecated
  the trailing `caseSensitive` (string) and `epsilon` (double) arguments of
  `ParameterFilterRuleFactory`. Both signatures are now attempted, so the
  same file works across Revit versions instead of failing on one of them.
- **Unfilterable parameters failed the whole operation.** Rules are now
  validated against `ParameterFilterUtilities.GetFilterableParametersInCommon`
  before the transaction, turning a total failure into a precise per-rule
  warning.
- **Selection scanned every element in the view.** The chosen categories are
  now pushed into a native `ElementMulticategoryFilter` pre-filter, so
  Revit's own engine discards most candidates before the slower per-element
  Python evaluation runs.
- **Adding a filter to a template-controlled view threw.** When a view
  template controls *V/G Overrides Filters*, Filter Pro now detects it and
  offers to apply the filter to the template instead.
- **The document was captured once at start-up.** It is now resolved per
  action from the active `UIApplication`, so switching documents while the
  panel is open cannot write into a stale one.
- Opening the tool twice stacked duplicate windows; the live window is now
  reused.
- Family Editor documents are rejected with a clear message instead of
  failing obscurely.

### Added

- **12 rule operators**, in both matching engines: equals, does not equal,
  contains, does not contain, begins with, ends with, greater than, greater
  than or equal to, less than, less than or equal to, has a value, has no
  value. Multiple values are OR-ed on positive operators and AND-ed on
  negative ones, so *"does not equal A or B"* means what it should.
- **AND/OR combine mode** for the parameter rules.
- **Full colour picker**: RGB sliders, hex entry, live swatch, named presets
  and the native Windows colour dialog when available.
- **Graphic override options**: line weight, halftone, solid surface fill,
  and whether cut patterns are overridden too.
- **Isolate Matching / Reset Isolate** in the active view.
- **Use Current Selection**, so an existing Revit selection can seed the
  criteria without re-picking.
- **Rename** for existing view filters on the Manage tab.
- Empty parameters are now offered in the parameter list, so *has no value*
  rules can be built against them.
- An offline test suite (`tests/`) covering the matching logic, the native
  rule shapes, the API-version fallbacks and the XAML/script wiring. It runs
  on plain CPython 3 with stubbed Revit types.

### Changed

- Numeric comparisons run against raw internal-unit values rather than
  formatted display strings, so `3000 mm` and `3.0 m` cannot disagree.
- Numeric operators take their threshold from the collected values list
  rather than free text, which keeps the comparison unit-safe.
- Errors raised inside the external event are caught and reported in the
  panel rather than reaching Revit as an unhandled-exception dialog.

## [1.0.0] - 2026-09-10

### Added

- Initial release: pick elements, build criteria from Category, Family,
  Level and any instance or type parameter, then either select the matching
  elements in the active view or create a native Revit View Filter with a
  colour override. Manage tab to select by, toggle and delete existing view
  filters.
