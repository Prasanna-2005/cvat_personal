# Patch Notes

This document tracks custom patches and enhancements maintained by Quantrium on top of upstream CVAT.

---

# [Released]

## [2026-08-03]
**Commits:** `c3c69a769` · `cc8d777a1`

### Added
- Added new `extract_table_with_qc` serverless function.

### Changed
- **QSIH-1056**: Implemented table extraction output mapping from Mistral OCR.
- Updated `sheet_populator` and `extract_table` functions to support Mistral OCR output mapping.
- Registered the new `extract_table_with_qc` serverless function in sheet_populator serverless function.

---

## [2026-07-24]
**Commit:** `9775d3718`

### Changed
- Improved serverless function reliability across all Nuclio custom functions (`extract_header`, `extract_table`, `layout_extractor`, `ppocr`, `sheet_populator`, `skewocr`, `template_duplicator`, `textvalidator`, `textvalidator-gflash3.1`) by introducing:
  - Liveness probes
  - Rate limiting configurations
  - Updated timeouts
- Updated `deploy-k8s.sh` and `helm-chart/values.override.yml` to reflect these reliability configurations.

---

## [2026-07-23]
**Commits:** `87d10a3b3` · `99f26deb3`

### Changed
- Changed validation model in `textvalidator` to Gemini-flash-lite 3.1 to improve response and retrieval speed.

### Fixed
- Removed service tier option from flex configuration in `textvalidator-gflash3.1`.

---

## [2026-07-22]
**Commits:** `2d712c15f` · `e6bdad476` · `ec4b71b34` · `53adacfdd` · `1cc749fe4` · `7ac1dcfdd`

### Added

#### Local Deployment Script
- Added `serverless/local_deploy.sh` script to streamline local function testing and deployment.

#### Custom Nuclio Dashboard & Timeout Patch
- Created `Dockerfile.nuclio-dashboard` and `patches/nuclio-nginx.conf` to bake raised proxy timeouts (`proxy_read_timeout 300s`) into custom `cvat/nuclio-dashboard:1.16.6-local` image, preventing 504 errors on long-running function invocations.

### Changed

#### Nuclio Directory Structure
- Improved directory structure for Nuclio serverless functions by nesting YAML/Python resources under dedicated `nuclio/` subdirectories (`extract_header`, `extract_table`, `sheet_populator`, `template_duplicator`).

#### QSIH-1007 — Async Layout Extractor
- Refactored `layout_extractor` Nuclio function to operate asynchronously for long-running layout inference tasks.

#### QSIH-1008 — Async Table & Header Extraction & MLflow Prompts
- Converted `extract_table` and `extract_header` functions into asynchronous execution handlers matching the `sheet_populator` architecture.
- Migrated prompt templates to MLflow for centralized prompt tracking and version management.

#### Deployment Configurations & Helm Settings
- Updated `helm-chart/values.override.yml` to set `functionInvocationTimeout: 5m` and reference local dashboard image.
- Standardized custom function scanning in `deploy-k8s.sh` to match `serverless/custom/*/nuclio`.

#### Documentation & Git Configurations
- Renamed patch notes documentation file from `Patch Notes.md` to `PATCH_NOTES.md`.
- Updated `.gitignore` to ignore local configurations and integration files.

### Fixed

#### Sheet Populator Response Handling
- Fixed downstream response handling in `sheet_populator` for better error resilience.

---

## [2026-07-21]
**Commits:** `d1187e037` · `864bb28a1` · `b78d4020d` · `f841edbc2` · `85c03b3c4` · `674e695c5` · `13487d952`

### Added

#### QSIH-1009 — HTTP/2 Integration & Async Text Validation
- Integrated HTTP/2 protocol support across serverless functions for faster sheet service communication.
- Added asynchronous execution triggers to `textvalidator` (QC-1) and `textvalidator-gflash3.1` (QC-2).

#### QSIH-1001 — Pydantic Structured Output
- Implemented Pydantic schema validation for VLM data extraction across `extract_header`, `extract_table`, and `sheet_populator`.

### Changed

#### QSIH-1010 — Template Duplicator Consolidation
- Merged `template_duplicator` functionality directly into `sheet_populator` and updated UI components (`tools-control.tsx`, `object-item-details.tsx`).

#### QSIH-1001 — Sheet Populator Refactoring
- Comprehensive refactoring of `sheet_populator` codebase for improved quality, retry mechanics, and modularity.

### Fixed

#### QSIH-1006 — CVAT Backend Timeout Fix
- Resolved HTTP read timeouts between CVAT backend and function invocation by adding `cvat-nginx.conf` proxy settings.

#### Fast-QC Model Adjustments
- Fine-tuned sampling temperature parameters for Fast-QC validation models (`textvalidator` and `textvalidator-gflash3.1`).

---

## [2026-07-20]
**Commit:** `4c73d518c`

### Changed

#### QSIH-1001 — Dynamic Sheet Selection
- Updated `sheet_populator` to automatically select the first sheet in target workbooks rather than using a hardcoded "Sheet1".

---

## [2026-07-17]
**Commits:** `5572d8f7b` · `f20d4e779` · `498b9fa40` · `a9f5df9cb`
### Added

#### QSIH-990 — Table Extraction
- Added a new `extract_table` serverless function for extracting table structures from document regions.
- Implements logic for table extraction and Excel population.

### Changed

#### QSIH-990 — Header Extractor Refinements
- Increased event timeout to 300s in `extract_header` function config.
- Refined extraction rules for improved accuracy.

#### QSIH-990 — Sheet Populator Fixes
- Fixed sheet-populator timeout configuration.
- Added handling for duplicate sheet copies.

### Fixed

#### Deployment
- Modified the Nuclio JSON key mount directory in `deploy_cpu.sh`.

---

## [2026-07-16]
**Commit:** `e3f05d779`
### Added

#### QSIH-990 — Excel Populator & Header Extractor
- Added `sheet_populator` serverless function to populate Google Sheets with extracted annotation data.
- Added `extract_header` serverless function for extracting document headers.
- Added `sheetPopulator.tsx` UI patch for triggering sheet population from the annotation interface.
- Integrated sheet population trigger into `tools-control.tsx` and `patch-helpers.tsx`.

---

## [2026-07-15]
**Commit:** `4e67bcb16`
### Added

#### QSIH-990 — Google Sheets Integration
- Introduced `template_duplicator` serverless function for duplicating Google Sheets templates via the Sheets API.
- Added a trigger button in the object details sidebar (`object-item-details.tsx`) to initiate template duplication.
- Extended `object-item-attribute.tsx` to support the new Sheets integration attributes.
- Updated `deploy_cpu.sh` to include the new serverless function in the deployment pipeline.

---

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