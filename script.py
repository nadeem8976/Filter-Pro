# -*- coding: utf-8 -*-
"""Filter Pro

Pick elements in the model, then build filter criteria from their Category,
Family, Level or any parameter (this naturally includes every field you
could add to a schedule, since schedule fields ARE element/type parameters).

Two outputs:
  1. "Select Matching Elements in View"  - evaluates the criteria in Python
     against every element in the active view and selects the matches.
     This works for ANY category/parameter, with no Revit-side limitations.
  2. "Create View Filter"                - creates a native Revit
     ParameterFilterElement (a real View Filter with color overrides) that
     shows up in Visibility/Graphics Overrides > Filters, editable later
     like any other Revit filter. Because Revit's native filter engine has
     its own constraints (see README "Known limitations"), a couple of edge
     cases (Level across mixed categories, non-filterable categories) fall
     back with a clear warning instead of silently producing a wrong filter.

Version: 1.0.0  (see CHANGELOG.md)
"""

import time

from pyrevit import revit, DB, forms
from pyrevit.framework import List

from Autodesk.Revit.UI.Selection import ObjectType

__title__ = "Filter Pro"
__author__ = "Filter Pro Add-in"
__doc__ = "Build selection and view filters from Category, Family, Level and any parameter."


# =============================================================================
# CONSTANTS
# =============================================================================

# Common instance-parameter names Revit uses for "Level" across different
# categories (there is no single BuiltInParameter that covers every
# category, which is exactly why the View Filter Level rule is best-effort -
# see _detect_common_level_param below and the README).
LEVEL_PARAM_NAMES = ("Level", "Reference Level", "Base Level", "Schedule Level")

# Tolerance used when comparing Double (real number) parameter values for
# native Revit View Filter rules. Revit's ParameterFilterRuleFactory requires
# an explicit epsilon for equality on doubles because they are stored in
# internal (feet-based) units.
DOUBLE_EPSILON = 1e-6

# Fixed, readable override-color palette. Kept small and named on purpose -
# a full color picker adds UI complexity without adding real value here.
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


# =============================================================================
# HELPERS (module-level, no UI dependency - easy to unit-test / reuse)
# =============================================================================

def eid_value(eid):
    """Return a plain int for an ElementId.

    Revit 2024+ deprecated ElementId.IntegerValue in favor of ElementId.Value
    (Int64), to support 64-bit ids. Since this tool targets Revit 2025-2026,
    we prefer .Value but fall back to .IntegerValue so the same script also
    tolerates being run on an older host if someone copies it back.
    """
    if eid is None:
        return None
    try:
        return int(eid.Value)
    except AttributeError:
        return int(eid.IntegerValue)


def get_family_info(el, doc):
    """Return (family_name, family_element_id) for el, or (None, None)."""
    try:
        et = doc.GetElement(el.GetTypeId())
    except Exception:
        et = None
    if et is not None:
        try:
            fam = et.Family
            if fam:
                return fam.Name, fam.Id
        except Exception:
            pass
        try:
            if getattr(et, "FamilyName", None):
                return et.FamilyName, None
        except Exception:
            pass
    return None, None


def get_level_and_id(el, doc):
    """Return (level_name, level_element_id) for el, or (None, None)."""
    try:
        lvl_id = getattr(el, "LevelId", None)
    except Exception:
        lvl_id = None
    if lvl_id and lvl_id != DB.ElementId.InvalidElementId:
        lvl = doc.GetElement(lvl_id)
        if lvl:
            return lvl.Name, lvl_id
    for pname in LEVEL_PARAM_NAMES:
        p = el.LookupParameter(pname)
        if p and p.HasValue and p.StorageType == DB.StorageType.ElementId:
            eid = p.AsElementId()
            if eid and eid != DB.ElementId.InvalidElementId:
                lvl = doc.GetElement(eid)
                if lvl:
                    return lvl.Name, eid
    return None, None


