# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

**Harness** is a SAP data reference and transactional data management tool. It ingests SAP metadata (DD03L field definitions, DD04T field descriptions, DD08L check-table mappings) and transactional table exports, then provides a web UI for browsing and enriching the data.

## Working Guidelines

Before starting any task, state how you'll verify the work. After completing it, verify it — check best practices, efficiency, and absence of regressions.

No test suite exists. Testing is manual via the web UI.

## Architecture

**Frontend** (`index.html`, `login.html`): Vanilla JS SPA. No build step, no framework.

- `renderTable()` renders `rows`/`columns` globals into the data table panel; shows first 200 rows
- `toast()` displays modal notifications (`ok` / `warn` / `err`)
- `simulation` is reserved for the D3 relationship graph
