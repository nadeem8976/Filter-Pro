# -*- coding: utf-8 -*-
"""Filter Pro - build Revit selections and View Filters from picked elements.

Workflow
--------
1. Pick elements in the model (or reuse the current selection).
2. Filter Pro reads their Category, Family, Level and every instance + type
   parameter. Because a schedule "field" IS an instance or type parameter,
   this list is exactly the set of fields Revit itself would offer you when
   scheduling those categories.
3. Narrow it down with multi-select lists and parameter rules (12 operators,
   combined with AND or OR).
4. Produce one of two outputs:

   * "Select Matching Elements in View" / "Isolate Matching in View"
     Evaluated in Python (``element_matches``) against the active view.
     It has none of Revit's native-filter restrictions, so it works
     uniformly for any category, any parameter and any operator.

   * "Create View Filter"
     Creates a real ``DB.ParameterFilterElement`` that appears in
     Visibility/Graphics Overrides > Filters and is editable afterwards like
     any hand-made Revit filter. Revit's native filter engine has genuine
     constraints (see README "Known limitations"); rather than silently
     emitting a filter that is subtly wrong, anything that cannot be
     expressed natively is skipped and reported in an explicit warning list.

Architecture note (why this file looks the way it does)
-------------------------------------------------------
The window is **modeless**, so its WPF event handlers run on the UI thread
*outside* Revit's API context. Calling ``Document.Delete``, starting a
``Transaction`` or calling ``Selection.PickObjects`` from there raises
"Starting a transaction from an external application running outside of API
context is not allowed". Every piece of work that touches Revit is therefore
posted to a ``RevitTaskHandler`` (an ``IExternalEventHandler``) and executed
by Revit on its own main thread, in a valid API context. See
``FilterProWindow.run_in_revit``.

Written for IronPython 2.7 (pyRevit's default engine for WPF bundles):
no f-strings, no type hints, ``.format()`` everywhere.
"""

# pyRevit tears the script's engine down as soon as the command returns. A
# modeless window outlives the command, so the engine - and with it the
# ExternalEvent and its handler - must be kept alive. This flag is read by
# pyRevit at load time and MUST stay at module level.
__persistentengine__ = True

__title__ = "Filter Pro"
__author__ = "Filter Pro Add-in"
__version__ = "2.2.0"
__doc__ = "Build selections and native View Filters from Category, Family, Level and any parameter."

import threading
import time
from collections import deque

# pyrevit must be imported before System.Windows.* - importing it loads the
# WPF assemblies (PresentationCore/PresentationFramework/WindowsBase) that the
# colour-picker types below live in.
from pyrevit import DB, forms, script
from pyrevit.framework import List

import System
from System import Action
from System.Windows import GridLength, GridUnitType, TextAlignment, Thickness, Visibility
from System.Windows.Controls import CheckBox, ColumnDefinition, TextBlock
from System.Windows.Controls import Grid as WpfGrid
from System.Windows.Media import Color as MediaColor
from System.Windows.Media import SolidColorBrush

from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI import ExternalEvent, IExternalEventHandler
from Autodesk.Revit.UI.Selection import ObjectType

logger = script.get_logger()


# =============================================================================
# CONSTANTS
# =============================================================================

# Instance-parameter names Revit uses for "Level" across different categories.
# There is no single BuiltInParameter that covers every category, which is
# exactly why the native View Filter Level rule is best-effort - see
# _detect_common_level_param() and the README.
LEVEL_PARAM_NAMES = ("Level", "Reference Level", "Base Level", "Schedule Level")

# Tolerance for Double (real number) comparisons. Revit stores lengths in
# internal feet, so 1e-6 ft is ~0.0003 mm - far below any modelling tolerance.
# Used both by the Python matcher and by the legacy epsilon overloads of
# ParameterFilterRuleFactory.
DOUBLE_EPSILON = 1e-6

# ---- Rule operators ---------------------------------------------------------
# The UI stores these strings verbatim, so they double as their own labels.
OP_EQUALS = "equals"
OP_NOT_EQUALS = "does not equal"
OP_CONTAINS = "contains"
OP_NOT_CONTAINS = "does not contain"
OP_BEGINS = "begins with"
OP_ENDS = "ends with"
OP_GREATER = "is greater than"
OP_GREATER_EQ = "is greater than or equal to"
OP_LESS = "is less than"
OP_LESS_EQ = "is less than or equal to"
OP_HAS_VALUE = "has a value"
OP_NO_VALUE = "has no value"

OPERATORS = [
    OP_EQUALS, OP_NOT_EQUALS,
    OP_CONTAINS, OP_NOT_CONTAINS, OP_BEGINS, OP_ENDS,
    OP_GREATER, OP_GREATER_EQ, OP_LESS, OP_LESS_EQ,
    OP_HAS_VALUE, OP_NO_VALUE,
]

# Operators that take their value(s) from the values list (so the raw, correctly
# united value is known) rather than from the free-text box.
LIST_VALUE_OPS = (OP_EQUALS, OP_NOT_EQUALS)
# Operators that compare numbers. Restricted to numeric parameters so that a
# ">" never silently degrades into a string comparison.
NUMERIC_OPS = (OP_GREATER, OP_GREATER_EQ, OP_LESS, OP_LESS_EQ)
# Operators that match text typed by the user.
TEXT_OPS = (OP_CONTAINS, OP_NOT_CONTAINS, OP_BEGINS, OP_ENDS)
# Operators that need no value at all.
NO_VALUE_OPS = (OP_HAS_VALUE, OP_NO_VALUE)
# Negative operators must be AND-ed across multiple values ("differs from all
# of them"), not OR-ed, or every element would trivially match.
NEGATIVE_OPS = (OP_NOT_EQUALS, OP_NOT_CONTAINS)

# Operator -> ParameterFilterRuleFactory method used for native View Filters.
NATIVE_RULE_METHODS = {
    OP_EQUALS: "CreateEqualsRule",
    OP_NOT_EQUALS: "CreateNotEqualsRule",
    OP_CONTAINS: "CreateContainsRule",
    OP_NOT_CONTAINS: "CreateNotContainsRule",
    OP_BEGINS: "CreateBeginsWithRule",
    OP_ENDS: "CreateEndsWithRule",
    OP_GREATER: "CreateGreaterRule",
    OP_GREATER_EQ: "CreateGreaterOrEqualRule",
    OP_LESS: "CreateLessRule",
    OP_LESS_EQ: "CreateLessOrEqualRule",
    OP_HAS_VALUE: "CreateHasValueParameterRule",
    OP_NO_VALUE: "CreateHasNoValueParameterRule",
}

# BuiltInParameters that carry the family, in preference order.
# Loadable families resolve to a Family element; system families only ever
# have a name string, and Revit exposes that under more than one id.
FAMILY_ID_PARAM_NAMES = ("ELEM_FAMILY_PARAM",)
FAMILY_NAME_PARAM_NAMES = ("ALL_MODEL_FAMILY_NAME", "SYMBOL_FAMILY_NAME_PARAM")

# How the criteria lists are ordered. Mirrors Revit's own Filter dialog,
# which lists categories with a count beside each.
SORT_BY_NAME = "Name"
SORT_BY_COUNT = "Count (most first)"
SORT_MODES = [SORT_BY_NAME, SORT_BY_COUNT]

# How the active rules are combined with each other.
COMBINE_AND = "Match ALL rules (AND)"
COMBINE_OR = "Match ANY rule (OR)"
COMBINE_MODES = [COMBINE_AND, COMBINE_OR]

# Quick-pick colours. The RGB sliders / hex box give full control; these are
# just the shortcuts people reach for most often.
COLOR_PALETTE_LIST = [
    ("Red", (255, 0, 0)),
    ("Orange", (255, 128, 0)),
    ("Yellow", (255, 255, 0)),
    ("Green", (0, 176, 80)),
    ("Cyan", (0, 176, 240)),
    ("Blue", (0, 82, 204)),
    ("Magenta", (204, 0, 153)),
    ("Purple", (112, 48, 160)),
    ("Gray", (128, 128, 128)),
    ("Black", (0, 0, 0)),
]
COLOR_PALETTE = dict(COLOR_PALETTE_LIST)
COLOR_NAMES = [c[0] for c in COLOR_PALETTE_LIST]

# Revit's line-weight scale is 1..16.
LINE_WEIGHTS = [str(i) for i in range(1, 17)]

# Key used to park the live window on the AppDomain so that re-clicking the
# ribbon button re-activates it instead of stacking a second copy. The
# AppDomain outlives pyRevit's script reloads, which module globals do not.
APPDOMAIN_WINDOW_KEY = "FilterPro.ActiveWindow.v2"


class FilterProError(Exception):
    """An expected, user-facing problem (no document open, unusable view...).

    Raised instead of letting an arbitrary exception bubble up, so the error
    reporter can show a plain sentence rather than an IronPython traceback.
    """
    pass


# =============================================================================
# REVIT HELPERS (no UI dependency - individually testable)
# =============================================================================

def eid_value(eid):
    """Return a plain int for an ElementId, or None.

    Revit 2024 deprecated ``ElementId.IntegerValue`` in favour of the 64-bit
    ``ElementId.Value``. We prefer the modern property and fall back, so the
    same file also survives being copied onto an older host.
    """
    if eid is None:
        return None
    try:
        return int(eid.Value)
    except AttributeError:
        return int(eid.IntegerValue)


