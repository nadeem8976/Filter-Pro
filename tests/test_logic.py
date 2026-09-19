"""Exercise Filter Pro's pure logic against stubbed Revit types."""
import io, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import revit_stubs
revit_stubs.install()

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.join(os.path.dirname(HERE),
                      "Filter.extension", "Filter Pro.tab", "Pro.panel",
                      "Filter Pro.pushbutton")
SCRIPT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BUNDLE, "script.py")
fp = types.ModuleType("filterpro")
fp.__file__ = SCRIPT
fp.__name__ = "filterpro"          # not __main__, so main() does not run
exec(compile(io.open(SCRIPT, encoding="utf-8").read(), SCRIPT, "exec"), fp.__dict__)

DB = revit_stubs.DB
ST = DB.StorageType
FAILS = []


def check(label, got, want):
    if got != want:
        FAILS.append("%s: got %r, want %r" % (label, got, want))


def rule(name, op, values=(), raws=(), text=None, storage=ST.String, pid=None):
    return {"name": name, "op": op, "values": list(values), "raws": list(raws),
            "text": text, "storage_type": storage,
            "param_eid": pid if pid is not None else DB.ElementId(7),
            "is_type": False}


# ---------------------------------------------------------------- hex colours
check("hex #RRGGBB", fp.parse_hex_color("#FF8000"), (255, 128, 0))
check("hex bare", fp.parse_hex_color("00b050"), (0, 176, 80))
check("hex padded", fp.parse_hex_color("  #000000 "), (0, 0, 0))
for bad in ("", None, "#FFF", "#GGGGGG", "12345678"):
    check("hex reject %r" % (bad,), fp.parse_hex_color(bad), None)

# ------------------------------------------------------------ rule_matches
r_eq = rule("Comments", fp.OP_EQUALS, ["A", "B"])
check("equals hit", fp.rule_matches(r_eq, "A", "A"), True)
check("equals miss", fp.rule_matches(r_eq, "C", "C"), False)
check("equals empty", fp.rule_matches(r_eq, None, None), False)

r_ne = rule("Comments", fp.OP_NOT_EQUALS, ["A", "B"])
check("not-equals on listed", fp.rule_matches(r_ne, "A", "A"), False)
check("not-equals on other", fp.rule_matches(r_ne, "C", "C"), True)

r_ct = rule("Mark", fp.OP_CONTAINS, text="wa")
check("contains hit", fp.rule_matches(r_ct, "Wall-01", "Wall-01"), True)
check("contains case-insensitive", fp.rule_matches(r_ct, "WALL", "WALL"), True)
check("contains miss", fp.rule_matches(r_ct, "Door", "Door"), False)
check("not-contains", fp.rule_matches(rule("Mark", fp.OP_NOT_CONTAINS, text="wa"),
                                      "Door", "Door"), True)
check("begins with", fp.rule_matches(rule("Mark", fp.OP_BEGINS, text="wa"),
                                     "Wall-01", "Wall-01"), True)
check("begins with miss", fp.rule_matches(rule("Mark", fp.OP_BEGINS, text="01"),
                                          "Wall-01", "Wall-01"), False)
check("ends with", fp.rule_matches(rule("Mark", fp.OP_ENDS, text="01"),
                                   "Wall-01", "Wall-01"), True)

num = lambda op: rule("Width", op, ["200"], [200.0], storage=ST.Double)
check("greater true", fp.rule_matches(num(fp.OP_GREATER), 300.0, "300"), True)
check("greater false at equal", fp.rule_matches(num(fp.OP_GREATER), 200.0, "200"), False)
check("greater-eq at equal", fp.rule_matches(num(fp.OP_GREATER_EQ), 200.0, "200"), True)
check("less true", fp.rule_matches(num(fp.OP_LESS), 100.0, "100"), True)
check("less false at equal", fp.rule_matches(num(fp.OP_LESS), 200.0, "200"), False)
check("less-eq at equal", fp.rule_matches(num(fp.OP_LESS_EQ), 200.0, "200"), True)
check("numeric on empty", fp.rule_matches(num(fp.OP_GREATER), None, None), False)
check("numeric on text value", fp.rule_matches(num(fp.OP_GREATER), "abc", "abc"), False)

