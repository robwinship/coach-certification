# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog.
This project uses a custom release numbering convention.

## Unreleased

### Added
- Placeholder for upcoming features.

### Changed
- Placeholder for upcoming changes.

### Fixed
- Placeholder for upcoming fixes.

## 1.102 - 2026-03-16

### Added
- Slack incoming webhook notification system: change-based alerts for coach additions and removals.
- `manual_slack_test.yml` GitHub Actions workflow for on-demand Slack test messages.

### Changed
- Removed temporary "Run Slack Test" and "Open Slack Test Action" buttons from tracker webpages now that the workflow trigger location is confirmed.

## 1.101 - 2026-03-15

### Added
- GitHub Pages tracker site with dashboard and detail pages.
- Certified and In Progress coach list views.
- Current Summary static page.
- OBA update date tracking panel (Date Updated and Next Scheduled Update).
- Transition detection for coaches moving from In Progress to Certified.

### Changed
- Switched data collection to Playwright plus Wix cloud-data responses for reliable dynamic-page scraping.
- Added query timestamps (local and UTC) to status payload and page metadata.
- Updated workflow schedule to run twice daily.

### Fixed
- Resolved zero-row extraction issue caused by delayed JavaScript rendering.
- Improved scraper resilience for network-idle timeout behavior.

## [Backup 2026-04-07_16-53-50]
### Backup Note
- Initial backup before coach sorting