def get_family_info(el, doc):
    """Return ``(family_name, family_element_id)`` for an element.

    Loadable families resolve to a real ``Family`` element, so both values are
    populated. System families (walls, floors, pipes, ducts...) have no Family
    element at all - they only expose a ``FamilyName`` string - so the id comes
    back None and callers must fall back to a name-based rule. Getting this
    wrong is what made the Family criterion silently do nothing for walls in
    v1.0.0.
    """
    try:
        el_type = doc.GetElement(el.GetTypeId())
    except Exception:
        el_type = None
    if el_type is None:
        return None, None
    try:
        family = el_type.Family
        if family is not None:
            return family.Name, family.Id
    except Exception:
        pass  # system family type: no .Family property
    try:
        family_name = getattr(el_type, "FamilyName", None)
        if family_name:
            return family_name, None
    except Exception:
        pass
    return None, None


def get_level_and_id(el, doc):
    """Return ``(level_name, level_element_id)`` for an element, or (None, None).

    Tries the strongly-typed ``Element.LevelId`` first and falls back to the
    per-category level parameter names, because plenty of categories carry a
    level without exposing ``LevelId``.
    """
    try:
        level_id = getattr(el, "LevelId", None)
    except Exception:
        level_id = None
    if level_id is not None and level_id != DB.ElementId.InvalidElementId:
        level = doc.GetElement(level_id)
        if level is not None:
            return level.Name, level_id

    for param_name in LEVEL_PARAM_NAMES:
        param = el.LookupParameter(param_name)
        if param is not None and param.HasValue \
                and param.StorageType == DB.StorageType.ElementId:
            value_id = param.AsElementId()
            if value_id is not None and value_id != DB.ElementId.InvalidElementId:
                level = doc.GetElement(value_id)
                if level is not None:
                    return level.Name, value_id
    return None, None


def get_raw_display(param, doc):
    """Return ``(raw_value, display_string)`` for a Parameter.

    ``raw_value`` is the storage-typed value the native filter rules need;
    ``display_string`` is what the user sees (formatted with project units)
    and what the Python matcher compares against.
    """
    storage = param.StorageType

    if storage == DB.StorageType.String:
        raw = param.AsString()
        display = raw if raw else param.AsValueString()
        return raw, display

    if storage == DB.StorageType.Integer:
        raw = param.AsInteger()
        display = param.AsValueString()
        if not display:
            display = str(raw)
        return raw, display

    if storage == DB.StorageType.Double:
        raw = param.AsDouble()
        display = param.AsValueString()
        if not display:
            display = str(round(raw, 4))
        return raw, display

    if storage == DB.StorageType.ElementId:
        raw = param.AsElementId()
        display = param.AsValueString()
        if not display and raw is not None and raw != DB.ElementId.InvalidElementId:
            target = doc.GetElement(raw)
            display = target.Name if (target is not None and hasattr(target, "Name")) else None
        return raw, display

    return None, None


def collect_element_parameters(el, doc, param_data):
    """Merge an element's instance + type parameters into ``param_data``.

    ``param_data`` maps::

        param_name -> {
            "storage_type": DB.StorageType,
            "is_type":      bool,     # True if it came from the type
            "param_eid":    ElementId, # Definition.Id, for native filter rules
            "values":       {display_string: raw_value},
            "counts":       {display_string: how many picked elements have it},
        }

    Reading instance *and* type parameters mirrors what Revit offers as
    available schedule fields for those categories, which is why this single
    pass doubles as the "schedule field parameters" source the UI exposes.

    Parameters that are present but empty are still registered (with no
    collected value) so that "has no value" rules can be built against them.

    Call this once per element: ``counts`` is incremented at most once per
    element per (parameter, value), so a value that appears on both the
    instance and its type is not counted twice.
    """
    counted = set()
    sources = [(el, False)]
    try:
        el_type = doc.GetElement(el.GetTypeId())
    except Exception:
        el_type = None
    if el_type is not None:
        sources.append((el_type, True))

    for source, is_type in sources:
        try:
            params = source.GetOrderedParameters()
        except Exception:
            continue
        for param in params:
            try:
                name = param.Definition.Name
            except Exception:
                continue

            entry = param_data.get(name)
            if entry is None:
                try:
                    param_eid = param.Definition.Id
                except Exception:
                    param_eid = None
                entry = {
                    "storage_type": param.StorageType,
                    "is_type": is_type,
                    "param_eid": param_eid,
                    "values": {},
                    "counts": {},
                }
                param_data[name] = entry

            if not param.HasValue:
                continue
            try:
                raw, display = get_raw_display(param, doc)
            except Exception:
                continue
            if display:
                entry["values"][display] = raw
                token = (name, display)
                if token not in counted:
                    counted.add(token)
                    entry["counts"][display] = entry["counts"].get(display, 0) + 1


def get_element_param(el, doc, name):
    """Look up parameter ``name`` on an element (instance first, then its type).

    Returns ``(raw_value, display_string)``; ``(None, None)`` when the
    parameter is absent or empty.
    """
    param = el.LookupParameter(name)
    if param is None or not param.HasValue:
        try:
            el_type = doc.GetElement(el.GetTypeId())
        except Exception:
            el_type = None
        param = el_type.LookupParameter(name) if el_type is not None else None
    if param is None or not param.HasValue:
        return None, None
    return get_raw_display(param, doc)


def _as_number(value):
    """Best-effort float conversion; None when the value is not numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_hex_color(text):
    """Parse "#RRGGBB" / "RRGGBB" into an (r, g, b) tuple, or None.

    Returns None rather than raising for anything unparseable, because it is
    called on every keystroke while the user is still typing the value.
    """
    if not text:
        return None
    cleaned = text.strip().lstrip("#")
    if len(cleaned) != 6:
        return None
    try:
        return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))
    except ValueError:
        return None


def rule_matches(rule, raw, display):
    """Evaluate one parameter rule against one element's parameter value.

    Text comparisons: ``equals``/``does not equal`` are exact, because their
    values are picked from the collected list and therefore always match
    character-for-character. The typed operators (``contains``, ``begins
    with``, ``ends with``) are case-insensitive, because the user typed them.
    """
    op = rule["op"]

    # Presence tests first - they are the only ones defined for an empty value.
    if op == OP_HAS_VALUE:
        return display is not None
    if op == OP_NO_VALUE:
        return display is None
    if display is None:
        return False

    if op in LIST_VALUE_OPS:
        hit = display in rule["values"]
        return (not hit) if op == OP_NOT_EQUALS else hit

    if op in TEXT_OPS:
        needle = (rule["text"] or "").lower()
        haystack = display.lower()
        if op == OP_CONTAINS:
            return needle in haystack
        if op == OP_NOT_CONTAINS:
            return needle not in haystack
        if op == OP_BEGINS:
            return haystack.startswith(needle)
        return haystack.endswith(needle)

    if op in NUMERIC_OPS:
        # Compare raw internal-unit values, never the formatted display string,
        # so "3000 mm" vs "3.0 m" cannot produce a wrong answer.
        left = _as_number(raw)
        right = _as_number(rule["raws"][0]) if rule["raws"] else None
        if left is None or right is None:
            return False
        if op == OP_GREATER:
            return left > right + DOUBLE_EPSILON
        if op == OP_GREATER_EQ:
            return left >= right - DOUBLE_EPSILON
        if op == OP_LESS:
            return left < right - DOUBLE_EPSILON
        return left <= right + DOUBLE_EPSILON

    return False


def sort_entries(counts, sort_mode, raw_values=None):
    """Order a ``{key: count}`` dict for display as ``[(key, count), ...]``.

    ``raw_values`` maps each key to that value's raw (storage) value. When
    every one of them is numeric, sorting by name sorts *numerically* - a
    Thickness or Offset list has to read 20, 100, 1000 and not 100, 1000, 20
    as a plain text sort would give. Falls back to text ordering the moment
    any value is non-numeric.
    """
    keys = list(counts.keys())

    if sort_mode == SORT_BY_COUNT:
        # Ties broken by name so the order is stable between renders.
        keys.sort(key=lambda k: (-counts[k], k))
        return [(k, counts[k]) for k in keys]

    numeric = {}
    if raw_values and keys:
        for key in keys:
            value = _as_number(raw_values.get(key))
            if value is None:
                numeric = None
                break
            numeric[key] = value
    else:
        numeric = None

    if numeric:
        keys.sort(key=lambda k: numeric[k])
    else:
        keys.sort()
    return [(k, counts[k]) for k in keys]


def _build_count_row(key, count):
    """A 'name ....... count' row, laid out like Revit's Filter dialog."""
    name_block = TextBlock()
    name_block.Text = key

    count_block = TextBlock()
    count_block.Text = str(count)
    count_block.TextAlignment = TextAlignment.Right
    count_block.Margin = Thickness(8, 0, 0, 0)
    WpfGrid.SetColumn(count_block, 1)

    name_column = ColumnDefinition()
    name_column.Width = GridLength(1, GridUnitType.Star)
    count_column = ColumnDefinition()
    count_column.Width = GridLength(52)

    row = WpfGrid()
    row.ColumnDefinitions.Add(name_column)
    row.ColumnDefinitions.Add(count_column)
    row.Children.Add(name_block)
    row.Children.Add(count_block)
    return row


def populate_check_list(list_box, entries, is_checked, on_toggle):
    """Fill a ListBox with tickable 'name + count' rows.

    Each row is a real ``CheckBox`` control rather than a data-bound item.
    WPF binding reflects over CLR properties, which plain IronPython objects
    do not expose, so building the controls directly is the route that
    actually works on pyRevit's engine. The real key rides on each CheckBox's
    ``Tag``, so the text shown and the key looked up never drift apart.
    """
    list_box.Items.Clear()
    for key, count in entries:
        box = CheckBox()
        box.Content = _build_count_row(key, count)
        box.Tag = key
        box.IsChecked = bool(is_checked(key))
        box.Checked += on_toggle
        box.Unchecked += on_toggle
        list_box.Items.Add(box)


