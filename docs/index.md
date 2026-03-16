---
title: Sarnia Coaches Checker
---

# Sarnia Coaches Multi-URL Status Checker

This project monitors two RegisterOBA pages for entries containing "Sarnia" and tracks changes over time.

## What It Does

- Fetches monitored pages
- Detects additions and removals
- Saves snapshots for comparison
- Sends optional Slack alerts when changes are detected

## Monitored URLs

- https://www.registeroba.ca/certified-coaches
- https://www.registeroba.ca/certification-inprogress-by-local

## Repository

Source code and setup instructions are in this repository.

## Notes

- GitHub Pages hosts this documentation site.
- The Python checker itself should run through GitHub Actions or your local machine.
- Please follow the target site's robots.txt and terms of use.
