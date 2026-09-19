"""Cross-check ui.xaml against script.py: every handler must exist, every
self.<widget> must be a real x:Name, and every x:Name should be used."""
import ast, io, os, re, sys, xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.join(os.path.dirname(HERE),
                      "Filter.extension", "Filter Pro.tab", "Pro.panel",
                      "Filter Pro.pushbutton")
XAML = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BUNDLE, "ui.xaml")
SCRIPT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(BUNDLE, "script.py")
X = "{http://schemas.microsoft.com/winfx/2006/xaml}"
EVENTS = {"Click", "SelectionChanged", "TextChanged", "ValueChanged",
          "Checked", "Unchecked", "Loaded", "Closed", "Closing",
          "MouseDoubleClick", "KeyDown"}

names, handlers = set(), {}
for el in ET.parse(XAML).getroot().iter():
    for attr, val in el.attrib.items():
        if attr == X + "Name":
            names.add(val)
        elif attr in EVENTS:
            handlers.setdefault(val, []).append("%s.%s" % (el.tag.rsplit('}')[-1], attr))

tree = ast.parse(io.open(SCRIPT, encoding="utf-8").read())
cls = next(n for n in ast.walk(tree)
           if isinstance(n, ast.ClassDef) and n.name == "FilterProWindow")
methods = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}

# self.X referenced anywhere in the class
selfattrs = set()
for node in ast.walk(cls):
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self"):
        selfattrs.add(node.attr)
# self.X assigned in the class = our own state, not a widget
assigned = set()
for node in ast.walk(cls):
    tgts = []
    if isinstance(node, ast.Assign):
        tgts = node.targets
    elif isinstance(node, ast.AugAssign):
        tgts = [node.target]
    for t in tgts:
        if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                and t.value.id == "self"):
            assigned.add(t.attr)

fail = False
missing = sorted(h for h in handlers if h not in methods)
if missing:
    fail = True
    for h in missing:
        print("FAIL  XAML handler '%s' (%s) has no method" % (h, ", ".join(handlers[h])))

widget_like = sorted(a for a in selfattrs
                     if re.match(r'^(lst|cmb|txt|btn|chk|lbl|slider|rect|tab)', a))
ghosts = [a for a in widget_like if a not in names]
if ghosts:
    fail = True
    for g in ghosts:
        print("FAIL  script uses self.%s but ui.xaml has no such x:Name" % g)

unused = sorted(n for n in names if n not in selfattrs)
for u in unused:
    print("warn  x:Name '%s' declared in XAML but never used in script" % u)

print("\n%d x:Names, %d handlers, %d methods" % (len(names), len(handlers), len(methods)))
print("handlers all resolved: %s" % (not missing))
print("widget refs all backed by XAML: %s" % (not ghosts))
sys.exit(1 if fail else 0)
