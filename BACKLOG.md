# Backlog

## Verification

- Investigate `FloodPathResolutionTest.test_filters_flood_catalog_rows_to_overlay_bbox`.
  On 2026-07-14, `python -m unittest discover -s tests -v` passed 14 of 15
  tests; this test returned no in-bounds flood path where one was expected.
  The failure existed during repository cleanup and was not hidden by moving
  the active overlay code or its tests.
