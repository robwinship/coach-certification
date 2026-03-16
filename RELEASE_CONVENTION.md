# Release Convention

## Versioning

- Custom project versioning is used.
- Baseline release starts at v1.101.
- Subtle changes increment by 0.001 (example: v1.101 -> v1.102).
- Major release bumps happen only when explicitly instructed.

## Commit Message Pattern

Use Conventional Commit style where practical:

- feat: new feature
- fix: bug fix
- chore: maintenance
- docs: documentation updates
- refactor: internal restructuring

## Changelog Workflow

1. Keep all upcoming work in CHANGELOG.md under Unreleased.
2. Before a release, move Unreleased entries into a new version section.
3. Add release date in YYYY-MM-DD format.
4. Keep categories: Added, Changed, Fixed.

## Release Steps

1. Ensure main is up to date.
2. Update CHANGELOG.md with the new version section.
3. Commit release metadata:
   - git add CHANGELOG.md
   - git commit -m "chore(release): vX.Y.Z"
4. Create an annotated tag:
   - git tag -a vX.Y.Z -m "Release vX.Y.Z"
5. Push commit and tag:
   - git push
   - git push origin vX.Y.Z
6. Create a GitHub Release from tag vX.Y.Z.
7. Paste the matching CHANGELOG.md section into release notes.

## Suggested Cadence

- Create a release after each meaningful milestone.
- Use 0.001 increments for subtle updates.
- Use larger jumps only when a milestone or major release is requested.

## Initial Baseline

- Current baseline version: 1.101
- Date: 2026-03-15