def get_raw_display(p, doc):
    """Return (raw_value, display_string) for a Parameter, or (None, None)."""
    st = p.StorageType
    if st == DB.StorageType.String:
        raw = p.AsString()
        disp = raw if raw else p.AsValueString()
        return raw, disp
    elif st == DB.StorageType.Integer:
        raw = p.AsInteger()
        disp = p.AsValueString()
        if not disp:
            disp = str(raw)
        return raw, disp
    elif st == DB.StorageType.Double:
        raw = p.AsDouble()
        disp = p.AsValueString()
        if not disp:
            disp = str(round(raw, 4))
        return raw, disp
    elif st == DB.StorageType.ElementId:
        raw = p.AsElementId()
        disp = p.AsValueString()
        if not disp and raw and raw != DB.ElementId.InvalidElementId:
            el2 = doc.GetElement(raw)
            disp = el2.Name if (el2 and hasattr(el2, "Name")) else None
        return raw, disp
    return None, None


def collect_element_parameters(el, doc, param_data):
    """Merge el's instance + type parameters into param_data (in place).

    param_data: { param_name: {storage_type, is_type, param_eid, values: {display: raw}} }

    Reading both instance AND type parameters mirrors exactly what Revit
    itself offers as available "fields" when you build a schedule for these
    categories, which is why this single collection doubles as the
    "schedule field parameters" source the UI exposes.
    """
    sources = [(el, False)]
    try:
        et = doc.GetElement(el.GetTypeId())
    except Exception:
        et = None
    if et is not None:
        sources.append((et, True))

    for src, is_type in sources:
        try:
            params = src.GetOrderedParameters()
        except Exception:
            continue
        for p in params:
            try:
                if not p.HasValue:
                    continue
                name = p.Definition.Name
                raw, disp = get_raw_display(p, doc)
                if not disp:
                    continue
                entry = param_data.get(name)
                if entry is None:
                    param_eid = None
                    try:
                        param_eid = p.Definition.Id
                    except Exception:
                        param_eid = None
                    entry = {
                        "storage_type": p.StorageType,
                        "is_type": is_type,
                        "param_eid": param_eid,
                        "values": {},
                    }
                    param_data[name] = entry
                entry["values"][disp] = raw
            except Exception:
                continue


def get_element_param_display(el, doc, name):
    """Look up param `name` on el (instance, falling back to type) and
    return its display string, or None if not present/no value."""
    p = el.LookupParameter(name)
    if p is None or not p.HasValue:
        try:
            et = doc.GetElement(el.GetTypeId())
        except Exception:
            et = None
        p = et.LookupParameter(name) if et else None
    if p is None or not p.HasValue:
        return None
    _, disp = get_raw_display(p, doc)
    return disp


def element_matches(el, doc, cat_names, fam_names, lvl_names, rules):
    """Python-side evaluation used by 'Select Matching Elements in View'.

    Deliberately independent of Revit's native filter engine so it works
    uniformly for every category/parameter combination, with none of the
    native-filter constraints documented in the README.
    """
    if cat_names:
        cat = el.Category
        if not cat or cat.Name not in cat_names:
            return False
    if fam_names:
        fam_name, _ = get_family_info(el, doc)
        if not fam_name or fam_name not in fam_names:
            return False
    if lvl_names:
        lvl_name, _ = get_level_and_id(el, doc)
        if not lvl_name or lvl_name not in lvl_names:
            return False
    for rule in rules:
        disp = get_element_param_display(el, doc, rule["name"])
        if disp is None or disp not in rule["values"]:
            return False
    return True


# =============================================================================
# MAIN WINDOW
# =============================================================================

