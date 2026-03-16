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