def checked_keys(list_box):
    """Keys of the ticked rows. An empty set means 'ignore this criterion'."""
    keys = set()
    for item in list_box.Items:
        try:
            if item.IsChecked:
                keys.add(item.Tag)
        except AttributeError:
            continue
    return keys


def set_all_checked(list_box, state):
    """Tick or untick every row (the Check All / Check None buttons)."""
    for item in list_box.Items:
        try:
            item.IsChecked = state
        except AttributeError:
            continue


def describe_rule(rule):
    """One-line human-readable form of a rule, as shown in the active list."""
    operator = rule["op"]
    if operator in NO_VALUE_OPS:
        return "{0}  {1}".format(rule["name"], operator)
    if operator in TEXT_OPS:
        return '{0}  {1}  "{2}"'.format(rule["name"], operator, rule["text"])
    return "{0}  {1}  {2}".format(
        rule["name"], operator, ", ".join(sorted(rule["values"])))


def element_matches(el, doc, cat_names, fam_names, lvl_names, rules, combine_or):
    """Python-side evaluation behind "Select / Isolate Matching in View".

    Deliberately independent of Revit's native filter engine, so it works for
    every category / parameter / operator combination with none of the
    constraints listed in the README.

    Category, Family and Level are always AND-ed (they scope *what* you are
    looking at); ``combine_or`` only controls how the parameter rules combine
    with each other, matching Revit's own filter semantics.
    """
    if cat_names:
        category = el.Category
        if category is None or category.Name not in cat_names:
            return False

    if fam_names:
        family_name, _ = get_family_info(el, doc)
        if not family_name or family_name not in fam_names:
            return False

    if lvl_names:
        level_name, _ = get_level_and_id(el, doc)
        if not level_name or level_name not in lvl_names:
            return False

    if not rules:
        return True

    for rule in rules:
        raw, display = get_element_param(el, doc, rule["name"])
        hit = rule_matches(rule, raw, display)
        if combine_or:
            if hit:
                return True
        elif not hit:
            return False
    return not combine_or


# =============================================================================
# NATIVE VIEW-FILTER RULE CONSTRUCTION
# =============================================================================

def make_filter_rule(param_id, op, value, storage):
    """Build one ``FilterRule`` via ``ParameterFilterRuleFactory``.

    Revit has churned these signatures: the string overloads used to take a
    trailing ``caseSensitive`` bool and the double overloads a trailing
    ``epsilon``, both of which were deprecated in favour of two-argument
    versions. Rather than pinning one Revit release, we try the modern
    signature first and fall back to the legacy one, re-raising the *first*
    error if neither is accepted so the message stays meaningful.
    """
    method_name = NATIVE_RULE_METHODS.get(op)
    if not method_name:
        raise FilterProError("Operator '{0}' has no native equivalent.".format(op))

    method = getattr(DB.ParameterFilterRuleFactory, method_name, None)
    if method is None:
        raise FilterProError(
            "This Revit version's API has no ParameterFilterRuleFactory.{0}.".format(method_name))

    if op in NO_VALUE_OPS:
        return method(param_id)

    attempts = [(param_id, value)]
    if storage == DB.StorageType.Double:
        attempts.append((param_id, value, DOUBLE_EPSILON))
    elif storage == DB.StorageType.String:
        attempts.append((param_id, value, True))

    first_error = None
    for args in attempts:
        try:
            return method(*args)
        except Exception as ex:
            if first_error is None:
                first_error = ex
    raise first_error


def combine_filters(element_filters, use_or):
    """AND/OR a list of ElementFilters into one, or None when the list is empty."""
    if not element_filters:
        return None
    if len(element_filters) == 1:
        return element_filters[0]
    typed = List[DB.ElementFilter](element_filters)
    return DB.LogicalOrFilter(typed) if use_or else DB.LogicalAndFilter(typed)


def build_rule_element_filter(rule):
    """Turn one Filter Pro rule into a native ``ElementFilter``.

    Multiple values on a positive operator are OR-ed ("is any of these"); on a
    negative operator they must be AND-ed ("is none of these"), otherwise every
    element trivially satisfies at least one "does not equal".
    """
    param_id = rule["param_eid"]
    storage = rule["storage_type"]
    op = rule["op"]

    if op in NO_VALUE_OPS:
        return DB.ElementParameterFilter(make_filter_rule(param_id, op, None, storage))

    if op in TEXT_OPS:
        values = [rule["text"]]
        value_storage = DB.StorageType.String
    else:
        values = [v for v in rule["raws"] if v is not None]
        value_storage = storage

    if not values:
        return None

    sub_filters = [
        DB.ElementParameterFilter(make_filter_rule(param_id, op, value, value_storage))
        for value in values
    ]
    return combine_filters(sub_filters, use_or=(op not in NEGATIVE_OPS))


def unique_filter_name(doc, base_name):
    """Return base_name, or base_name_1/_2/... if Revit already has that name.

    Revit rejects duplicate ParameterFilterElement names outright, so this
    turns a hard failure into a rename the user is told about.
    """
    existing = set(f.Name for f in DB.FilteredElementCollector(doc).OfClass(
        DB.ParameterFilterElement))
    if base_name not in existing:
        return base_name
    index = 1
    while "{0}_{1}".format(base_name, index) in existing:
        index += 1
    return "{0}_{1}".format(base_name, index)


def get_filterable_param_ids(doc, category_ids):
    """Parameter ids usable in a View Filter for these categories.

    Returns a set of ints, or None when Revit refuses to answer (in which case
    callers must not pre-reject anything). Checking up-front turns a whole
    failed ``ParameterFilterElement.Create`` into a precise per-rule warning.
    """
    try:
        ids = DB.ParameterFilterUtilities.GetFilterableParametersInCommon(
            doc, List[DB.ElementId](list(category_ids)))
        return set(eid_value(i) for i in ids)
    except Exception as ex:
        logger.debug("GetFilterableParametersInCommon failed: %s", ex)
        return None


def resolve_builtin_param(candidate_names, filterable_params):
    """First of these BuiltInParameters that exists AND can drive a filter.

    Revit exposes "Family Name" through more than one BuiltInParameter and
    which one a given category actually offers to the Filters dialog is not
    something to guess at. Trying the candidates against the filterable set is
    both version-proof and category-proof; returning None means none of them
    works here, so the caller should warn rather than build a broken rule.
    """
    for name in candidate_names:
        builtin = getattr(DB.BuiltInParameter, name, None)
        if builtin is None:
            continue  # this Revit version does not define it
        param_id = DB.ElementId(builtin)
        if filterable_params is None:
            return param_id  # Revit would not tell us; let it try
        if eid_value(param_id) in filterable_params:
            return param_id
    return None


def get_solid_fill_pattern_id(doc):
    """Id of the *drafting* solid fill pattern.

    v1.0.0 took the first solid pattern it found, which can be a **model**
    pattern - those scale with the model rather than the sheet, so the
    override looked wrong at some view scales. Revit's own "Solid fill" is a
    drafting pattern, so prefer that and only fall back to a model one.
    The name is localised, hence the ``IsSolidFill`` test instead of a lookup
    by name.
    """
    fallback = None
    for pattern_element in DB.FilteredElementCollector(doc).OfClass(DB.FillPatternElement):
        try:
            pattern = pattern_element.GetFillPattern()
            if not pattern.IsSolidFill:
                continue
            if pattern.Target == DB.FillPatternTarget.Drafting:
                return pattern_element.Id
            if fallback is None:
                fallback = pattern_element.Id
        except Exception:
            continue
    return fallback


def build_override_settings(doc, rgb, line_weight, use_solid_fill, halftone, apply_to_cut):
    """Assemble the ``OverrideGraphicSettings`` applied to the new filter."""
    color = DB.Color(rgb[0], rgb[1], rgb[2])
    ogs = DB.OverrideGraphicSettings()

    ogs.SetProjectionLineColor(color)
    if line_weight:
        ogs.SetProjectionLineWeight(line_weight)

    if apply_to_cut:
        ogs.SetCutLineColor(color)
        if line_weight:
            ogs.SetCutLineWeight(line_weight)

    if use_solid_fill:
        solid_id = get_solid_fill_pattern_id(doc)
        if solid_id is not None:
            ogs.SetSurfaceForegroundPatternColor(color)
            ogs.SetSurfaceForegroundPatternId(solid_id)
            ogs.SetSurfaceForegroundPatternVisible(True)
            if apply_to_cut:
                ogs.SetCutForegroundPatternColor(color)
                ogs.SetCutForegroundPatternId(solid_id)
                ogs.SetCutForegroundPatternVisible(True)

    ogs.SetHalftone(bool(halftone))
    return ogs


# =============================================================================
# REVIT API CONTEXT PLUMBING
# =============================================================================