check("has value", fp.rule_matches(rule("X", fp.OP_HAS_VALUE), "v", "v"), True)
check("has value on empty", fp.rule_matches(rule("X", fp.OP_HAS_VALUE), None, None), False)
check("no value on empty", fp.rule_matches(rule("X", fp.OP_NO_VALUE), None, None), True)
check("no value on filled", fp.rule_matches(rule("X", fp.OP_NO_VALUE), "v", "v"), False)

# ------------------------------------------------------------- describe_rule
check("describe equals", fp.describe_rule(r_eq), "Comments  equals  A, B")
check("describe text", fp.describe_rule(r_ct), 'Mark  contains  "wa"')
check("describe no-value", fp.describe_rule(rule("X", fp.OP_NO_VALUE)), "X  has no value")


# ----------------------------------------------------------- element_matches
class P(object):
    def __init__(self, value, storage=ST.String):
        self._v, self.StorageType = value, storage
        self.HasValue = value is not None

    def AsString(self):
        return self._v

    def AsValueString(self):
        return None if self._v is None else str(self._v)

    def AsInteger(self):
        return self._v

    def AsDouble(self):
        return self._v

    def AsElementId(self):
        return self._v


class Cat(object):
    def __init__(self, name):
        self.Name, self.Id = name, DB.ElementId(abs(hash(name)) % 999)


class El(object):
    def __init__(self, cat, params=None, level=None, type_id=None):
        self.Category, self._params = Cat(cat), params or {}
        self.LevelId, self._type_id = level, type_id
        self.Id = DB.ElementId(1)

    def LookupParameter(self, n):
        return self._params.get(n)

    def GetTypeId(self):
        return self._type_id


class Lvl(object):
    def __init__(self, name):
        self.Name = name


class Doc(object):
    def __init__(self, m=None):
        self._m = m or {}

    def GetElement(self, eid):
        return self._m.get(eid)


lvl_id = DB.ElementId(55)
doc = Doc({lvl_id: Lvl("Level 1")})
wall = El("Walls", {"Comments": P("A"), "Mark": P("W-01")}, level=lvl_id)

check("cat gate pass", fp.element_matches(wall, doc, {"Walls"}, set(), set(), [], False), True)
check("cat gate block", fp.element_matches(wall, doc, {"Doors"}, set(), set(), [], False), False)
check("level gate pass",
      fp.element_matches(wall, doc, set(), set(), {"Level 1"}, [], False), True)
check("level gate block",
      fp.element_matches(wall, doc, set(), set(), {"Level 2"}, [], False), False)
check("no criteria matches all",
      fp.element_matches(wall, doc, set(), set(), set(), [], False), True)

hit = rule("Comments", fp.OP_EQUALS, ["A"])
miss = rule("Comments", fp.OP_EQUALS, ["ZZ"])
check("AND both hit", fp.element_matches(wall, doc, set(), set(), set(), [hit, hit], False), True)
check("AND one miss", fp.element_matches(wall, doc, set(), set(), set(), [hit, miss], False), False)
check("OR one hit", fp.element_matches(wall, doc, set(), set(), set(), [hit, miss], True), True)
check("OR none hit", fp.element_matches(wall, doc, set(), set(), set(), [miss, miss], True), False)
check("category AND'd with rules even in OR mode",
      fp.element_matches(wall, doc, {"Doors"}, set(), set(), [hit], True), False)

# --------------------------------------------------------- native rule shapes
one = fp.combine_filters([1], use_or=True)
check("combine single passes through", one, 1)
check("combine empty", fp.combine_filters([], use_or=True), None)
check("combine OR type",
      type(fp.combine_filters([1, 2], use_or=True)).__name__, "LogicalOrFilter")
check("combine AND type",
      type(fp.combine_filters([1, 2], use_or=False)).__name__, "LogicalAndFilter")

pos = fp.build_rule_element_filter(
    rule("C", fp.OP_EQUALS, ["A", "B"], ["A", "B"], storage=ST.String))
check("positive multi-value is OR'd", type(pos).__name__, "LogicalOrFilter")
neg = fp.build_rule_element_filter(
    rule("C", fp.OP_NOT_EQUALS, ["A", "B"], ["A", "B"], storage=ST.String))
check("negative multi-value is AND'd", type(neg).__name__, "LogicalAndFilter")

