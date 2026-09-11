# Filter Pro (Revit / pyRevit Add-in) - Project Handoff Summary

Use this as context when continuing this project in Claude Code. It captures
what was built, why, and what's still open.

## 1. Goal

Build a Revit add-in ("Filter Pro") that lets a user:
- Pick elements in the model
- Build filter criteria from their **Category, Family, Level, and any
  parameter** (including "schedule field" parameters)
- Either **select matching elements in the active view**, or
  **create a native Revit View Filter** (with a color override) from those
  criteria
- Manage (select-by / toggle visibility / delete) existing view filters in
  the project

## 2. Key decisions (from clarifying questions asked up front)

The user was asked and answered:
1. Scope: **both** a filter-builder (native View Filters) **and** a
   selection tool, in one panel/window.
2. Selection workflow: **pick elements first**, then the UI shows their
   Category/Family/Level/Parameters to filter down (not criteria-first).
3. Target Revit version: **2025-2026**.

## 3. Platform / architecture chosen

- Built as a **pyRevit extension** (not a compiled C# .NET add-in), because
  the user's uploaded starter files (`bundle.yaml`, `ui.xaml`, `scrip.py` -
  all empty) matched pyRevit's pushbutton-bundle naming convention exactly.
- Engine: **pyRevit's IronPython 2.7 engine** (default for `.pushbutton`
  bundles with a WPF `ui.xaml` - most mature WPF interop). Code is written
  IronPython-2.7-safe (no f-strings, `.format()` used throughout, no type
  hints).
- UI: WPF window (`forms.WPFWindow` subclass) with two tabs:
  - **Build Filter** - pick button, Category/Family/Level multi-select
    lists, a Parameter dropdown + values list + "Add Rule" (rules AND
    together across parameters, values OR within one parameter's rule),
    an active-rules list, then two outputs: "Select Matching Elements in
    View" and "Create View Filter" (name + color override).
  - **Manage View Filters** - lists all `ParameterFilterElement`s in the
    project; buttons to select-by, toggle visibility, delete.

## 4. Two independent matching engines (important design choice)

- **"Select Matching Elements in View"** - pure Python-side evaluation
  (`element_matches()` in script.py) against every element in the active
  view via `FilteredElementCollector`. No Revit-filter-engine restrictions;
  works uniformly for any category/parameter.
- **"Create View Filter"** - must use Revit's native
  `DB.ParameterFilterElement` API, which has real constraints (see
  Section 6). Rather than silently building a filter that's subtly wrong,
  the tool surfaces explicit warnings for anything it had to skip.

### How "schedule field parameters" is implemented
Union of each picked element's **instance** parameters
(`GetOrderedParameters()`) plus its **type's** parameters - this matches
exactly what Revit itself offers as available schedule fields for that
category.

### Family / Level / Category mapping specifics
- **Category** maps directly to `ParameterFilterElement.Create`'s category
  set (no separate rule needed).
- **Family** uses Revit's built-in pseudo-parameter `BuiltInParameter.ELEM_FAMILY_PARAM`,
  matched by the family's `ElementId` (mirrors Revit's own Filters dialog).
- **Level** is best-effort for native filters: `_detect_common_level_param()`
  samples the picked elements to see if all *selected* categories happen to
  share one common Level parameter (`Level`, `Reference Level`,
  `Base Level`, `Schedule Level` are the names checked). If they don't
  agree, the Level rule is **skipped for the native filter with a warning**
  but still works fully for "Select Matching in View" (which reads
  `Element.LevelId` / the level parameter directly, no filter-rule
  involved).

## 5. Files delivered (zip: `FilterPro.extension.zip`)

```
FilterPro.extension/
├── README.md                 <- install/usage/architecture/limitations
├── CHANGELOG.md               <- semver changelog, currently v1.0.0
└── FilterPro.tab/
    ├── bundle.yaml             (tab title: "Filter Pro")
    └── Filter Tools.panel/
        ├── bundle.yaml         (panel title: "Filter Tools")
        └── FilterPro.pushbutton/
            ├── bundle.yaml     (button title/tooltip/author/min_revit_version)
            ├── icon.png        (user-provided icons8-filter-64.png)
            ├── script.py       (~790 lines - all logic + event handlers)
            └── ui.xaml         (WPF layout, ~165 lines)
```

