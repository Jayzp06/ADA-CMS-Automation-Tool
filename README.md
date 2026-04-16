# ADA-CMS-Automation-Tool

Automated accessibility (ADA / WCAG 2.1 AA) compliance system for bulk-updating website content inside a CMS (twdCMS / TerminalFour).

## Script

`bulk_ada_playwright.py` is a production-ready Python Playwright automation script that:

- Logs into the CMS.
- Reads page URLs from CSV.
- Opens each page in edit mode and extracts HTML from `[contenteditable="true"]`.
- Runs strict ADA-focused HTML fixes (OpenAI mode or deterministic fallback).
- Replaces content, saves page, retries failures once, and continues.
- Writes per-page HTML backups and structured logs.

## Install

```bash
pip install playwright openai
playwright install chromium
```

## CSV format

Use either:

- A header named `url`, or
- URLs in the first column.

Example:

```csv
url
https://example-cms/page-1
https://example-cms/page-2
```

## Run (debug mode, visible browser)

```bash
python bulk_ada_playwright.py \
  --base-url "https://your-cms.example.com/login" \
  --username "your_username" \
  --password "your_password" \
  --csv "pages.csv" \
  --use-openai
```

## Run headless (for scale)

```bash
python bulk_ada_playwright.py \
  --base-url "https://your-cms.example.com/login" \
  --username "your_username" \
  --password "your_password" \
  --csv "pages.csv" \
  --use-openai \
  --headless
```

Set `OPENAI_API_KEY` in environment before using `--use-openai`.