class RevitTaskHandler(IExternalEventHandler):
    """Runs queued callables inside a valid Revit API context.

    A modeless window's WPF handlers run outside that context, where the
    document API is illegal. ``ExternalEvent.Raise()`` asks Revit to call
    ``Execute(uiapp)`` on its own main thread at the next idle moment, which
    is a legal context. Each queued item is a ``callable(uiapp)``.
    """

    def __init__(self, on_error):
        self._queue = deque()
        self._lock = threading.Lock()
        self._on_error = on_error

    def post(self, action):
        with self._lock:
            self._queue.append(action)

    def Execute(self, uiapp):
        # Drain the entire queue. Revit coalesces several Raise() calls into a
        # single Execute(), so handling one action per call would silently drop
        # work whenever the user clicked twice in quick succession.
        while True:
            with self._lock:
                if not self._queue:
                    return
                action = self._queue.popleft()
            try:
                action(uiapp)
            except Exception as ex:
                # An exception escaping here surfaces to the user as a Revit
                # crash dialog, so nothing is allowed past this point.
                try:
                    self._on_error(ex)
                except Exception:
                    logger.error("Filter Pro error reporter failed: %s", ex)

    def GetName(self):
        return "Filter Pro external event handler"


def require_document(uiapp):
    """Return ``(uidoc, doc)`` for the active document, or raise FilterProError.

    Resolved per action rather than cached at start-up, so switching documents
    while the panel is open cannot leave it writing into a stale one.
    """
    uidoc = getattr(uiapp, "ActiveUIDocument", None)
    if uidoc is None or uidoc.Document is None:
        raise FilterProError("No Revit project is open.")
    doc = uidoc.Document
    if doc.IsFamilyDocument:
        raise FilterProError(
            "Filter Pro works on projects, not on the Family Editor. "
            "Open a project and try again.")
    return uidoc, doc


def require_graphics_view(doc):
    """Return the active view, or raise if it cannot carry graphic overrides."""
    view = doc.ActiveView
    if view is None:
        raise FilterProError("There is no active view.")
    if not view.AreGraphicsOverridesAllowed():
        raise FilterProError(
            "The active view ('{0}') does not support view filters or graphic "
            "overrides. Switch to a plan, section, elevation or 3D view and "
            "try again.".format(view.Name))
    return view


def run_transaction(doc, name, func):
    """Run ``func()`` inside a Revit transaction, rolling back on any error."""
    transaction = DB.Transaction(doc, name)
    transaction.Start()
    try:
        result = func()
        transaction.Commit()
        return result
    except:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
        raise


def resolve_filter_host_view(doc, view):
    """Return ``(host_view, template_name)`` for applying a filter.

    When a view template controls "V/G Overrides Filters", adding a filter to
    the view itself throws. The template is the only place the filter can
    actually live, so report that to the caller and let the user decide.
    ``template_name`` is None when the view accepts filters directly.
    """
    template_id = view.ViewTemplateId
    if template_id is None or template_id == DB.ElementId.InvalidElementId:
        return view, None
    template = doc.GetElement(template_id)
    if template is None:
        return view, None
    try:
        non_controlled = set(
            eid_value(i) for i in template.GetNonControlledTemplateParameterIds())
    except Exception:
        return view, None
    filters_param = eid_value(DB.ElementId(DB.BuiltInParameter.VIS_GRAPHICS_FILTERS))
    if filters_param in non_controlled:
        return view, None  # the template leaves filters to the view
    return template, template.Name


def build_candidate_collector(doc, view_id, category_ids):
    """Elements in the view, pre-narrowed to the chosen categories.

    The category pre-filter runs in Revit's native (C++) filter engine, so on a
    busy view it removes the overwhelming majority of candidates before the
    per-element Python evaluation - which calls ``LookupParameter`` and is
    orders of magnitude slower - ever sees them.
    """
    collector = DB.FilteredElementCollector(doc, view_id).WhereElementIsNotElementType()
    if category_ids:
        try:
            collector = collector.WherePasses(
                DB.ElementMulticategoryFilter(List[DB.ElementId](list(category_ids))))
        except Exception as ex:
            logger.debug("Category pre-filter unavailable, scanning all: %s", ex)
    return collector


# Optional: the native Windows colour dialog gives users the custom-colour grid
# and eyedropper they expect. The RGB sliders and hex box below are fully
# sufficient on their own, so if the WinForms assemblies are unavailable the
# button is simply hidden rather than the tool failing to load.
WINFORMS_COLOR_DIALOG_AVAILABLE = False
try:
    import clr
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    from System.Windows.Forms import ColorDialog as WinFormsColorDialog
    from System.Windows.Forms import DialogResult as WinFormsDialogResult
    from System.Drawing import Color as DrawingColor
    WINFORMS_COLOR_DIALOG_AVAILABLE = True
except Exception as _winforms_error:      # pragma: no cover - host dependent
    logger.debug("Native colour dialog unavailable: %s", _winforms_error)


# =============================================================================
# MAIN WINDOW
# =============================================================================

