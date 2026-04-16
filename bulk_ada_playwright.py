#!/usr/bin/env python3
"""
Bulk ADA (WCAG 2.1 AA) content updater for twdCMS using Playwright.

Workflow:
1. Login to CMS
2. Read page URLs from CSV
3. For each page, open editor, extract HTML, run ADA fixes, replace content, save
4. Backup original HTML and write run logs
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError, sync_playwright

try:
    from openai import OpenAI
except ImportError:  # Optional dependency at runtime for AI mode
    OpenAI = None  # type: ignore[assignment]


@dataclass
class Config:
    base_url: str
    username: str
    password: str
    csv_path: Path
    backup_dir: Path
    log_file: Path
    headless: bool
    use_openai: bool
    openai_model: str
    max_retries: int = 1
    nav_timeout_ms: int = 45_000


class AdaAutomation:
    def __init__(self, config: Config):
        self.config = config
        self.logger = self._build_logger(config.log_file)
        self.processed: List[str] = []
        self.skipped: List[str] = []
        self.failed: List[str] = []

    def _build_logger(self, log_file: Path) -> logging.Logger:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("ada_automation")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()

        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        return logger

    def login(self, page: Page) -> None:
        """Log into CMS and wait until dashboard is loaded."""
        self.logger.info("Navigating to login page: %s", self.config.base_url)
        page.goto(self.config.base_url, wait_until="domcontentloaded", timeout=self.config.nav_timeout_ms)

        page.locator('input[name="username"], input[type="email"]').first.fill(self.config.username)
        page.locator('input[name="password"], input[type="password"]').first.fill(self.config.password)

        login_button = page.locator('button:has-text("Login"), button:has-text("Sign in"), input[type="submit"]').first
        login_button.click()

        # Wait for dashboard signal(s). Uses multiple signals to be resilient.
        page.wait_for_load_state("networkidle", timeout=self.config.nav_timeout_ms)
        page.wait_for_selector(
            'text=/Dashboard|Welcome|Content|Site Structure/i', timeout=self.config.nav_timeout_ms
        )
        self.logger.info("Login successful; dashboard loaded")

    def process_page(self, page: Page, page_url: str) -> bool:
        """Process one CMS page: open editor, fix HTML, save page."""
        self.logger.info("Processing page: %s", page_url)

        try:
            edit_url = self._ensure_edit_mode_url(page_url)
            page.goto(edit_url, wait_until="domcontentloaded", timeout=self.config.nav_timeout_ms)
            page.wait_for_load_state("networkidle", timeout=self.config.nav_timeout_ms)

            page.locator('text="Edit this Content Block"').first.click(timeout=10_000)
            page.locator('text="Edit"').first.click(timeout=10_000)

            editor = page.locator('[contenteditable="true"]').first
            editor.wait_for(state="visible", timeout=20_000)

            original_html = editor.evaluate("el => el.innerHTML")
            if not isinstance(original_html, str):
                raise RuntimeError("Failed to read HTML from contenteditable area")

            self._backup_original_html(page_url, original_html)

            fixed_html = self.fix_html(original_html, page_url)
            if not fixed_html.strip():
                raise RuntimeError("AI fixer returned empty HTML")

            editor.evaluate("(el, html) => { el.innerHTML = html; }", fixed_html)
            self.save_page(page)

            self.processed.append(page_url)
            self.logger.info("Processed successfully: %s", page_url)
            return True

        except Exception as exc:
            self.logger.error("Failed processing %s | %s", page_url, exc)
            return False

    def fix_html(self, html: str, page_url: str = "") -> str:
        """Apply strict ADA-focused fixes to HTML.

        Uses OpenAI when enabled, otherwise a deterministic fallback function.
        """
        if self.config.use_openai:
            return self._fix_html_with_openai(html, page_url)
        return self._fix_html_fallback(html)

    def _fix_html_with_openai(self, html: str, page_url: str = "") -> str:
        if OpenAI is None:
            raise RuntimeError("openai package not installed. Install with: pip install openai")

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY environment variable is missing")

        client = OpenAI(api_key=api_key)

        system_prompt = (
            "You are an ADA remediation assistant for CMS HTML. "
            "Apply ONLY WCAG 2.1 AA accessibility fixes requested below. "
            "Do not rewrite, summarize, remove, reorder, or structurally redesign content. "
            "Return HTML only, with no markdown fences, comments, or explanation."
        )

        user_prompt = f"""
Page URL: {page_url or 'N/A'}

Task:
Apply ONLY these fixes to the HTML:
1) Add missing alt attributes to <img> tags with concise descriptive text.
2) Fix heading hierarchy for logical H1 -> H2 -> H3 progression.
3) Replace vague anchor text like 'click here', 'read more', 'learn more' with descriptive anchor text based on nearby context.
4) Add accessibility attributes where appropriate (aria-label, role) without unnecessary additions.

Hard constraints:
- Keep all existing content.
- Do not summarize or rewrite body copy.
- Do not alter layout/structure unless minimally required for accessibility.
- Keep valid, clean HTML.

