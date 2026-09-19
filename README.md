# Filter Pro

A [pyRevit](https://github.com/pyrevitlabs/pyRevit) extension for Revit
2025–2026 that turns a handful of picked elements into either a **selection**
or a **native Revit View Filter**.

Pick some elements, and Filter Pro reads back their Category, Family, Level
and every instance and type parameter they carry. Narrow that down with
multi-select lists and parameter rules, then either select/isolate everything
matching in the active view, or generate a real `ParameterFilterElement` with
a colour override that shows up in *Visibility/Graphics Overrides → Filters*
and stays editable like any hand-made Revit filter.

**Version 2.2.0** · [Changelog](CHANGELOG.md)

---

## Contents

- [Install](#install)
- [Using it](#using-it)
- [Rule operators](#rule-operators)
- [The two engines](#the-two-engines)
- [Known limitations](#known-limitations)
- [Architecture](#architecture)
- [Tests](#tests)
- [Repository layout](#repository-layout)

---

## Install

**Requirements:** Revit 2025 or 2026, and pyRevit 4.8 or newer.

### Option A — register the repo as an extension folder (recommended)

```bash
git clone https://github.com/nadeem8976/Filter-Pro.git
pyrevit extensions paths add "<path-to>/Filter-Pro"
pyrevit reload
```

`Filter.extension` sits one level inside the repo, which is exactly what
`extensions paths add` expects — it scans the folder you give it for
`*.extension` directories.

### Option B — copy it into pyRevit's extensions folder

Copy the `Filter.extension` folder into
`%APPDATA%\pyRevit\Extensions\` and reload pyRevit.

Either way the tool appears on the ribbon as **Filter Pro → Filter Tools →
Filter Pro**.

> **Upgrading from 1.0.0?** Reload pyRevit (`pyrevit reload`) after updating.
> v2.0.0 asks pyRevit for a persistent engine, and that is only picked up on
> a reload.

---

## Using it

1. **Pick elements** — click *Pick Elements…* and select in the model, or
   click *Use Current Selection* if you have already selected them.
2. **Narrow it down** — the Category, Family and Level lists fill with what
   the picked elements actually are, laid out like Revit's own Filter dialog:
   a tick box per row, a count column, **All** / **None** buttons, and a
   running total. They open fully ticked, so untick to narrow. Click **None**
   on a list to drop that axis from the filter entirely. These three are
   always AND-ed.

   Sort any list by **Name** or by **Count**. When a parameter's values are
   all numeric — Offset, Thickness, Length — name sorting sorts *numerically*,
   so you get `20, 100, 1000` rather than the `100, 1000, 20` that a text
   sort would give. A single non-numeric value such as `Varies` falls the
   list back to text ordering rather than guessing.
3. **Add parameter rules** — choose a parameter, an operator and a value,
   then *Add Rule*. Rules combine with AND or OR, your choice.
4. **Produce an output:**
   - *Count Matching* — reports how many elements in the active view match,
     changing nothing. Use it to tune criteria without Revit highlighting and
     zooming each time, and without losing your current selection.
   - *Select Matching* / *Isolate Matching* — acts on the active view now.
   - *Create View Filter* — builds a real, persistent Revit view filter.

> The per-row counts describe **your picked elements**, not the whole model.
> *Count Matching* is the one that measures the active view.

The **Manage View Filters** tab lists every view filter in the project (not
only Filter Pro's) and can select by one, toggle its visibility in the active
view, rename it, or delete it.

### Where the parameter list comes from

The dropdown is the union of each picked element's **instance** parameters and
its **type's** parameters. That is precisely the set Revit itself offers as
available fields when you schedule those categories — so "schedule field
parameters" need no special handling, they are already there.

---

## Rule operators

| Operator | Takes its value from | Applies to |
| --- | --- | --- |
| equals, does not equal | the values list (multi-select) | any parameter |
| contains, does not contain, begins with, ends with | the text box | any parameter |
| is greater than / or equal to, is less than / or equal to | the values list (one value) | numeric parameters only |
| has a value, has no value | — | any parameter |

Two details worth knowing:

- **Multiple values on a negative operator are AND-ed, not OR-ed.**
  "does not equal A or B" has to mean *neither A nor B*; OR-ing would make
  every element match at least one clause and the rule would do nothing.
- **Numeric operators take their threshold from the values list, not the text
  box.** A typed `3000` is ambiguous — project millimetres, or Revit's
  internal feet? A value picked from the list carries its exact raw value, so
  the comparison cannot be silently wrong.

Text matching: `equals`/`does not equal` are exact (their values come from the
list, so they always match character-for-character), while the typed operators
are case-insensitive.

---

## The two engines

The two outputs deliberately use different machinery.

| | Select / Isolate Matching | Create View Filter |
| --- | --- | --- |
| Evaluated by | Python, in `element_matches()` | Revit's native filter engine |
| Scope | elements in the active view | persistent, re-evaluated by Revit |
| Category support | any | only `GetAllFilterableCategories()` |
| Parameter support | any | only `GetFilterableParametersInCommon()` |
| Level across mixed categories | always works | only with a shared Level parameter |

Native View Filters are genuinely more constrained. Rather than emit a filter
that is subtly wrong, Filter Pro drops what it cannot express and reports each
omission in an explicit **Notes** list when the filter is created. Anything
skipped there still works under *Select Matching*.

---

## Known limitations

- **Level in a native filter needs one shared parameter.** There is no single
  Level `BuiltInParameter`; categories variously use *Level*, *Reference
  Level*, *Base Level* or *Schedule Level*. If the selected categories
  disagree, the Level rule is skipped for the native filter (with a warning)
  and you are told to use *Select Matching* instead.
- **Not every category can be filtered natively.** Selected categories
  outside `ParameterFilterUtilities.GetAllFilterableCategories()` are excluded
  from *Create View Filter* only, with a warning.
- **Not every parameter can drive a native filter.** Rules are checked against
  `GetFilterableParametersInCommon` and skipped with a warning if unusable.
- **Display-string collision.** The values list is keyed by the formatted
  display string, so two different raw values that format identically collapse
  into one entry. Numeric comparisons sidestep this by using raw values;
  equality does not.
- **`Create View Filter` needs a view that allows overrides.** Schedules,
  legends and sheets do not — the tool says so rather than failing obscurely.
- **Large views freeze the panel briefly.** *Select Matching* runs
  synchronously inside Revit's API context. The category pre-filter keeps this
  short in practice, but a view with no category filter and hundreds of
  thousands of elements will pause the UI while it scans.

---

## Architecture

```
FilterProWindow (WPF, modeless)          RevitTaskHandler (IExternalEventHandler)
  *_click / *_changed  ── run_in_revit ──▶  queue + ExternalEvent.Raise()
        ▲                                             │
        └──────────── on_ui (Dispatcher) ◀── _do_*(uiapp), in API context
```

A modeless window's WPF handlers run on the UI thread, **outside** Revit's API
context, where starting a transaction or calling `PickObjects` is illegal.
So the window never touches the document directly:

- `*_click` handlers read WPF state, snapshot the criteria and call
  `run_in_revit(action)`.
- `RevitTaskHandler.Execute(uiapp)` runs the queued actions on Revit's own
  main thread, in a valid API context. It drains the whole queue, because
  Revit coalesces several `Raise()` calls into one `Execute()`.
- `_do_*` methods do the document work and push results back with `on_ui()`,
  which marshals onto the WPF dispatcher.
- Exceptions are caught inside `Execute` — an escaping one would surface to
  the user as a Revit crash dialog.

`__persistentengine__ = True` at the top of `script.py` is what keeps the
IronPython engine, and therefore the handler, alive after the command returns.
The live window is parked on the `AppDomain` so re-clicking the ribbon button
re-activates it instead of stacking a second copy.

### Compatibility helpers

- `eid_value()` prefers `ElementId.Value` (Revit 2024+) and falls back to the
  deprecated `IntegerValue`.
- `make_filter_rule()` tries the modern two-argument
  `ParameterFilterRuleFactory` signatures first and falls back to the legacy
  `epsilon` / `caseSensitive` overloads, so the file is not pinned to one
  Revit release.

The code is written for **IronPython 2.7** (pyRevit's default engine for WPF
bundles): no f-strings, no type hints, `.format()` throughout.

---

## Tests

```bash
python3 tests/run_all.py
```

Runs on plain CPython 3 — no Revit, pyRevit or IronPython needed. Revit types
are stubbed in `tests/revit_stubs.py`. The suite covers:

- `script.py` parses, `ui.xaml` is well-formed XML;
- every XAML event handler resolves to a method, and every `self.<widget>`
  reference is backed by a real `x:Name` (`tests/test_ui_wiring.py`);
- the matching engine across all 12 operators, AND/OR combining, the
  positive-OR / negative-AND rule shapes, and the API-version fallbacks
  (`tests/test_logic.py`).

**What these tests cannot cover:** actual behaviour inside Revit. The Revit API
signatures, WPF interop and the external-event lifecycle are stubbed here, so a
run against a real Revit 2025/2026 session remains the necessary final check.

---

## Repository layout

```
Filter-Pro/
├── README.md
├── CHANGELOG.md
├── .gitignore
├── tests/
│   ├── run_all.py            <- run everything
│   ├── revit_stubs.py        <- fake Revit/.NET types
│   ├── test_logic.py         <- matching + rule construction
│   └── test_ui_wiring.py     <- ui.xaml <-> script.py cross-check
└── Filter.extension/                     <- the pyRevit extension
    └── Filter Pro.tab/                   <- ribbon tab   (title: Filter Pro)
        ├── bundle.yaml
        └── Pro.panel/                    <- ribbon panel (title: Filter Tools)
            ├── bundle.yaml
            └── Filter Pro.pushbutton/
                ├── bundle.yaml
                ├── icon.png
                ├── script.py             <- all logic and event handlers
                └── ui.xaml               <- WPF layout
```

The folder names and the ribbon labels are deliberately independent: the
`title:` in each `bundle.yaml` is what Revit displays, so the panel reads
*Filter Tools* rather than *Pro*.

---

## Licence

No licence has been chosen yet — add one before sharing this publicly.
