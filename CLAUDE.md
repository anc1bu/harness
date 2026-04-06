# CLAUDE.md

## What This Project Is

**Harness** is a SAP data reference and transactional data management tool. It ingests SAP metadata (DD03L field definitions, DD04T field descriptions, DD08L check-table mappings) and transactional table exports, then provides a web UI for browsing and enriching the data.

## Working Guidelines

Before starting any task, state how you'll verify the work. After completing it, verify it.

**Frontend** (`index.html`, `login.html`): Vanilla JS SPA. No build step, no framework. Testing is manual via the web UI.
