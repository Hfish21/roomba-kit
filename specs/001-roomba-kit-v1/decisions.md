# Implementation decisions: Roomba Kit V1

Non-obvious calls made during implementation, within the spec and constitution. Newest last.

| # | Decision | Why | Alternative rejected |
|---|---|---|---|
| 1 | `pr-gate`'s diff-size cap counts hand-written lines only. Generated files (`uv.lock`, `docs/reference/*`, `tests/contract/snapshots/*`) are excluded, and each PR body states both counts. | Owner-approved standing override (execution plan D1). Without it, every PR that adds an operation BLOCKs on regenerated reference docs no one reviews by hand. | Separate "regen" PRs: doubles the PR count for no review value. |
| 2 | CI (T005) ships without the `gen_docs.py --check` step; the step is added in the PR that lands T039. | The script doesn't exist until T039, and a red CI on `main` would block every lane. | A stub `gen_docs.py`: a placeholder that passes is a false green. |
| 3 | Ruff excludes `.specify/` (vendored SpecKit scripts). | They're third-party tooling, patched locally, not kit code. | Reformatting them: makes re-applying upstream SpecKit updates harder. |
| 4 | `transport/discovery.py` takes an injected `match: Callable[[str], str \| None]` (SKU → profile ID) instead of importing `profiles.loader`. | The layering contract is `transport → core` only (plan, T003); T017's wording would have made transport import profiles. The service injects `match_profile`. | Allowing `transport → profiles`: weakens the isolation contract for one convenience call. |
| 5 | The CLI root auto-registers any `roomba_kit.cli.*` module exposing `register(group)`, and the API factory exposes `stream_presenters`. | Lets story lanes add CLI extras (wizard, aliases, watchers) and SSE as new files, never editing `cli/main.py` or `api/app.py` (the "new files per story" rule). | Editing a shared index per story: guaranteed merge conflicts across parallel lanes. |