novalue = fp.build_rule_element_filter(rule("C", fp.OP_NO_VALUE))
check("no-value rule builds", novalue.rule.method, "CreateHasNoValueParameterRule")
check("no-value rule takes param only", len(novalue.rule.args), 1)

txt = fp.build_rule_element_filter(rule("C", fp.OP_CONTAINS, text="abc", storage=ST.Double))
check("text op forces string storage", txt.rule.args[1], "abc")

# modern two-arg signature is preferred when the host offers it
pid = DB.ElementId(9)
r = fp.make_filter_rule(pid, fp.OP_EQUALS, 1.5, ST.Double)
check("double uses modern 2-arg first", len(r.args), 2)

# ...and the legacy epsilon/caseSensitive overloads are used when they are all
# the host has (this is the Revit-version-spanning fallback)
saved = DB.ParameterFilterRuleFactory
DB.ParameterFilterRuleFactory = revit_stubs.LegacyOnlyFactory
try:
    r = fp.make_filter_rule(pid, fp.OP_EQUALS, 1.5, ST.Double)
    check("double falls back to epsilon overload", len(r.args), 3)
    check("epsilon value passed", r.args[2], fp.DOUBLE_EPSILON)
    r = fp.make_filter_rule(pid, fp.OP_EQUALS, "x", ST.String)
    check("string falls back to caseSensitive overload", r.args[2], True)
    try:
        fp.make_filter_rule(pid, fp.OP_NOT_EQUALS, "x", ST.String)
        FAILS.append("missing factory method should raise")
    except fp.FilterProError:
        pass
finally:
    DB.ParameterFilterRuleFactory = saved

# --------------------------------------------------------- element counting
counts = {"Walls": 24, "Doors": 3, "Roofs": 24}
check("sort by name", [k for k, _ in fp.sort_entries(counts, fp.SORT_BY_NAME)],
      ["Doors", "Roofs", "Walls"])
check("sort by count, ties by name",
      [k for k, _ in fp.sort_entries(counts, fp.SORT_BY_COUNT)],
      ["Roofs", "Walls", "Doors"])
check("counts come through", fp.sort_entries(counts, fp.SORT_BY_NAME)[0], ("Doors", 3))
check("empty counts", fp.sort_entries({}, fp.SORT_BY_NAME), [])

# The point of numeric-aware sorting: a Thickness/Offset list must not read
# 100, 1000, 20 the way a plain text sort would give.
thickness = {"20": 4, "100": 9, "1000": 2}
raws = {"20": 20.0, "100": 100.0, "1000": 1000.0}
check("numeric values sort numerically",
      [k for k, _ in fp.sort_entries(thickness, fp.SORT_BY_NAME, raws)],
      ["20", "100", "1000"])
check("without raw values it is a text sort",
      [k for k, _ in fp.sort_entries(thickness, fp.SORT_BY_NAME)],
      ["100", "1000", "20"])
check("one non-numeric value falls back to text",
      [k for k, _ in fp.sort_entries(
          {"20": 1, "Varies": 1}, fp.SORT_BY_NAME, {"20": 20.0, "Varies": "Varies"})],
      ["20", "Varies"])
check("negative offsets order correctly",
      [k for k, _ in fp.sort_entries(
          {"-150": 1, "0": 1, "75": 1}, fp.SORT_BY_NAME,
          {"-150": -150.0, "0": 0.0, "75": 75.0})],
      ["-150", "0", "75"])
check("count sort ignores raw values",
      [k for k, _ in fp.sort_entries(thickness, fp.SORT_BY_COUNT, raws)],
      ["100", "20", "1000"])


class TypedEl(object):
    """An element whose type reports the SAME parameter name and value."""
    def __init__(self, inst, typ):
        self._inst, self._typ = inst, typ
        self.Id = DB.ElementId(1)

    def GetOrderedParameters(self):
        return self._inst

    def GetTypeId(self):
        return DB.ElementId(99)


class TypeEl(object):
    def __init__(self, params):
        self._p = params

    def GetOrderedParameters(self):
        return self._p


class NamedP(P):
    def __init__(self, name, value, storage=ST.String):
        P.__init__(self, value, storage)
        self.Definition = types.SimpleNamespace(Name=name, Id=DB.ElementId(42))


