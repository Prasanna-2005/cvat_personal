# Patch Notes

This document tracks custom patches and enhancements maintained by Quantrium on top of upstream CVAT.

---

# [Unreleased]

## Added
Not merged with main:
`feature/QSIH-921-populate-block_no-and-line_no`
### QSIH-921 — Auto Populate Line Number & Block Number
- Introduced an ID Generator interactor to automatically populate `line_no` and `block_no` attributes for selected annotations.
- Integrated automatic attribute assignment into the annotation workflow.

---

# [Released]

## [2026-07-09]
**Commit:** `198b10ead96de3010798d831bcf6d50e42a54277`
### Added

#### QSIH-923 — Model Name Suffixes
- Added model name suffixes to interactors and detectors for improved model identification within the UI.

#### QSIH-920 — Group Clear
- Added a Group Clear interactor to bulk remove annotations belonging to the selected group.

#### QSIH-919 — Group Propagate
- Added a Group Propagate interactor to propagate annotation attributes across grouped objects.

#### QSIH-915 — FastQC-2
- Introduced FastQC-2 to automatically remove unresolved FastQC-1 issues before generating new validation issues using **Gemini Flash-Lite 3.1**.
- Improved the validation workflow for repeated quality control passes.

---

## [2026-07-07]
**Commit:** `3460c1a5ee7d90cbf1e5548d69b097f84204cfcd`
### Added

#### QSIH-890 — PaddleOCR Layout Integration
- Integrated the PaddleOCR Layout detector with CVAT detectors.
- Added automatic label mapping support for layout detection.

---

## [2026-07-03]
**Commit:** `3c9aa1128a254e3be2d7a8d70d358169faa4cd9e`
### Changed

#### Frontend Refactor
- Refactored interactor handling logic from `tools-control.tsx` into the `cvat-ui/src/patches/` directory for improved modularity and maintainability.

#### QSIH-885 — Linearisation Improvements
- Improved OCR text linearisation for skewed polygon annotations.
- Enhanced text grouping and reading order for rotated document regions.

### Removed
- Removed interactor handling logic from `tools-control.tsx`.

---

## [2026-07-01]
**Commit:**`e111b7e7163ed18cf6b1b560e1db6f088bbaa0c3`
### Added

#### MLflow Integration
- Added MLflow tracing for VLM API calls used by FastQC-1.

#### Deployment
- Added a unified `deploy.sh` script to simplify deployment.

### Changed

#### FastQC-1 Validation
- Added colour-difference based validation using `jsdiff` to improve text mismatch detection.

### Fixed

#### Skew OCR
- Fixed incorrect classification of polygon annotations as rectangles in the Skew OCR interactor.

### Removed
- Removed individual `deploy_<tool>.sh` scripts in favour of a unified `deploy.sh`.

---

## [2026-06-30]
**Commit:**`3a060255a1b4c43c6ad1b52eea957c02259fbf7f`
### Added

#### Skew OCR
- Added a dedicated Skew OCR interactor for extracting text from rotated document regions.
- Integrated PaddleOCR for OCR inference on skewed document regions.
- Added `deploy_skewocr.sh`.
- Integrated the validation workflow into the CVAT annotation interface (`tools-control.tsx`).

### Changed

#### FastQC-1
- Improved the OCR validation pipeline with enhanced text grouping.
- Added polygon annotation support for FastQC-1 validation.

---

## [2026-06-29]
**Commit:**`768a92ae7a053d8ab533256b38c366a748a7f654`
### Added

#### FastQC-1
- Introduced the first OCR validation interactor for validating extracted text against document regions.
- Integrated **Gemma-4-26B-A4B-IT** as the validation model.
- Optimized inference by packing multiple image crops using `rectpack`, reducing API calls and improving performance.
- Added Django patches (`lambda_views.py`) and required `docker-compose.yml` configuration.
- Integrated the validation workflow into the CVAT annotation interface (`tools-control.tsx`).
- Added `deploy_test_validator.sh`.

---

## [2026-06-19]
**Commit:** `31018ea4259a540892bc56dd0ecb2c2e15a76f9c
`
### Added

#### CVAT OCR Interactor
- Introduced the first OCR interactor for CVAT using PaddleOCR through a Nuclio serverless function.
- Enabled automatic OCR attribute population when users draw bounding boxes.
- Integrated OCR inference into the annotation workflow (`tools-control.tsx`).