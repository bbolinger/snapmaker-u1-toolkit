# U1 orient rotation and slice review — 2026-06

Root cause: Orca `--orient` reports a best down-vector/cost but does not export a rotated STL. The toolkit must parse/receive that vector, rotate the mesh itself, drop Z-min to the bed, write `oriented.stl`, and use that same file for both render and slice.

Regression: EGO String Trimmer holder should render with the diagonal gusset face on the bed. If step-5 render shows the source orientation (vertical mounting plate / horizontal U-cradle / negative Z), the rotation step did not run.