shared = [NamedP("Fire Rating", "60 min")]
el = TypedEl(shared, None)
doc2 = Doc({DB.ElementId(99): TypeEl([NamedP("Fire Rating", "60 min")])})
data = {}
fp.collect_element_parameters(el, doc2, data)
check("instance+type same value counts once",
      data["Fire Rating"]["counts"]["60 min"], 1)

# two distinct elements with the same value accumulate
fp.collect_element_parameters(TypedEl([NamedP("Fire Rating", "60 min")], None), doc2, data)
check("second element increments", data["Fire Rating"]["counts"]["60 min"], 2)

# an empty parameter is still offered, but contributes no count
data2 = {}
fp.collect_element_parameters(
    TypedEl([NamedP("Comments", None)], None), Doc({}), data2)
check("empty param still listed", "Comments" in data2, True)
check("empty param has no counted value", data2["Comments"]["counts"], {})

# --------------------------------------------------- checkbox list plumbing
class FakeItems(list):
    """A .NET-style ItemCollection: Add/Clear on top of list iteration."""
    def Add(self, item):
        self.append(item)

    def Clear(self):
        del self[:]


class FakeListBox(object):
    """Enough ListBox surface for the check-list helpers."""
    def __init__(self):
        self.Items = FakeItems()


lb = FakeListBox()
toggles = []
fp.populate_check_list(lb, [("Walls", 24), ("Doors", 3)],
                       lambda k: k == "Walls", lambda *a: toggles.append(a))
check("one row per entry", len(lb.Items), 2)
check("real key rides on Tag", [i.Tag for i in lb.Items], ["Walls", "Doors"])
check("initial tick state honoured", [i.IsChecked for i in lb.Items], [True, False])
check("checked_keys reads the ticks", fp.checked_keys(lb), {"Walls"})

fp.set_all_checked(lb, True)
check("check all", fp.checked_keys(lb), {"Walls", "Doors"})
fp.set_all_checked(lb, False)
check("check none means ignore this axis", fp.checked_keys(lb), set())

# Re-populating must not accumulate rows.
fp.populate_check_list(lb, [("Roofs", 1)], lambda k: True, lambda *a: None)
check("repopulate replaces rather than appends", len(lb.Items), 1)

# A row that is not a CheckBox must not break the readers.
lb.Items.append(object())
check("non-checkbox rows are skipped", fp.checked_keys(lb), {"Roofs"})
fp.set_all_checked(lb, False)
check("set_all tolerates foreign rows", fp.checked_keys(lb), set())

# ------------------------------------------------- builtin param resolution
# SYMBOL_FAMILY_NAME_PARAM is deliberately absent from the stub, mimicking a
# host where only some of the candidates exist.
check("picks the first filterable candidate",
      fp.resolve_builtin_param(["ALL_MODEL_FAMILY_NAME", "SYMBOL_FAMILY_NAME_PARAM"],
                               {DB.BuiltInParameter.ALL_MODEL_FAMILY_NAME}),
      DB.ElementId(DB.BuiltInParameter.ALL_MODEL_FAMILY_NAME))
check("skips a candidate that is not filterable",
      fp.resolve_builtin_param(["ALL_MODEL_FAMILY_NAME"], set()), None)
check("skips a candidate this Revit does not define",
      fp.resolve_builtin_param(["NO_SUCH_BUILTIN_PARAM"], None), None)
check("unknown filterable set still returns something",
      fp.resolve_builtin_param(["ELEM_FAMILY_PARAM"], None),
      DB.ElementId(DB.BuiltInParameter.ELEM_FAMILY_PARAM))

# ------------------------------------------------------------- misc invariants
check("every operator has a native method",
      sorted(fp.NATIVE_RULE_METHODS.keys()), sorted(fp.OPERATORS))
covered = set(fp.LIST_VALUE_OPS) | set(fp.NUMERIC_OPS) | set(fp.TEXT_OPS) | set(fp.NO_VALUE_OPS)
check("every operator is in exactly one input class", sorted(covered), sorted(fp.OPERATORS))
check("negative ops are real ops", [o for o in fp.NEGATIVE_OPS if o not in fp.OPERATORS], [])

print("=" * 62)
if FAILS:
    print("%d FAILURE(S):" % len(FAILS))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All logic checks passed.")