class FilterProWindow(forms.WPFWindow):

    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)
        self.doc = revit.doc
        self.uidoc = revit.uidoc

        # picked-elements-derived state
        self.picked_ids = []
        self.category_ids_map = {}      # name -> ElementId
        self.family_ids_map = {}        # name -> {int_id: ElementId}
        self.level_ids_map = {}         # name -> ElementId
        self.param_data = {}            # name -> {...} (see collect_element_parameters)
        self.active_rules = []          # list of rule dicts

        # existing-filters state (Manage tab)
        self.filters_map = {}           # name -> ElementId

        self.cmbColor.ItemsSource = COLOR_NAMES
        self.cmbColor.SelectedIndex = 0

        self.refresh_filters_click(None, None)
        self.log("Ready. Click 'Pick Elements' to begin.")

    # ---------------------------------------------------------- utilities --

    def log(self, msg):
        self.txtLog.Text = "[{0}] {1}".format(time.strftime("%H:%M:%S"), msg)

    def refresh_active_rules_list(self):
        self.lstActiveRules.ItemsSource = [
            "{0}  =  {1}".format(r["name"], ", ".join(sorted(r["values"])))
            for r in self.active_rules
        ]

    # ------------------------------------------------------- pick elements --

    def pick_elements_click(self, sender, args):
        self.Hide()
        ids = []
        try:
            try:
                refs = self.uidoc.Selection.PickObjects(
                    ObjectType.Element,
                    "Pick elements, then click Finish (top of screen) - Esc to cancel",
                )
                ids = [r.ElementId for r in refs]
            except Exception:
                ids = []  # user pressed Esc / cancelled the pick
        finally:
            self.Show()

        if not ids:
            self.log("Pick cancelled - no elements were selected.")
            return

        self.picked_ids = ids
        self.populate_from_pick()

    def clear_pick_click(self, sender, args):
        self.picked_ids = []
        self.category_ids_map = {}
        self.family_ids_map = {}
        self.level_ids_map = {}
        self.param_data = {}
        self.active_rules = []

        self.lstCategory.ItemsSource = None
        self.lstFamily.ItemsSource = None
        self.lstLevel.ItemsSource = None
        self.cmbParam.ItemsSource = None
        self.lstParamValues.ItemsSource = None
        self.lstActiveRules.ItemsSource = None

        self.lblPickedCount.Text = "No elements picked yet."
        self.log("Selection cleared.")

    def populate_from_pick(self):
        doc = self.doc
        cat_map = {}
        fam_map = {}
        lvl_map = {}
        param_data = {}

        for eid in self.picked_ids:
            el = doc.GetElement(eid)
            if not el:
                continue

            try:
                if el.Category and el.Category.Name:
                    cat_map[el.Category.Name] = el.Category.Id
            except Exception:
                pass

            fam_name, fam_id = get_family_info(el, doc)
            if fam_name:
                d = fam_map.setdefault(fam_name, {})
                if fam_id is not None:
                    d[eid_value(fam_id)] = fam_id

            lvl_name, lvl_id = get_level_and_id(el, doc)
            if lvl_name:
                lvl_map[lvl_name] = lvl_id

            collect_element_parameters(el, doc, param_data)

        self.category_ids_map = cat_map
        self.family_ids_map = fam_map
        self.level_ids_map = lvl_map
        self.param_data = param_data
        self.active_rules = []

        self.lstCategory.ItemsSource = sorted(cat_map.keys())
        self.lstFamily.ItemsSource = sorted(fam_map.keys())
        self.lstLevel.ItemsSource = sorted(lvl_map.keys())
        self.cmbParam.ItemsSource = sorted(param_data.keys())
        if self.cmbParam.Items.Count > 0:
            self.cmbParam.SelectedIndex = 0
        else:
            self.lstParamValues.ItemsSource = None
        self.lstActiveRules.ItemsSource = None

        self.lblPickedCount.Text = "{0} element(s) picked.".format(len(self.picked_ids))
        self.log(
            "Picked {0} elements -> {1} categories, {2} families, {3} levels, {4} parameters available.".format(
                len(self.picked_ids), len(cat_map), len(fam_map), len(lvl_map), len(param_data)
            )
        )

    # ------------------------------------------------------ parameter rules --

    def param_selected_changed(self, sender, args):
        name = self.cmbParam.SelectedItem
        if name and name in self.param_data:
            self.lstParamValues.ItemsSource = sorted(self.param_data[name]["values"].keys())
        else:
            self.lstParamValues.ItemsSource = None

    def add_rule_click(self, sender, args):
        name = self.cmbParam.SelectedItem
        values = list(self.lstParamValues.SelectedItems)
        if not name or not values:
            forms.alert("Select a parameter and at least one value first.")
            return

        entry = self.param_data[name]
        raw_map = dict((v, entry["values"].get(v)) for v in values)
        new_rule = {
            "name": name,
            "values": set(values),
            "raw_map": raw_map,
            "storage_type": entry["storage_type"],
            "param_eid": entry["param_eid"],
            "is_type": entry["is_type"],
        }

        replaced = False
        for i, r in enumerate(self.active_rules):
            if r["name"] == name:
                self.active_rules[i] = new_rule
                replaced = True
                break
        if not replaced:
            self.active_rules.append(new_rule)

        self.refresh_active_rules_list()
        self.log("Rule {0}: '{1}' -> {2} value(s).".format(
            "updated" if replaced else "added", name, len(values)))

    def remove_rule_click(self, sender, args):
        idx = self.lstActiveRules.SelectedIndex
        if idx is None or idx < 0:
            forms.alert("Select a rule to remove first.")
            return
        removed = self.active_rules.pop(idx)
        self.refresh_active_rules_list()
        self.log("Rule removed: {0}".format(removed["name"]))

    # --------------------------------------------------- select matching ---

    def select_matching_click(self, sender, args):
        if not self.picked_ids:
            forms.alert("Pick elements first (step 1) so Filter Pro knows which fields to offer.")
            return

        doc = self.doc
        active_view = doc.ActiveView
        cat_names = set(self.lstCategory.SelectedItems)
        fam_names = set(self.lstFamily.SelectedItems)
        lvl_names = set(self.lstLevel.SelectedItems)
        rules = self.active_rules

        if not (cat_names or fam_names or lvl_names or rules):
            forms.alert("Select at least one Category / Family / Level value, or add a parameter rule.")
            return

        collector = DB.FilteredElementCollector(doc, active_view.Id).WhereElementIsNotElementType()
        matches = []
        for el in collector:
            try:
                if element_matches(el, doc, cat_names, fam_names, lvl_names, rules):
                    matches.append(el.Id)
            except Exception:
                continue

        if not matches:
            self.log("0 elements matched in view '{0}'.".format(active_view.Name))
            forms.alert("No elements in the active view match these criteria.")
            return

        self.uidoc.Selection.SetElementIds(List[DB.ElementId](matches))
        self.log("{0} elements matched and selected in view '{1}'.".format(len(matches), active_view.Name))

    # ---------------------------------------------------- create view filter --

    def _detect_common_level_param(self, cat_ids):
        """Return a single ElementId shared by the Level parameter of every
        selected category, sampled from the picked elements - or None if the
        categories don't agree on one Level parameter (see README)."""
        doc = self.doc
        cat_id_ints = set(eid_value(c) for c in cat_ids)
        found = set()
        for eid in self.picked_ids:
            el = doc.GetElement(eid)
            if not el or not el.Category or eid_value(el.Category.Id) not in cat_id_ints:
                continue
            for pname in LEVEL_PARAM_NAMES:
                p = el.LookupParameter(pname)
                if p and p.HasValue and p.StorageType == DB.StorageType.ElementId:
                    try:
                        found.add(eid_value(p.Definition.Id))
                    except Exception:
                        pass
                    break
        if len(found) == 1:
            return DB.ElementId(list(found)[0])
        return None

    def _build_param_rule_filters(self, rule):
        param_id = rule["param_eid"]
        storage = rule["storage_type"]
        raws = [v for v in rule["raw_map"].values() if v is not None]
        if not raws:
            return None
        sub = []
        for raw in raws:
            if storage == DB.StorageType.String:
                fr = DB.ParameterFilterRuleFactory.CreateEqualsRule(param_id, raw)
            elif storage == DB.StorageType.Integer:
                fr = DB.ParameterFilterRuleFactory.CreateEqualsRule(param_id, int(raw))
            elif storage == DB.StorageType.Double:
                fr = DB.ParameterFilterRuleFactory.CreateEqualsRule(param_id, float(raw), DOUBLE_EPSILON)
            elif storage == DB.StorageType.ElementId:
                fr = DB.ParameterFilterRuleFactory.CreateEqualsRule(param_id, raw)
            else:
                continue
            sub.append(DB.ElementParameterFilter(fr))
        if not sub:
            return None
        return sub[0] if len(sub) == 1 else DB.LogicalOrFilter(List[DB.ElementFilter](sub))

    def _unique_filter_name(self, doc, base_name):
        existing = set(f.Name for f in DB.FilteredElementCollector(doc).OfClass(DB.ParameterFilterElement))
        if base_name not in existing:
            return base_name
        i = 1
        while "{0}_{1}".format(base_name, i) in existing:
            i += 1
        return "{0}_{1}".format(base_name, i)

    def _get_solid_fill_pattern_id(self, doc):
        for fp in DB.FilteredElementCollector(doc).OfClass(DB.FillPatternElement):
            try:
                if fp.GetFillPattern().IsSolidFill:
                    return fp.Id
            except Exception:
                continue
        return None

    def _build_override(self, doc, rgb):
        color = DB.Color(rgb[0], rgb[1], rgb[2])
        ogs = DB.OverrideGraphicSettings()
        ogs.SetProjectionLineColor(color)
        ogs.SetCutLineColor(color)
        ogs.SetProjectionLineWeight(6)
        solid_id = self._get_solid_fill_pattern_id(doc)
        if solid_id is not None:
            ogs.SetSurfaceForegroundPatternColor(color)
            ogs.SetSurfaceForegroundPatternId(solid_id)
            ogs.SetSurfaceForegroundPatternVisible(True)
        return ogs

    def create_filter_click(self, sender, args):
        if not self.picked_ids:
            forms.alert("Pick elements first (step 1) so Filter Pro knows which fields to offer.")
            return

        doc = self.doc
        active_view = doc.ActiveView

        if not active_view.AreGraphicsOverridesAllowed():
            forms.alert(
                "The active view ('{0}') does not support view filters / graphic overrides. "
                "Switch to a plan, section, elevation or 3D view and try again.".format(active_view.Name)
            )
            return

        # --- categories (required by Revit - a ParameterFilterElement is
        # always scoped to a set of categories) ---
        cat_names = list(self.lstCategory.SelectedItems)
        auto_categories = False
        if not cat_names:
            cat_names = list(self.category_ids_map.keys())
            auto_categories = True
            if not cat_names:
                forms.alert("No categories available. Pick at least one element first.")
                return

        filterable_ids = set(eid_value(eid) for eid in DB.ParameterFilterUtilities.GetAllFilterableCategories())
        final_cat_ids = []
        excluded_cats = []
        for name in cat_names:
            cid = self.category_ids_map.get(name)
            if cid is None:
                continue
            if eid_value(cid) in filterable_ids:
                final_cat_ids.append(cid)
            else:
                excluded_cats.append(name)

        if not final_cat_ids:
            forms.alert("None of the selected categories can be used in a native Revit View Filter.")
            return

        warnings = []
        if auto_categories:
            warnings.append("No category selected - used all {0} categories from the picked elements.".format(len(cat_names)))
        if excluded_cats:
            warnings.append("Excluded (not supported by Revit View Filters): {0}".format(", ".join(excluded_cats)))

        rule_filters = []

        # --- Family rule ---
        fam_names = set(self.lstFamily.SelectedItems)
        if fam_names:
            family_ids = {}
            for n in fam_names:
                family_ids.update(self.family_ids_map.get(n, {}))
            if family_ids:
                fam_param_id = DB.ElementId(DB.BuiltInParameter.ELEM_FAMILY_PARAM)
                sub = [
                    DB.ElementParameterFilter(DB.ParameterFilterRuleFactory.CreateEqualsRule(fam_param_id, fid))
                    for fid in family_ids.values()
                ]
                rule_filters.append(sub[0] if len(sub) == 1 else DB.LogicalOrFilter(List[DB.ElementFilter](sub)))
            else:
                warnings.append("Family rule skipped - could not resolve a Family element for the View Filter.")

        # --- Level rule (best effort, see README "Known limitations") ---
        lvl_names = set(self.lstLevel.SelectedItems)
        if lvl_names:
            level_param_id = self._detect_common_level_param(final_cat_ids)
            if level_param_id is not None:
                level_ids = [self.level_ids_map[n] for n in lvl_names if n in self.level_ids_map]
                sub = [
                    DB.ElementParameterFilter(DB.ParameterFilterRuleFactory.CreateEqualsRule(level_param_id, lid))
                    for lid in level_ids
                ]
                if sub:
                    rule_filters.append(sub[0] if len(sub) == 1 else DB.LogicalOrFilter(List[DB.ElementFilter](sub)))
            else:
                warnings.append(
                    "Level rule skipped in the View Filter - the selected categories don't share one common "
                    "'Level' parameter at the Revit API level. Level still works for "
                    "'Select Matching Elements in View'."
                )

        # --- Generic parameter rules (this is where schedule-field params land) ---
        skipped_params = []
        for rule in self.active_rules:
            if rule["param_eid"] is None:
                skipped_params.append(rule["name"])
                continue
            try:
                sub = self._build_param_rule_filters(rule)
                if sub is not None:
                    rule_filters.append(sub)
            except Exception:
                skipped_params.append(rule["name"])
        if skipped_params:
            warnings.append("Parameter rule(s) skipped (unsupported by native View Filters): {0}".format(
                ", ".join(skipped_params)))

        combined_filter = None
        if len(rule_filters) == 1:
            combined_filter = rule_filters[0]
        elif len(rule_filters) > 1:
            combined_filter = DB.LogicalAndFilter(List[DB.ElementFilter](rule_filters))

        base_name = (self.txtFilterName.Text or "").strip()
        if not base_name:
            base_name = "FilterPro_{0}".format(time.strftime("%Y%m%d_%H%M%S"))
        final_name = self._unique_filter_name(doc, base_name)
        if final_name != base_name:
            warnings.append("Renamed to '{0}' - a filter called '{1}' already exists.".format(final_name, base_name))

        color_name = self.cmbColor.SelectedItem
        rgb = COLOR_PALETTE.get(color_name, COLOR_PALETTE["Red"])

        try:
            with revit.Transaction("Filter Pro - Create View Filter"):
                pfe = DB.ParameterFilterElement.Create(doc, final_name, List[DB.ElementId](final_cat_ids))
                if combined_filter is not None:
                    pfe.SetElementFilter(combined_filter)
                applied_ids = [eid_value(f) for f in active_view.GetFilters()]
                if eid_value(pfe.Id) not in applied_ids:
                    active_view.AddFilter(pfe.Id)
                ogs = self._build_override(doc, rgb)
                active_view.SetFilterOverrides(pfe.Id, ogs)
        except Exception as ex:
            forms.alert("Could not create the view filter:\n{0}".format(str(ex)))
            return

        self.uidoc.RefreshActiveView()
        msg = "View filter '{0}' created and applied to '{1}'.".format(final_name, active_view.Name)
        if warnings:
            msg += "\n\nNotes:\n- " + "\n- ".join(warnings)
        self.log("Filter '{0}' created ({1} categories).".format(final_name, len(final_cat_ids)))
        forms.alert(msg)

        self.tabMain.SelectedIndex = 1
        self.refresh_filters_click(None, None)

    # -------------------------------------------------- manage view filters --

    def refresh_filters_click(self, sender, args):
        doc = self.doc
        self.filters_map = {}
        for pfe in DB.FilteredElementCollector(doc).OfClass(DB.ParameterFilterElement):
            self.filters_map[pfe.Name] = pfe.Id
        self.lstExistingFilters.ItemsSource = sorted(self.filters_map.keys())
        self.log("{0} view filter(s) found in the project.".format(len(self.filters_map)))

    def select_by_existing_filter_click(self, sender, args):
        name = self.lstExistingFilters.SelectedItem
        if not name:
            forms.alert("Select a filter from the list first.")
            return

        doc = self.doc
        fid = self.filters_map[name]
        pfe = doc.GetElement(fid)
        active_view = doc.ActiveView

        cat_ids = pfe.GetCategories()
        rule_filter = pfe.GetElementFilter()
        cat_filter = DB.ElementMulticategoryFilter(cat_ids) if cat_ids and cat_ids.Count > 0 else None

        if cat_filter and rule_filter:
            combined = DB.LogicalAndFilter(cat_filter, rule_filter)
        elif cat_filter:
            combined = cat_filter
        elif rule_filter:
            combined = rule_filter
        else:
            combined = None

        collector = DB.FilteredElementCollector(doc, active_view.Id).WhereElementIsNotElementType()
        ids = collector.WherePasses(combined).ToElementIds() if combined else collector.ToElementIds()

        if ids.Count == 0:
            self.log("0 elements match filter '{0}' in this view.".format(name))
            forms.alert("No elements in the active view match this filter.")
            return

        self.uidoc.Selection.SetElementIds(ids)
        self.log("{0} elements selected (matching filter '{1}').".format(ids.Count, name))

    def toggle_filter_visibility_click(self, sender, args):
        name = self.lstExistingFilters.SelectedItem
        if not name:
            forms.alert("Select a filter from the list first.")
            return

        doc = self.doc
        fid = self.filters_map[name]
        active_view = doc.ActiveView

        try:
            with revit.Transaction("Filter Pro - Toggle Filter Visibility"):
                applied_ids = [eid_value(f) for f in active_view.GetFilters()]
                if eid_value(fid) not in applied_ids:
                    active_view.AddFilter(fid)
                    active_view.SetFilterVisibility(fid, True)
                    new_state = True
                else:
                    current = active_view.GetFilterVisibility(fid)
                    active_view.SetFilterVisibility(fid, not current)
                    new_state = not current
        except Exception as ex:
            forms.alert("Could not toggle filter visibility:\n{0}".format(str(ex)))
            return

        self.log("Filter '{0}' visibility set to {1} in '{2}'.".format(name, new_state, active_view.Name))

    def delete_filter_click(self, sender, args):
        name = self.lstExistingFilters.SelectedItem
        if not name:
            forms.alert("Select a filter from the list first.")
            return

        if not forms.alert(
            "Delete view filter '{0}' from the whole project? This cannot be undone from this tool.".format(name),
            yes=True, no=True,
        ):
            return

        doc = self.doc
        fid = self.filters_map[name]
        try:
            with revit.Transaction("Filter Pro - Delete Filter"):
                doc.Delete(fid)
        except Exception as ex:
            forms.alert("Could not delete filter:\n{0}".format(str(ex)))
            return

        self.log("Filter '{0}' deleted.".format(name))
        self.refresh_filters_click(None, None)


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    FilterProWindow("ui.xaml").show(modal=False)