Versioning follows semver (`MAJOR.MINOR.PATCH`), currently **1.0.0**.

## 6. Known limitations (documented in README, worth remembering)

- Level rule in native View Filters requires selected categories to share
  one common Level parameter (see above); otherwise skipped with a warning.
- Not all categories are filterable natively -
  `DB.ParameterFilterUtilities.GetAllFilterableCategories()` is checked and
  non-filterable selected categories are excluded (with a warning) from
  "Create View Filter" only.
- Double (real number) parameter equality in native filters uses an epsilon
  of `1e-6` (Revit internal feet-based units).
- The Python-side "Select Matching" path compares `AsValueString()` display
  strings (not raw typed values) - a deliberate simplicity trade-off; two
  different raw values that format identically would be treated as equal
  there. The native filter path uses exact typed raw values instead.
- Active view must pass `AreGraphicsOverridesAllowed()` for "Create View
  Filter" (schedules/legends etc. don't).

## 7. Compatibility note

`ElementId.IntegerValue` was deprecated in Revit 2024+ in favor of
`ElementId.Value` (Int64). A small helper `eid_value(eid)` in script.py
tries `.Value` first, falls back to `.IntegerValue`, used everywhere instead
of the raw property, so the code degrades gracefully on older Revit hosts
too.

## 8. Validation already done

- `ui.xaml` confirmed well-formed XML (`xml.etree.ElementTree.parse`) after
  fixing an illegal `--` sequence inside two XML comments, and an invalid
  `Height="Auto"` on a `ListBox` (that value is only legal on grid
  `RowDefinition`s, not on a `FrameworkElement.Height`) - fixed by removing
  the fixed `Height` from the global `ListBox` style and giving the
  Category/Family/Level lists explicit heights instead.
- `script.py` confirmed syntactically valid via `ast.parse()` (Python-level
  syntax check only - **not** yet tested inside actual Revit/pyRevit**,
  since that requires a live Revit installation).
- Cross-checked every XAML `x:Name` against the `Click`/`SelectionChanged`
  handler names referenced in `script.py` - all match.

**Not yet done:** actually running the tool inside Revit 2025/2026 with
pyRevit installed. This is the most important next step - IronPython/WPF
data-binding quirks and Revit API signature details (e.g.
`ParameterFilterRuleFactory.CreateEqualsRule` overloads, whether
`ElementType.Family` exists uniformly across all system + loadable family
categories) should be verified against a real Revit session.

## 9. GitHub setup (discussed, not yet executed by the assistant)

Suggested workflow, given as instructions (not run, since this is a local
file the user downloaded):

```bash
unzip FilterPro.extension.zip
cd FilterPro.extension
git init
git branch -M main
# .gitignore for pyc/cache/rvt/rte/bak files was suggested
git add .
git commit -m "Filter Pro v1.0.0 - initial release"
git remote add origin https://github.com/<username>/filter-pro-revit.git
git push -u origin main
git tag -a v1.0.0 -m "Filter Pro v1.0.0"
git push origin v1.0.0
```

Open question posed to the user (unanswered as of this summary): whether the
GitHub repo root should **be** the `FilterPro.extension` folder itself
(pyRevit convention - enables `pyrevit extend ui FilterPro <repo-url>` and
direct `git clone` into the Extensions folder), or a repo that just
*contains* `FilterPro.extension/` as a subfolder (better if more tools get
added later).

## 10. Open items / good next steps for Claude Code

1. Test the extension in an actual Revit 2025 or 2026 + pyRevit session;
   fix any live API/runtime issues (this is the biggest unknown).
2. Decide + execute the GitHub repo layout (Section 9) and actually push.
3. Possible enhancements mentioned but not built: "not equals"/wildcard
   rule types (extension point: `_build_param_rule_filters()` for the
   native path, `element_matches()` for the selection path); a fuller color
   picker instead of the fixed palette in `COLOR_PALETTE_LIST`.
4. User's global preferences to keep applying: ask clarifying questions
   before big changes, explain reasoning/trade-offs, proactively flag
   bugs/edge cases, keep code production-ready and fully commented, use
   semantic versioning (bump `CHANGELOG.md` + `bundle.yaml` version info on
   changes).