Input HTML:
{html}
""".strip()

        response = client.responses.create(
            model=self.config.openai_model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
        )

        fixed = (response.output_text or "").strip()
        if not fixed:
            raise RuntimeError("OpenAI returned empty response")
        return fixed

    def _fix_html_fallback(self, html: str) -> str:
        """Minimal deterministic fallback for environments without OpenAI."""
        updated = html

        # Add missing/empty alt attributes.
        def img_alt_replacer(match: re.Match[str]) -> str:
            img_tag = match.group(0)
            if re.search(r'\balt\s*=\s*(["\']).*?\1', img_tag, flags=re.IGNORECASE):
                return img_tag

            src_match = re.search(r'\bsrc\s*=\s*["\']([^"\']+)["\']', img_tag, flags=re.IGNORECASE)
            description = "Descriptive image"
            if src_match:
                filename = Path(src_match.group(1)).name
                base = re.sub(r'[_\-]+', ' ', Path(filename).stem).strip()
                if base:
                    description = f"Image of {base}"

            return img_tag[:-1] + f' alt="{description}">'

        updated = re.sub(r"<img\b[^>]*>", img_alt_replacer, updated, flags=re.IGNORECASE)

        # Replace common vague link texts conservatively.
        updated = re.sub(
            r'(<a\b[^>]*>)\s*(click here|read more|learn more)\s*(</a>)',
            r'\1View details\3',
            updated,
            flags=re.IGNORECASE,
        )

        return updated

    def save_page(self, page: Page) -> None:
        """Click save and wait for confirmation or a short delay."""
        save_btn = page.locator('button:has-text("SAVE"), input[value="SAVE"], text="SAVE"').first
        save_btn.click(timeout=10_000)

        # Prefer explicit confirmation, otherwise use timed delay fallback.
        try:
            page.wait_for_selector(
                'text=/Saved|Success|Updated|Changes saved/i', timeout=10_000
            )
        except TimeoutError:
            time.sleep(3)

    def run(self) -> None:
        urls = self._read_urls_from_csv(self.config.csv_path)
        self.logger.info("Loaded %s URLs from %s", len(urls), self.config.csv_path)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.config.headless)
            context = browser.new_context()
            page = context.new_page()

            self.login(page)

            for url in urls:
                success = self.process_page(page, url)
                if success:
                    continue

                # Retry once (or config.max_retries times)
                retried = False
                for attempt in range(self.config.max_retries):
                    self.logger.warning("Retry %s/%s for %s", attempt + 1, self.config.max_retries, url)
                    if self.process_page(page, url):
                        retried = True
                        break

                if not retried:
                    self.failed.append(url)

            context.close()
            browser.close()

        self._write_summary_log()

    def _ensure_edit_mode_url(self, url: str) -> str:
        if "edit" in url.lower():
            return url
        connector = "&" if "?" in url else "?"
        return f"{url}{connector}mode=edit"

    def _read_urls_from_csv(self, csv_path: Path) -> List[str]:
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV file not found: {csv_path}")

        urls: List[str] = []
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if "url" in (reader.fieldnames or []):
                for row in reader:
                    url = (row.get("url") or "").strip()
                    if url:
                        urls.append(url)
            else:
                fh.seek(0)
                raw_reader = csv.reader(fh)
                for row in raw_reader:
                    if row and row[0].strip() and row[0].strip().lower() != "url":
                        urls.append(row[0].strip())

        if not urls:
            raise ValueError("No URLs found in CSV. Include a 'url' column or first-column URLs.")
        return urls

    def _backup_original_html(self, page_url: str, html: str) -> None:
        self.config.backup_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", page_url).strip("_")[:80] or "page"
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        backup_file = self.config.backup_dir / f"{slug}_{timestamp}.html"
        backup_file.write_text(html, encoding="utf-8")

    def _write_summary_log(self) -> None:
        summary = {
            "processed_count": len(self.processed),
            "failed_count": len(self.failed),
            "skipped_count": len(self.skipped),
            "processed": self.processed,
            "failed": self.failed,
            "skipped": self.skipped,
            "timestamp_utc": datetime.utcnow().isoformat() + "Z",
        }
        summary_path = self.config.log_file.with_suffix(".summary.json")
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        self.logger.info("Run complete | Processed: %s | Failed: %s | Skipped: %s", len(self.processed), len(self.failed), len(self.skipped))
        self.logger.info("Summary JSON written to: %s", summary_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bulk ADA CMS updater using Playwright")
    parser.add_argument("--base-url", required=True, help="CMS login URL")
    parser.add_argument("--username", required=True, help="CMS username")
    parser.add_argument("--password", required=True, help="CMS password")
    parser.add_argument("--csv", required=True, help="Path to CSV containing page URLs")
    parser.add_argument("--backup-dir", default="backups", help="Directory to save original HTML backups")
    parser.add_argument("--log-file", default="logs/ada_automation.log", help="Path to log file")
    parser.add_argument("--headless", action="store_true", help="Run browser headless (default is visible for debug)")
    parser.add_argument("--use-openai", action="store_true", help="Use OpenAI API for ADA HTML fixes")
    parser.add_argument("--openai-model", default="gpt-4.1-mini", help="OpenAI model for HTML remediation")
    parser.add_argument("--max-retries", type=int, default=1, help="Number of retries for failed pages")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = Config(
        base_url=args.base_url,
        username=args.username,
        password=args.password,
        csv_path=Path(args.csv),
        backup_dir=Path(args.backup_dir),
        log_file=Path(args.log_file),
        headless=args.headless,
        use_openai=args.use_openai,
        openai_model=args.openai_model,
        max_retries=args.max_retries,
    )

    automation = AdaAutomation(config)
    automation.run()


if __name__ == "__main__":
    main()
