"""Minimal fakes for the Revit/.NET surface script.py imports, so the pure
matching + rule-building logic can be exercised without Revit."""
import sys, types


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class StorageType(object):
    String, Integer, Double, ElementId = "String", "Integer", "Double", "ElementId"


class ElementId(object):
    InvalidElementId = None

    def __init__(self, value):
        self.Value = int(value) if not isinstance(value, str) else hash(value)
        self._label = value

    def __eq__(self, o):
        return isinstance(o, ElementId) and o.Value == self.Value

    def __ne__(self, o):
        return not self.__eq__(o)

    def __hash__(self):
        return hash(self.Value)

    def __repr__(self):
        return "Eid(%s)" % (self._label,)


ElementId.InvalidElementId = ElementId(-1)


class BuiltInParameter(object):
    ELEM_FAMILY_PARAM = 1001
    ALL_MODEL_FAMILY_NAME = 1002
    VIS_GRAPHICS_FILTERS = 1003


class _Rule(object):
    """Records which factory call produced it, for assertions."""
    def __init__(self, method, args):
        self.method, self.args = method, args

    def __repr__(self):
        return "Rule(%s,%r)" % (self.method, self.args)


class ParameterFilterRuleFactory(object):
    """Every method accepts (param, value) only, and REJECTS the legacy
    3-argument form - so the fallback logic in make_filter_rule is exercised
    in its 'modern signature wins' direction."""
    calls = []

    @classmethod
    def _make(cls, name):
        def factory(*args):
            if len(args) > 2:
                raise TypeError("no overload takes %d args" % len(args))
            cls.calls.append((name, args))
            return _Rule(name, args)
        return staticmethod(factory)


for _n in ("CreateEqualsRule", "CreateNotEqualsRule", "CreateContainsRule",
           "CreateNotContainsRule", "CreateBeginsWithRule", "CreateEndsWithRule",
           "CreateGreaterRule", "CreateGreaterOrEqualRule", "CreateLessRule",
           "CreateLessOrEqualRule", "CreateHasValueParameterRule",
           "CreateHasNoValueParameterRule"):
    setattr(ParameterFilterRuleFactory, _n, ParameterFilterRuleFactory._make(_n))


class LegacyOnlyFactory(object):
    """Opposite host: only the deprecated 3-argument overloads exist, and only
    for a few operators - deliberately NOT a subclass, so methods this host
    lacks really are absent rather than inherited."""
    calls = []

    @classmethod
    def _make(cls, name):
        def factory(*args):
            if len(args) != 3:
                raise TypeError("legacy host needs 3 args, got %d" % len(args))
            cls.calls.append((name, args))
            return _Rule(name, args)
        return staticmethod(factory)


for _n in ("CreateEqualsRule", "CreateContainsRule", "CreateGreaterRule"):
    setattr(LegacyOnlyFactory, _n, LegacyOnlyFactory._make(_n))


class ElementFilter(object):
    pass


class ElementParameterFilter(ElementFilter):
    def __init__(self, rule):
        self.rule = rule


class LogicalOrFilter(ElementFilter):
    def __init__(self, filters):
        self.filters = list(filters)


class LogicalAndFilter(ElementFilter):
    def __init__(self, filters):
        self.filters = list(filters)


class _Collector(object):
    def __init__(self, items=()):
        self.items = list(items)

    def OfClass(self, _c):
        return self

    def WhereElementIsNotElementType(self):
        return self

    def WherePasses(self, _f):
        return self

    def __iter__(self):
        return iter(self.items)


DB = _mod("_DBstub",
          StorageType=StorageType, ElementId=ElementId,
          BuiltInParameter=BuiltInParameter,
          ParameterFilterRuleFactory=ParameterFilterRuleFactory,
          ElementFilter=ElementFilter,
          ElementParameterFilter=ElementParameterFilter,
          LogicalOrFilter=LogicalOrFilter, LogicalAndFilter=LogicalAndFilter,
          FilteredElementCollector=lambda *a: _Collector(),
          ParameterFilterElement=object, FillPatternElement=object,
          FillPatternTarget=types.SimpleNamespace(Drafting="Drafting"),
          Color=lambda r, g, b: (r, g, b),
          OverrideGraphicSettings=object,
          ElementMulticategoryFilter=lambda ids: ElementFilter(),
          ParameterFilterUtilities=object, Transaction=object,
          TemporaryViewMode=types.SimpleNamespace(TemporaryHideIsolate=1))


class _WPFWindow(object):
    def __init__(self, *a, **k):
        pass


def install():
    _mod("pyrevit", DB=DB,
         forms=types.SimpleNamespace(WPFWindow=_WPFWindow, alert=lambda *a, **k: None,
                                     ask_for_string=lambda *a, **k: None),
         script=types.SimpleNamespace(get_logger=lambda: types.SimpleNamespace(
             debug=lambda *a, **k: None, error=lambda *a, **k: None)))
    class _GenericList(object):
        """Mimics IronPython's List[T](sequence) construction."""
        def __getitem__(self, _t):
            return lambda seq=(): list(seq)
    _mod("pyrevit.framework", List=_GenericList())
    _mod("System", Action=lambda f: f, Byte=int,
         AppDomain=types.SimpleNamespace(CurrentDomain=types.SimpleNamespace(
             GetData=lambda k: None, SetData=lambda k, v: None)))
    _mod("System.Windows",
         Visibility=types.SimpleNamespace(Collapsed=0),
         GridLength=lambda *a: a,
         GridUnitType=types.SimpleNamespace(Star="Star"),
         TextAlignment=types.SimpleNamespace(Right="Right"),
         Thickness=lambda *a: a)

    class _Children(list):
        def Add(self, item):
            self.append(item)

    class _FakeGrid(object):
        """Enough Grid surface for _build_count_row to run."""
        def __init__(self):
            self.ColumnDefinitions = _Children()
            self.Children = _Children()

        @staticmethod
        def SetColumn(_el, _n):
            pass

    class _FakeCheckBox(object):
        def __init__(self):
            self.Content = self.Tag = None
            self.IsChecked = False
            self.Checked = self.Unchecked = _FakeEvent()

    class _FakeEvent(object):
        def __iadd__(self, _handler):
            return self

    _mod("System.Windows.Controls",
         CheckBox=_FakeCheckBox,
         ColumnDefinition=lambda: types.SimpleNamespace(Width=None),
         TextBlock=lambda: types.SimpleNamespace(
             Text=None, TextAlignment=None, Margin=None),
         Grid=_FakeGrid)
    _mod("System.Windows.Media",
         Color=types.SimpleNamespace(FromRgb=lambda r, g, b: (r, g, b)),
         SolidColorBrush=lambda c: c)
    _mod("Autodesk")
    _mod("Autodesk.Revit")
    _mod("Autodesk.Revit.Exceptions",
         OperationCanceledException=type("OperationCanceledException", (Exception,), {}))
    _mod("Autodesk.Revit.UI", ExternalEvent=types.SimpleNamespace(Create=lambda h: None),
         IExternalEventHandler=object)
    _mod("Autodesk.Revit.UI.Selection", ObjectType=types.SimpleNamespace(Element=1))
    # pyrevit's `from pyrevit import DB` needs DB importable as a submodule too
    sys.modules["pyrevit.DB"] = DB
