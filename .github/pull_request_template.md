## Summary

<!-- What changed and why. -->

## Roadmap item

<!-- For example: Stage F, item F.2. Update its status in docs/ROADMAP.md in this pull request. -->

## Constraint changes

- [ ] This change does not alter scope, non-goals, tool or command constraints, or security boundaries.
- [ ] This change does alter them, and it includes the ADR (`docs/adr/NNNN-...`) and the matching specification change.

## Documentation updated

- [ ] `README.md`
- [ ] `docs/DESIGN.md`
- [ ] `docs/THREAT_MODEL.md`
- [ ] `docs/COMPLIANCE.md`
- [ ] `docs/EVAL_REPORT.md`
- [ ] `CHANGELOG.md`
- [ ] `docs/ROADMAP.md`
- [ ] Not applicable. Reason:

## Verification

- [ ] `ruff format --check .` and `ruff check .`
- [ ] `pytest --cov=proofcoder`
- [ ] `python scripts/compliance_check.py`
- [ ] `python scripts/secret_scan.py` (on Git older than 2.44, `--scope working-tree --scope index`)
- [ ] Evaluation fixtures added or updated, if the stage exit criteria require them

<!-- Paste the commands you ran and their results. -->
