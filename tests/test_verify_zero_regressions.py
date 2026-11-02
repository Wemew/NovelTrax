import sys
import os

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
sys.path.insert(0, os.path.join(project_root, "backend"))

import app.calibre.scanner as scanner
from tests.test_verify_synthesis import parse_calibre_volumes_and_series_candidate

# Monkeypatch
scanner.parse_calibre_volumes_and_series = parse_calibre_volumes_and_series_candidate

print("[Monkeypatch] scanner.parse_calibre_volumes_and_series patched with candidate synthesis.")

# Run test 1: test_challenger_m2_omnibus
print("\n=== Running test_challenger_m2_omnibus ===")
import tests.test_challenger_m2_omnibus as t1
t1.parse_calibre_volumes_and_series = parse_calibre_volumes_and_series_candidate
t1.test_volume_ranges()
t1.test_comma_plus_ampersand_lists()
t1.test_single_volume_overrides()
t1.test_fractional_volumes()
t1.test_dual_series_titles_and_scanner()
t1.test_extreme_and_adversarial_inputs()
t1.test_non_strippable_assertions()
res1 = t1.recorder.summary()
assert res1 == 0, f"{res1} omnibus tests failed!"

# Run test 2: test_challenger_m2_it3_adversarial
print("\n=== Running test_challenger_m2_it3_adversarial ===")
import tests.test_challenger_m2_it3_adversarial as t3
t3.parse_calibre_volumes_and_series = parse_calibre_volumes_and_series_candidate
t3.test_multiple_hyphens()
t3.test_numeric_years_and_numbered_franchises()
t3.test_delimiter_combinations()
t3.test_reverse_omnibus_ranges()
t3.test_unicode_dashes_and_range_connectors()
t3.test_calibre_scanner_adversarial_integration()
t3.test_assertion_survives_optimization()
res3 = t3.recorder.summary()
assert res3 == 0, f"{res3} adversarial tests failed!"

print("\nALL IN-MEMORY MONKEYPATCHED REGRESSION TESTS PASSED (100% SUCCESS)!")
