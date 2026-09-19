"""Run every offline check for the Filter Pro bundle.

These run on plain CPython 3 - they do not need Revit, pyRevit or IronPython.
They cover what can be verified without a live Revit session:
syntax, XAML well-formedness, XAML/script wiring, and the pure matching and
rule-building logic (against stubbed Revit types).

    python3 tests/run_all.py
"""
import ast, io, os, subprocess, sys, xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUNDLE = os.path.join(ROOT, "Filter.extension", "Filter Pro.tab", "Pro.panel",
                      "Filter Pro.pushbutton")
failures = []


def report(name, ok, detail=""):
    print("%-34s %s%s" % (name, "PASS" if ok else "FAIL",
                          ("  " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


# 1. script.py parses as Python
try:
    ast.parse(io.open(os.path.join(BUNDLE, "script.py"), encoding="utf-8").read())
    report("script.py syntax", True)
except SyntaxError as exc:
    report("script.py syntax", False, str(exc))

# 2. ui.xaml is well-formed XML
try:
    ET.parse(os.path.join(BUNDLE, "ui.xaml"))
    report("ui.xaml well-formed", True)
except ET.ParseError as exc:
    report("ui.xaml well-formed", False, str(exc))

# 3 and 4. wiring and logic
for label, module in (("ui.xaml <-> script.py wiring", "test_ui_wiring.py"),
                      ("matching and rule logic", "test_logic.py")):
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, module)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = proc.communicate()[0].decode("utf-8", "replace")
    report(label, proc.returncode == 0)
    if proc.returncode != 0:
        print(out)

print("\n%s" % ("ALL CHECKS PASSED" if not failures
                else "FAILED: " + ", ".join(failures)))
sys.exit(1 if failures else 0)