class FilterProWindow(forms.WPFWindow):
    """The Filter Pro panel.

    Threading contract, observed throughout:

    * ``*_click`` / ``*_changed`` methods run on the WPF UI thread. They may
      only read WPF state and post work with :meth:`run_in_revit`.
    * ``_do_*`` methods run inside the external event, i.e. in a valid Revit
      API context. They may use the document freely but must push any UI
      update back through :meth:`on_ui`.
    """

    # Class-level defaults: WPF raises SelectionChanged/ValueChanged while the
    # XAML is still loading, i.e. before __init__ has created the instance
    # attributes, so the handlers need something safe to read.
    _suspend_color_sync = True
    _ready = False
    _rgb = (255, 0, 0)

    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)

        # --- Revit API context bridge ---------------------------------------
        self._handler = RevitTaskHandler(self.report_error)
        self._external_event = ExternalEvent.Create(self._handler)

        # --- criteria state, all rebuilt on every pick ----------------------
        self.picked_ids = []
        self.category_ids_map = {}   # category name  -> ElementId
        self.family_ids_map = {}     # family name    -> {"ids": {int: ElementId}}
        self.level_ids_map = {}      # level name     -> ElementId
        self.param_data = {}         # parameter name -> see collect_element_parameters
        self.active_rules = []       # list of rule dicts

        # How many picked elements sit behind each entry in each list.
        self.category_counts = {}
        self.family_counts = {}
        self.level_counts = {}
        self.value_counts = {}

        self.filters_map = {}        # Manage tab: filter name -> ElementId

        # --- static UI content ----------------------------------------------
        self.cmbOperator.ItemsSource = OPERATORS
        self.cmbOperator.SelectedIndex = 0
        self.cmbCombine.ItemsSource = COMBINE_MODES
        self.cmbCombine.SelectedIndex = 0
        self.cmbSort.ItemsSource = SORT_MODES
        self.cmbSort.SelectedIndex = 0
        self.cmbColorPreset.ItemsSource = COLOR_NAMES
        self.cmbLineWeight.ItemsSource = LINE_WEIGHTS
        self.cmbLineWeight.SelectedItem = "6"

        if not WINFORMS_COLOR_DIALOG_AVAILABLE:
            self.btnCustomColor.Visibility = Visibility.Collapsed

        # Wired here rather than as a XAML attribute on the root Window: the
        # code path is identical but does not depend on how LoadComponent
        # treats events declared on the root element itself.
        self.Closed += self.window_closed

        self._ready = True
        self._suspend_color_sync = False
        self.set_color((255, 0, 0), sync_preset=True)
        self.update_value_input_state()

        self.log("Ready. Pick elements - or use the current selection - to begin.")
        self.run_in_revit(self._do_refresh_filters)

    # -------------------------------------------------------- infrastructure --

    def run_in_revit(self, action):
        """Queue ``action(uiapp)`` to run in a valid Revit API context."""
        self._handler.post(action)
        self._external_event.Raise()

    def on_ui(self, func):
        """Run ``func()`` on the WPF UI thread (inline if already on it)."""
        try:
            if self.Dispatcher.CheckAccess():
                func()
            else:
                self.Dispatcher.Invoke(Action(func))
        except Exception as ex:
            logger.error("Filter Pro UI update failed: %s", ex)

    def report_error(self, error):
        """Surface an error raised by a queued Revit action.

        Expected problems get a plain sentence; anything else also goes to the
        pyRevit output log with its traceback so it can be diagnosed.
        """
        message = str(error)
        if not isinstance(error, FilterProError):
            logger.error("Filter Pro action failed", exc_info=True)
            message = "Filter Pro hit an unexpected error:\n\n{0}".format(message)
        self.on_ui(lambda: self._show_error(message))

    def _show_error(self, message):
        self.log(message.replace("\n", " ")[:300])
        forms.alert(message, title="Filter Pro")

    def log(self, message):
        self.txtLog.Text = "[{0}] {1}".format(time.strftime("%H:%M:%S"), message)

    def window_closed(self, sender, args):
        """Release the external event and forget the singleton instance."""
        try:
            self._external_event.Dispose()
        except Exception:
            pass
        try:
            System.AppDomain.CurrentDomain.SetData(APPDOMAIN_WINDOW_KEY, None)
        except Exception:
            pass

    # ------------------------------------------------------- colour picker --

    def set_color(self, rgb, sync_preset=False):
        """Push an (r, g, b) triple into every colour control at once.

        The sliders, the hex box and the preset combo all edit the same value
        and all raise change events, so a re-entrancy guard is what stops them
        from fighting each other.
        """
        red, green, blue = [max(0, min(255, int(round(c)))) for c in rgb]
        self._rgb = (red, green, blue)

        self._suspend_color_sync = True
        try:
            self.sliderR.Value = red
            self.sliderG.Value = green
            self.sliderB.Value = blue
            self.lblR.Text = str(red)
            self.lblG.Text = str(green)
            self.lblB.Text = str(blue)
            self.txtHex.Text = "#{0:02X}{1:02X}{2:02X}".format(red, green, blue)
            self.rectSwatch.Fill = SolidColorBrush(
                MediaColor.FromRgb(System.Byte(red), System.Byte(green), System.Byte(blue)))
            if sync_preset:
                match = None
                for name, value in COLOR_PALETTE_LIST:
                    if value == self._rgb:
                        match = name
                        break
                self.cmbColorPreset.SelectedItem = match
        finally:
            self._suspend_color_sync = False

    def color_preset_changed(self, sender, args):
        if self._suspend_color_sync or not self._ready:
            return
        rgb = COLOR_PALETTE.get(self.cmbColorPreset.SelectedItem)
        if rgb:
            self.set_color(rgb)

    def color_slider_changed(self, sender, args):
        if self._suspend_color_sync or not self._ready:
            return
        self.set_color((self.sliderR.Value, self.sliderG.Value, self.sliderB.Value),
                       sync_preset=True)

    def hex_text_changed(self, sender, args):
        if self._suspend_color_sync or not self._ready:
            return
        rgb = parse_hex_color(self.txtHex.Text)
        if rgb is None:
            return  # keep typing - invalid intermediate states are normal
        self.set_color(rgb, sync_preset=True)

    def custom_color_click(self, sender, args):
        """Open the native Windows colour dialog, seeded with the current colour."""
        if not WINFORMS_COLOR_DIALOG_AVAILABLE:
            return
        try:
            dialog = WinFormsColorDialog()
            dialog.FullOpen = True
            dialog.AnyColor = True
            dialog.Color = DrawingColor.FromArgb(
                self._rgb[0], self._rgb[1], self._rgb[2])
            if dialog.ShowDialog() == WinFormsDialogResult.OK:
                self.set_color((dialog.Color.R, dialog.Color.G, dialog.Color.B),
                               sync_preset=True)
        except Exception as ex:
            logger.error("Colour dialog failed: %s", ex)
            forms.alert("Could not open the colour dialog. Use the R/G/B sliders "
                        "or the hex box instead.", title="Filter Pro")

    # -------------------------------------------------------- pick elements --

    def pick_elements_click(self, sender, args):
        # The panel is hidden so it cannot sit on top of the model while Revit
        # is in pick mode; _do_pick shows it again when the pick ends.
        self.Hide()
        self.run_in_revit(self._do_pick)

    def use_selection_click(self, sender, args):
        self.run_in_revit(self._do_use_current_selection)

    def _do_pick(self, uiapp):
        try:
            uidoc, doc = require_document(uiapp)
            try:
                references = uidoc.Selection.PickObjects(
                    ObjectType.Element,
                    "Pick elements, then click Finish on the options bar - Esc to cancel")
                element_ids = [ref.ElementId for ref in references]
            except OperationCanceledException:
                element_ids = []
        finally:
            # Always restore the window, including when the pick threw.
            self.on_ui(self._restore_window)

        if not element_ids:
            self.on_ui(lambda: self.log("Pick cancelled - nothing was selected."))
            return
        self._ingest_picked(doc, element_ids)

    def _do_use_current_selection(self, uiapp):
        uidoc, doc = require_document(uiapp)
        element_ids = list(uidoc.Selection.GetElementIds())
        if not element_ids:
            raise FilterProError(
                "Nothing is selected in Revit. Select some elements first, or "
                "use 'Pick Elements'.")
        self._ingest_picked(doc, element_ids)

    def _restore_window(self):
        self.Show()
        try:
            self.Activate()
        except Exception:
            pass

    def _ingest_picked(self, doc, element_ids):
        """Read everything we need off the picked elements (API context)."""
        category_map = {}
        family_map = {}
        level_map = {}
        param_data = {}
        category_counts = {}
        family_counts = {}
        level_counts = {}

        def tally(counts, key):
            counts[key] = counts.get(key, 0) + 1

        for element_id in element_ids:
            element = doc.GetElement(element_id)
            if element is None:
                continue

            try:
                if element.Category is not None and element.Category.Name:
                    category_map[element.Category.Name] = element.Category.Id
                    tally(category_counts, element.Category.Name)
            except Exception:
                pass

            family_name, family_id = get_family_info(element, doc)
            if family_name:
                entry = family_map.setdefault(family_name, {"ids": {}})
                if family_id is not None:
                    entry["ids"][eid_value(family_id)] = family_id
                tally(family_counts, family_name)

            level_name, level_id = get_level_and_id(element, doc)
            if level_name:
                level_map[level_name] = level_id
                tally(level_counts, level_name)

            collect_element_parameters(element, doc, param_data)

        self.picked_ids = list(element_ids)
        self.category_ids_map = category_map
        self.family_ids_map = family_map
        self.level_ids_map = level_map
        self.param_data = param_data
        self.category_counts = category_counts
        self.family_counts = family_counts
        self.level_counts = level_counts
        self.active_rules = []

        self.on_ui(self._apply_picked_to_ui)

    # ------------------------------------------------------- criteria lists --

    CHECK_LISTS = ("lstCategory", "lstFamily", "lstLevel", "lstParamValues")

    def render_check_list(self, list_box, counts, raw_values=None, keep=None):
        """(Re)draw one criteria list.

        ``keep`` is the set of keys that should stay ticked; None ticks
        everything, which is how Revit's own Filter dialog opens - you untick
        to narrow rather than hunting for what to tick.
        """
        entries = sort_entries(counts, self.cmbSort.SelectedItem, raw_values)
        if keep is None:
            is_checked = lambda key: True
        else:
            is_checked = lambda key: key in keep
        populate_check_list(list_box, entries, is_checked, self.check_toggled)

    def current_param_raw_values(self):
        """Raw values for the selected parameter, for numeric-aware sorting."""
        entry = self.param_data.get(self.cmbParam.SelectedItem)
        return entry["values"] if entry else None

    def sort_changed(self, sender, args):
        """Re-order every list, preserving what is ticked."""
        if not self._ready:
            return
        self.render_check_list(self.lstCategory, self.category_counts,
                               keep=checked_keys(self.lstCategory))
        self.render_check_list(self.lstFamily, self.family_counts,
                               keep=checked_keys(self.lstFamily))
        self.render_check_list(self.lstLevel, self.level_counts,
                               keep=checked_keys(self.lstLevel))
        self.render_check_list(self.lstParamValues, self.value_counts,
                               self.current_param_raw_values(),
                               keep=checked_keys(self.lstParamValues))
        self.refresh_list_summaries()

    def check_toggled(self, sender, args):
        if self._ready:
            self.refresh_list_summaries()

    def _list_by_name(self, name):
        return dict((n, getattr(self, n)) for n in self.CHECK_LISTS)[name]

    def check_all_click(self, sender, args):
        set_all_checked(self._list_by_name(str(sender.Tag)), True)
        self.refresh_list_summaries()

    def check_none_click(self, sender, args):
        set_all_checked(self._list_by_name(str(sender.Tag)), False)
        self.refresh_list_summaries()

    @staticmethod
    def _summarise(counts, keys):
        """'3 of 8 ticked - 2576 element(s)', echoing the native dialog's total."""
        if not counts:
            return ""
        total = sum(counts.get(key, 0) for key in keys)
        return "{0} of {1} ticked - {2} element(s)".format(
            len(keys), len(counts), total)

    def refresh_list_summaries(self):
        self.lblCategorySummary.Text = self._summarise(
            self.category_counts, checked_keys(self.lstCategory))
        self.lblFamilySummary.Text = self._summarise(
            self.family_counts, checked_keys(self.lstFamily))
        self.lblLevelSummary.Text = self._summarise(
            self.level_counts, checked_keys(self.lstLevel))
        self.lblValueSummary.Text = self._summarise(
            self.value_counts, checked_keys(self.lstParamValues))

    def _apply_picked_to_ui(self):
        self.render_check_list(self.lstCategory, self.category_counts)
        self.render_check_list(self.lstFamily, self.family_counts)
        self.render_check_list(self.lstLevel, self.level_counts)
        self.cmbParam.ItemsSource = sorted(self.param_data.keys())
        if self.cmbParam.Items.Count > 0:
            self.cmbParam.SelectedIndex = 0
        else:
            self.value_counts = {}
            self.lstParamValues.Items.Clear()
        self.refresh_active_rules_list()
        self.refresh_list_summaries()

        self.lblPickedCount.Text = "{0} element(s) picked.".format(len(self.picked_ids))
        self.log(
            "Picked {0} element(s) -> {1} categor(ies), {2} famil(ies), "
            "{3} level(s), {4} parameter(s).".format(
                len(self.picked_ids), len(self.category_ids_map),
                len(self.family_ids_map), len(self.level_ids_map),
                len(self.param_data)))

    def clear_pick_click(self, sender, args):
        self.picked_ids = []
        self.category_ids_map = {}
        self.family_ids_map = {}
        self.level_ids_map = {}
        self.param_data = {}
        self.active_rules = []
        self.category_counts = {}
        self.family_counts = {}
        self.level_counts = {}
        self.value_counts = {}

        # The four criteria lists are populated through Items (they hold real
        # CheckBox controls); mixing that with ItemsSource would throw.
        for name in self.CHECK_LISTS:
            getattr(self, name).Items.Clear()
        self.lstActiveRules.ItemsSource = None
        self.cmbParam.ItemsSource = None
        self.txtRuleValue.Text = ""

        self.refresh_list_summaries()
        self.lblPickedCount.Text = "No elements picked yet."
        self.lblMatchCount.Text = ""
        self.log("Criteria cleared.")

    # ---------------------------------------------------- parameter rules --

    def param_selected_changed(self, sender, args):
        name = self.cmbParam.SelectedItem
        entry = self.param_data.get(name) if name else None
        if entry:
            self.value_counts = entry["counts"]
            # A fresh parameter starts with nothing ticked: its values are a
            # refinement you opt into, unlike Category/Family/Level which
            # describe the pick you already made.
            self.render_check_list(self.lstParamValues, entry["counts"],
                                   entry["values"], keep=set())
        else:
            self.value_counts = {}
            self.lstParamValues.Items.Clear()
        self.refresh_list_summaries()
        self.update_value_input_state()

    def operator_changed(self, sender, args):
        self.update_value_input_state()

    def param_value_selection_changed(self, sender, args):
        """Mirror the first selected value into the text box.

        Handy for the text operators: pick a real value, then trim it down to
        the fragment you actually want to match on.
        """
        if not self._ready:
            return
        selected = list(self.lstParamValues.SelectedItems)
        if selected and self.cmbOperator.SelectedItem in TEXT_OPS:
            # Rows are CheckBoxes; the real value rides on Tag.
            tag = getattr(selected[0], "Tag", None)
            if tag is not None:
                self.txtRuleValue.Text = str(tag)

    def update_value_input_state(self):
        """Enable only the input that the chosen operator actually reads.

        Numeric operators deliberately take their threshold from the values
        list rather than the text box: a typed "3000" is ambiguous (project
        units vs Revit's internal feet), whereas a listed value carries its
        exact raw value with it, so the comparison cannot be silently wrong.
        """
        if not self._ready:
            return
        operator = self.cmbOperator.SelectedItem
        wants_text = operator in TEXT_OPS
        wants_list = operator in LIST_VALUE_OPS or operator in NUMERIC_OPS

        self.txtRuleValue.IsEnabled = wants_text
        self.lstParamValues.IsEnabled = wants_list

        if operator in NO_VALUE_OPS:
            hint = "No value needed for this operator."
        elif wants_text:
            hint = "Type the text to match (case-insensitive)."
        elif operator in NUMERIC_OPS:
            hint = "Tick ONE value to compare against."
        else:
            hint = "Tick one or more values in the list."
        self.lblValueHint.Text = hint

    def add_rule_click(self, sender, args):
        name = self.cmbParam.SelectedItem
        operator = self.cmbOperator.SelectedItem
        if not name or name not in self.param_data:
            forms.alert("Choose a parameter first.", title="Filter Pro")
            return

        entry = self.param_data[name]
        storage = entry["storage_type"]
        selected_values = sorted(checked_keys(self.lstParamValues))
        text_value = (self.txtRuleValue.Text or "").strip()

        # --- validate the operator against the parameter and the input ------
        if operator in NUMERIC_OPS:
            if storage not in (DB.StorageType.Integer, DB.StorageType.Double):
                forms.alert(
                    "'{0}' only applies to numeric parameters. '{1}' stores "
                    "{2} values.".format(operator, name, storage),
                    title="Filter Pro")
                return
            if len(selected_values) != 1:
                forms.alert(
                    "Tick exactly one value in the list to compare against.",
                    title="Filter Pro")
                return
        elif operator in LIST_VALUE_OPS:
            if not selected_values:
                forms.alert("Tick at least one value in the list.",
                            title="Filter Pro")
                return
        elif operator in TEXT_OPS:
            if not text_value:
                forms.alert("Type the text this rule should match.",
                            title="Filter Pro")
                return
            selected_values = []

        if operator in NO_VALUE_OPS:
            selected_values = []
            text_value = None

        rule = {
            "name": name,
            "op": operator,
            "values": selected_values,
            "raws": [entry["values"].get(v) for v in selected_values],
            "text": text_value if operator in TEXT_OPS else None,
            "storage_type": storage,
            "param_eid": entry["param_eid"],
            "is_type": entry["is_type"],
        }

        # One rule per (parameter, operator) pair: re-adding replaces, so
        # tweaking a rule does not quietly stack contradictory copies. Two
        # different operators on the same parameter remain valid (a range).
        replaced = False
        for index, existing in enumerate(self.active_rules):
            if existing["name"] == name and existing["op"] == operator:
                self.active_rules[index] = rule
                replaced = True
                break
        if not replaced:
            self.active_rules.append(rule)

        self.refresh_active_rules_list()
        self.log("Rule {0}: {1}".format(
            "updated" if replaced else "added", describe_rule(rule)))

    def remove_rule_click(self, sender, args):
        index = self.lstActiveRules.SelectedIndex
        if index < 0:
            forms.alert("Select a rule to remove.", title="Filter Pro")
            return
        removed = self.active_rules.pop(index)
        self.refresh_active_rules_list()
        self.log("Rule removed: {0}".format(describe_rule(removed)))

    def clear_rules_click(self, sender, args):
        self.active_rules = []
        self.refresh_active_rules_list()
        self.log("All parameter rules cleared.")

    def refresh_active_rules_list(self):
        # Index-aligned with self.active_rules, which is what makes
        # lstActiveRules.SelectedIndex a valid index into it.
        self.lstActiveRules.ItemsSource = [describe_rule(r) for r in self.active_rules]

    # ------------------------------------------------------ criteria access --

    def read_criteria(self):
        """Snapshot the current criteria off the UI thread's controls.

        Called from the UI thread before posting work to Revit, so the queued
        action never reaches back into WPF for its inputs.
        """
        return {
            "categories": checked_keys(self.lstCategory),
            "families": checked_keys(self.lstFamily),
            "levels": checked_keys(self.lstLevel),
            "rules": list(self.active_rules),
            "combine_or": self.cmbCombine.SelectedItem == COMBINE_OR,
        }

    def require_criteria(self):
        """Return a criteria snapshot, or None (after alerting) if it is empty."""
        if not self.picked_ids:
            forms.alert("Pick elements first so Filter Pro knows which fields "
                        "to offer.", title="Filter Pro")
            return None
        criteria = self.read_criteria()
        if not (criteria["categories"] or criteria["families"]
                or criteria["levels"] or criteria["rules"]):
            forms.alert("Select at least one Category, Family or Level, or add "
                        "a parameter rule.", title="Filter Pro")
            return None
        return criteria

    def find_matches(self, doc, view, criteria):
        """Evaluate the criteria against the active view. Returns ElementIds."""
        category_ids = [self.category_ids_map[name]
                        for name in criteria["categories"]
                        if name in self.category_ids_map]
        collector = build_candidate_collector(doc, view.Id, category_ids)

        matches = []
        for element in collector:
            try:
                if element_matches(element, doc,
                                   criteria["categories"], criteria["families"],
                                   criteria["levels"], criteria["rules"],
                                   criteria["combine_or"]):
                    matches.append(element.Id)
            except Exception as ex:
                # One awkward element must not abort the whole scan.
                logger.debug("Skipped element during match: %s", ex)
        return matches

    # --------------------------------------------------- select / isolate --

    def select_matching_click(self, sender, args):
        criteria = self.require_criteria()
        if criteria is not None:
            self.run_in_revit(lambda uiapp: self._do_select_matching(uiapp, criteria))

    def _do_select_matching(self, uiapp, criteria):
        uidoc, doc = require_document(uiapp)
        view = doc.ActiveView
        if view is None:
            raise FilterProError("There is no active view.")

        matches = self.find_matches(doc, view, criteria)
        if not matches:
            self.on_ui(lambda: self.log(
                "0 elements matched in view '{0}'.".format(view.Name)))
            raise FilterProError("No elements in the active view match these criteria.")

        uidoc.Selection.SetElementIds(List[DB.ElementId](matches))
        count = len(matches)
        self.on_ui(lambda: (self.set_match_count(count, view.Name),
                            self.log("{0} element(s) matched and selected in "
                                     "'{1}'.".format(count, view.Name))))

    def count_matching_click(self, sender, args):
        criteria = self.require_criteria()
        if criteria is not None:
            self.run_in_revit(lambda uiapp: self._do_count_matching(uiapp, criteria))

    def _do_count_matching(self, uiapp, criteria):
        """How many elements match, without touching the selection or the view.

        Worth having separately from Select Matching: it lets you tune the
        criteria without Revit repeatedly zooming and highlighting, and
        without losing whatever you already had selected.
        """
        _, doc = require_document(uiapp)
        view = doc.ActiveView
        if view is None:
            raise FilterProError("There is no active view.")

        count = len(self.find_matches(doc, view, criteria))
        self.on_ui(lambda: self.set_match_count(count, view.Name))

    def set_match_count(self, count, view_name):
        """Update the persistent match readout next to the output buttons."""
        self.lblMatchCount.Text = "{0} matching".format(count)
        self.log("{0} element(s) in '{1}' match the current criteria.".format(
            count, view_name))

    def isolate_matching_click(self, sender, args):
        criteria = self.require_criteria()
        if criteria is not None:
            self.run_in_revit(lambda uiapp: self._do_isolate_matching(uiapp, criteria))

    def _do_isolate_matching(self, uiapp, criteria):
        uidoc, doc = require_document(uiapp)
        view = doc.ActiveView
        if view is None:
            raise FilterProError("There is no active view.")

        matches = self.find_matches(doc, view, criteria)
        if not matches:
            raise FilterProError("No elements in the active view match these criteria.")

        # Temporary Hide/Isolate is a view-state change, so it needs a
        # transaction even though nothing in the model is modified.
        run_transaction(
            doc, "Filter Pro - Isolate Matching",
            lambda: view.IsolateElementsTemporary(List[DB.ElementId](matches)))
        uidoc.RefreshActiveView()

        count = len(matches)
        self.on_ui(lambda: (self.set_match_count(count, view.Name),
                            self.log("{0} element(s) temporarily isolated in "
                                     "'{1}'. Use 'Reset Isolate' to restore "
                                     "the view.".format(count, view.Name))))

    def reset_isolate_click(self, sender, args):
        self.run_in_revit(self._do_reset_isolate)

    def _do_reset_isolate(self, uiapp):
        uidoc, doc = require_document(uiapp)
        view = doc.ActiveView
        if view is None:
            raise FilterProError("There is no active view.")
        run_transaction(
            doc, "Filter Pro - Reset Isolate",
            lambda: view.DisableTemporaryViewMode(
                DB.TemporaryViewMode.TemporaryHideIsolate))
        uidoc.RefreshActiveView()
        self.on_ui(lambda: self.log("Temporary Hide/Isolate reset."))

    # ---------------------------------------------------- create view filter --

    def create_filter_click(self, sender, args):
        criteria = self.require_criteria()
        if criteria is None:
            return
        options = {
            "name": (self.txtFilterName.Text or "").strip(),
            "rgb": self._rgb,
            "line_weight": int(self.cmbLineWeight.SelectedItem or 6),
            "solid_fill": bool(self.chkSolidFill.IsChecked),
            "halftone": bool(self.chkHalftone.IsChecked),
            "apply_to_cut": bool(self.chkApplyCut.IsChecked),
        }
        self.run_in_revit(lambda uiapp: self._do_create_filter(uiapp, criteria, options))

    def _resolve_filter_categories(self, selected_names, warnings):
        """Categories for the ParameterFilterElement.

        A ParameterFilterElement is always scoped to a category set, so when
        the user selected none we fall back to every category present in the
        pick. Categories Revit cannot filter natively are dropped with a
        warning instead of failing the whole operation.
        """
        names = list(selected_names)
        if not names:
            names = list(self.category_ids_map.keys())
            if not names:
                raise FilterProError("No categories available. Pick elements first.")
            warnings.append(
                "No category was selected - used all {0} categor(ies) from the "
                "picked elements.".format(len(names)))

        try:
            filterable = set(eid_value(cid) for cid
                             in DB.ParameterFilterUtilities.GetAllFilterableCategories())
        except Exception:
            filterable = None

        category_ids = []
        excluded = []
        for name in names:
            category_id = self.category_ids_map.get(name)
            if category_id is None:
                continue
            if filterable is None or eid_value(category_id) in filterable:
                category_ids.append(category_id)
            else:
                excluded.append(name)

        if excluded:
            warnings.append("Excluded (Revit View Filters do not support them): "
                            "{0}.".format(", ".join(sorted(excluded))))
        if not category_ids:
            raise FilterProError(
                "None of the selected categories can be used in a native Revit "
                "View Filter. 'Select Matching Elements in View' still works "
                "for them.")
        return category_ids

    def _build_family_filter(self, family_names, filterable_params, warnings):
        """Family rule, handling loadable and system families separately.

        Loadable families are matched on ``ELEM_FAMILY_PARAM`` by the Family
        element's id - this is what Revit's own Filters dialog does. System
        families (walls, floors, pipes...) have no Family element at all, so
        they are matched on the ``Family Name`` string parameter instead.
        v1.0.0 only handled the first case, which is why the Family criterion
        quietly did nothing for the most common categories in a model.
        """
        family_ids = {}
        system_family_names = []
        for name in family_names:
            entry = self.family_ids_map.get(name)
            if not entry:
                continue
            if entry["ids"]:
                family_ids.update(entry["ids"])
            else:
                system_family_names.append(name)

        sub_filters = []
        unsupported = []

        if family_ids:
            param_id = resolve_builtin_param(FAMILY_ID_PARAM_NAMES, filterable_params)
            if param_id is None:
                unsupported.append("loadable families")
            else:
                for value in family_ids.values():
                    try:
                        sub_filters.append(DB.ElementParameterFilter(
                            make_filter_rule(param_id, OP_EQUALS, value,
                                             DB.StorageType.ElementId)))
                    except Exception as ex:
                        logger.debug("Family id rule failed: %s", ex)
                        unsupported.append("loadable families")
                        break

        if system_family_names:
            param_id = resolve_builtin_param(FAMILY_NAME_PARAM_NAMES, filterable_params)
            if param_id is None:
                unsupported.append("system families")
            else:
                for value in system_family_names:
                    try:
                        sub_filters.append(DB.ElementParameterFilter(
                            make_filter_rule(param_id, OP_EQUALS, value,
                                             DB.StorageType.String)))
                    except Exception as ex:
                        logger.debug("Family name rule failed: %s", ex)
                        unsupported.append("system families")
                        break

        if unsupported:
            warnings.append(
                "Family rule skipped for {0} - the selected categories do not "
                "expose a filterable Family parameter.".format(
                    " and ".join(sorted(set(unsupported)))))
        if not sub_filters:
            return None
        return combine_filters(sub_filters, use_or=True)

    def _detect_common_level_param(self, doc, category_ids, filterable_params):
        """The one Level parameter shared by every selected category, or None.

        There is no universal Level BuiltInParameter, so this samples the
        picked elements. If the selected categories disagree (say Walls use
        "Base Level" while Generic Models use "Schedule Level") there is no
        single parameter to filter on and the caller must skip the rule.
        The result is also checked against the filterable set, so a parameter
        that exists but cannot drive a filter is rejected here rather than
        blowing up inside ParameterFilterElement.Create.
        """
        category_id_values = set(eid_value(cid) for cid in category_ids)
        candidates = {}
        for element_id in self.picked_ids:
            element = doc.GetElement(element_id)
            if element is None or element.Category is None:
                continue
            if eid_value(element.Category.Id) not in category_id_values:
                continue
            for param_name in LEVEL_PARAM_NAMES:
                param = element.LookupParameter(param_name)
                if param is not None and param.StorageType == DB.StorageType.ElementId:
                    try:
                        candidates[eid_value(param.Definition.Id)] = param.Definition.Id
                    except Exception:
                        pass
                    break

        if len(candidates) != 1:
            return None
        key = list(candidates.keys())[0]
        if filterable_params is not None and key not in filterable_params:
            return None
        return candidates[key]

    def _build_level_filter(self, doc, level_names, category_ids,
                            filterable_params, warnings):
        param_id = self._detect_common_level_param(doc, category_ids, filterable_params)
        if param_id is None:
            warnings.append(
                "Level rule skipped - the selected categories do not share one "
                "filterable 'Level' parameter. Level still works for 'Select "
                "Matching Elements in View'.")
            return None

        sub_filters = []
        for name in level_names:
            level_id = self.level_ids_map.get(name)
            if level_id is None:
                continue
            try:
                sub_filters.append(DB.ElementParameterFilter(
                    make_filter_rule(param_id, OP_EQUALS, level_id,
                                     DB.StorageType.ElementId)))
            except Exception as ex:
                logger.debug("Level rule failed for '%s': %s", name, ex)
        if not sub_filters:
            warnings.append("Level rule skipped - no usable Level rule could be built.")
            return None
        return combine_filters(sub_filters, use_or=True)

    def _build_parameter_filters(self, rules, filterable_params, warnings):
        """Native filters for the user's parameter rules, skipping the impossible."""
        built = []
        skipped = []
        for rule in rules:
            param_id = rule["param_eid"]
            if param_id is None:
                skipped.append(rule["name"])
                continue
            # Pre-validating against the filterable set converts what would be
            # a total failure inside SetElementFilter into one precise warning.
            if filterable_params is not None and eid_value(param_id) not in filterable_params:
                skipped.append(rule["name"])
                continue
            try:
                element_filter = build_rule_element_filter(rule)
                if element_filter is not None:
                    built.append(element_filter)
                else:
                    skipped.append(rule["name"])
            except Exception as ex:
                logger.debug("Rule '%s' not expressible natively: %s", rule["name"], ex)
                skipped.append(rule["name"])

        if skipped:
            warnings.append(
                "Parameter rule(s) skipped - not usable in a native View Filter "
                "for these categories: {0}.".format(", ".join(sorted(set(skipped)))))
        return built

    def _do_create_filter(self, uiapp, criteria, options):
        uidoc, doc = require_document(uiapp)
        view = require_graphics_view(doc)

        # A view template that controls filters is the only place the filter
        # can actually be applied; adding it to the view itself would throw.
        host_view, template_name = resolve_filter_host_view(doc, view)
        if template_name is not None:
            if not forms.alert(
                    "View '{0}' gets its filters from view template '{1}'.\n\n"
                    "Apply the new filter to that template instead? It will "
                    "affect every view using the template.".format(
                        view.Name, template_name),
                    title="Filter Pro", yes=True, no=True):
                raise FilterProError(
                    "Cancelled - the filter was not created. Remove the view "
                    "template from this view to apply filters to it directly.")

        warnings = []
        category_ids = self._resolve_filter_categories(criteria["categories"], warnings)
        filterable_params = get_filterable_param_ids(doc, category_ids)

        rule_filters = []
        if criteria["families"]:
            family_filter = self._build_family_filter(
                criteria["families"], filterable_params, warnings)
            if family_filter is not None:
                rule_filters.append(family_filter)
        if criteria["levels"]:
            level_filter = self._build_level_filter(
                doc, criteria["levels"], category_ids, filterable_params, warnings)
            if level_filter is not None:
                rule_filters.append(level_filter)

        parameter_filters = self._build_parameter_filters(
            criteria["rules"], filterable_params, warnings)

        # Category/Family/Level always scope the filter (AND), while the
        # parameter rules honour the user's AND/OR choice among themselves -
        # the same shape as Revit's own filter dialog.
        combined_parameters = combine_filters(parameter_filters, use_or=criteria["combine_or"])
        if combined_parameters is not None:
            rule_filters.append(combined_parameters)
        combined_filter = combine_filters(rule_filters, use_or=False)

        base_name = options["name"] or "FilterPro_{0}".format(
            time.strftime("%Y%m%d_%H%M%S"))
        final_name = unique_filter_name(doc, base_name)
        if final_name != base_name:
            warnings.append("Renamed to '{0}' - a filter called '{1}' already "
                            "exists.".format(final_name, base_name))

        overrides = build_override_settings(
            doc, options["rgb"], options["line_weight"], options["solid_fill"],
            options["halftone"], options["apply_to_cut"])

        def _create():
            filter_element = DB.ParameterFilterElement.Create(
                doc, final_name, List[DB.ElementId](category_ids))
            if combined_filter is not None:
                filter_element.SetElementFilter(combined_filter)
            applied = [eid_value(f) for f in host_view.GetFilters()]
            if eid_value(filter_element.Id) not in applied:
                host_view.AddFilter(filter_element.Id)
            host_view.SetFilterOverrides(filter_element.Id, overrides)
            return filter_element.Id

        try:
            run_transaction(doc, "Filter Pro - Create View Filter", _create)
        except Exception as ex:
            raise FilterProError("Could not create the view filter:\n\n{0}".format(ex))

        uidoc.RefreshActiveView()

        target = ("view template '{0}'".format(template_name) if template_name
                  else "view '{0}'".format(host_view.Name))
        message = "View filter '{0}' created and applied to {1}.".format(final_name, target)
        if warnings:
            message += "\n\nNotes:\n- " + "\n- ".join(warnings)
        summary = "Filter '{0}' created ({1} categor(ies), {2} note(s)).".format(
            final_name, len(category_ids), len(warnings))

        def _finish():
            self.log(summary)
            forms.alert(message, title="Filter Pro")
            self.tabMain.SelectedIndex = 1
        self.on_ui(_finish)
        self.run_in_revit(self._do_refresh_filters)

    # -------------------------------------------------- manage view filters --

    def refresh_filters_click(self, sender, args):
        self.run_in_revit(self._do_refresh_filters)

    def _do_refresh_filters(self, uiapp):
        uidoc, doc = require_document(uiapp)
        filters_map = {}
        for filter_element in DB.FilteredElementCollector(doc).OfClass(
                DB.ParameterFilterElement):
            filters_map[filter_element.Name] = filter_element.Id

        def _apply():
            self.filters_map = filters_map
            self.lstExistingFilters.ItemsSource = sorted(filters_map.keys())
            self.log("{0} view filter(s) in this project.".format(len(filters_map)))
        self.on_ui(_apply)

    def _selected_filter_name(self):
        name = self.lstExistingFilters.SelectedItem
        if not name:
            forms.alert("Select a filter from the list first.", title="Filter Pro")
            return None
        return str(name)

    def _filter_id(self, name):
        filter_id = self.filters_map.get(name)
        if filter_id is None:
            raise FilterProError(
                "Filter '{0}' no longer exists. Click 'Refresh List'.".format(name))
        return filter_id

    def select_by_existing_filter_click(self, sender, args):
        name = self._selected_filter_name()
        if name:
            self.run_in_revit(lambda uiapp: self._do_select_by_filter(uiapp, name))

    def _do_select_by_filter(self, uiapp, name):
        uidoc, doc = require_document(uiapp)
        view = doc.ActiveView
        if view is None:
            raise FilterProError("There is no active view.")

        filter_element = doc.GetElement(self._filter_id(name))
        if filter_element is None:
            raise FilterProError("Filter '{0}' no longer exists.".format(name))

        category_ids = filter_element.GetCategories()
        try:
            rule_filter = filter_element.GetElementFilter()
        except Exception:
            rule_filter = None  # a filter with no rules at all

        parts = []
        if category_ids is not None and category_ids.Count > 0:
            parts.append(DB.ElementMulticategoryFilter(category_ids))
        if rule_filter is not None:
            parts.append(rule_filter)
        combined = combine_filters(parts, use_or=False)

        collector = DB.FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType()
        try:
            element_ids = (collector.WherePasses(combined).ToElementIds() if combined
                           else collector.ToElementIds())
        except Exception as ex:
            raise FilterProError(
                "Revit could not evaluate filter '{0}' in this view:\n\n{1}".format(name, ex))

        if element_ids.Count == 0:
            self.on_ui(lambda: self.log(
                "0 elements match filter '{0}' in this view.".format(name)))
            raise FilterProError("No elements in the active view match this filter.")

        uidoc.Selection.SetElementIds(element_ids)
        count = element_ids.Count
        self.on_ui(lambda: self.log(
            "{0} element(s) selected via filter '{1}'.".format(count, name)))

    def toggle_filter_visibility_click(self, sender, args):
        name = self._selected_filter_name()
        if name:
            self.run_in_revit(lambda uiapp: self._do_toggle_visibility(uiapp, name))

    def _do_toggle_visibility(self, uiapp, name):
        uidoc, doc = require_document(uiapp)
        view = require_graphics_view(doc)
        host_view, template_name = resolve_filter_host_view(doc, view)
        filter_id = self._filter_id(name)

        state = {}

        def _toggle():
            applied = [eid_value(f) for f in host_view.GetFilters()]
            if eid_value(filter_id) not in applied:
                host_view.AddFilter(filter_id)
                host_view.SetFilterVisibility(filter_id, True)
                state["visible"] = True
                state["added"] = True
            else:
                current = host_view.GetFilterVisibility(filter_id)
                host_view.SetFilterVisibility(filter_id, not current)
                state["visible"] = not current
                state["added"] = False

        try:
            run_transaction(doc, "Filter Pro - Toggle Filter Visibility", _toggle)
        except Exception as ex:
            raise FilterProError("Could not toggle filter visibility:\n\n{0}".format(ex))
        uidoc.RefreshActiveView()

        target = ("template '{0}'".format(template_name) if template_name
                  else "'{0}'".format(host_view.Name))
        prefix = "added to" if state.get("added") else "visibility in"
        self.on_ui(lambda: self.log("Filter '{0}' {1} {2} -> visible={3}.".format(
            name, prefix, target, state.get("visible"))))

    def rename_filter_click(self, sender, args):
        name = self._selected_filter_name()
        if not name:
            return
        new_name = forms.ask_for_string(
            default=name, prompt="New name for this view filter:", title="Filter Pro")
        if not new_name:
            return
        new_name = new_name.strip()
        if not new_name or new_name == name:
            return
        self.run_in_revit(lambda uiapp: self._do_rename_filter(uiapp, name, new_name))

    def _do_rename_filter(self, uiapp, name, new_name):
        uidoc, doc = require_document(uiapp)
        filter_element = doc.GetElement(self._filter_id(name))
        if filter_element is None:
            raise FilterProError("Filter '{0}' no longer exists.".format(name))

        def _rename():
            filter_element.Name = new_name

        try:
            run_transaction(doc, "Filter Pro - Rename Filter", _rename)
        except Exception as ex:
            raise FilterProError(
                "Could not rename to '{0}' - Revit filter names must be "
                "unique:\n\n{1}".format(new_name, ex))

        self.on_ui(lambda: self.log("Filter '{0}' renamed to '{1}'.".format(name, new_name)))
        self.run_in_revit(self._do_refresh_filters)

    def delete_filter_click(self, sender, args):
        name = self._selected_filter_name()
        if not name:
            return
        # Confirm on the UI thread, before queueing anything destructive.
        if not forms.alert(
                "Delete view filter '{0}' from the whole project?\n\nIt will be "
                "removed from every view that uses it. Revit's Undo can still "
                "reverse this, but Filter Pro cannot.".format(name),
                title="Filter Pro", yes=True, no=True):
            return
        self.run_in_revit(lambda uiapp: self._do_delete_filter(uiapp, name))

    def _do_delete_filter(self, uiapp, name):
        uidoc, doc = require_document(uiapp)
        filter_id = self._filter_id(name)
        try:
            run_transaction(doc, "Filter Pro - Delete Filter",
                            lambda: doc.Delete(filter_id))
        except Exception as ex:
            raise FilterProError("Could not delete filter '{0}':\n\n{1}".format(name, ex))
        self.on_ui(lambda: self.log("Filter '{0}' deleted.".format(name)))
        self.run_in_revit(self._do_refresh_filters)


# =============================================================================
# ENTRY POINT
# =============================================================================

def get_live_window():
    """Return the already-open Filter Pro window, or None.

    pyRevit re-executes this script on every button click, so module globals
    are not a reliable singleton store. The AppDomain outlives those reloads,
    which makes it the right place to park the live window. Any failure here
    simply falls through to opening a fresh window.
    """
    try:
        window = System.AppDomain.CurrentDomain.GetData(APPDOMAIN_WINDOW_KEY)
        if window is not None and window.IsLoaded:
            return window
    except Exception as ex:
        logger.debug("No reusable Filter Pro window: %s", ex)
    return None


def main():
    existing = get_live_window()
    if existing is not None:
        # Re-clicking the ribbon button should surface the panel you already
        # have, not stack a second one on top of it.
        existing.Show()
        existing.Activate()
        return

    window = FilterProWindow("ui.xaml")
    try:
        System.AppDomain.CurrentDomain.SetData(APPDOMAIN_WINDOW_KEY, window)
    except Exception as ex:
        logger.debug("Could not register the Filter Pro window instance: %s", ex)
    window.show(modal=False)


if __name__ == "__main__":
    main()
