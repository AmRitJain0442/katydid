import sys
import xml.etree.ElementTree as ET
from app import total

cases = [([], 0), ([1, 2, 3], 6), ([-4, 4], 0), ([7], 7), ([0, 0], 0)]
suite = ET.Element("testsuite", name="pricing", tests=str(len(cases)))
failed = 0
for index, (prices, expected) in enumerate(cases):
    case = ET.SubElement(suite, "testcase", name=f"total_{index}", classname="pricing")
    try:
        actual = total(prices)
        assert actual == expected, f"total({prices}) expected {expected}, got {actual}"
    except Exception as error:
        failed += 1
        ET.SubElement(case, "failure", message=str(error)).text = str(error)
        print(error)
suite.set("failures", str(failed))
ET.ElementTree(suite).write(sys.argv[1], encoding="utf-8", xml_declaration=True)
raise SystemExit(1 if failed else 0)
