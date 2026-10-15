"""Build reviewable DOCX/PDF deliverables from versioned source documents."""
from pathlib import Path

from build_report import main as build_report
from playwright.sync_api import sync_playwright

base = Path(__file__).resolve().parents[1]
build_report()

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    page.goto((base / "docs/presentation.html").as_uri(), wait_until="networkidle")
    page.pdf(path=str(base / "docs/presentation.pdf"), width="1280px", height="720px", print_background=True, prefer_css_page_size=True)
    browser.close()
print("Created docs/report.docx and docs/presentation.pdf; run tools/render_report.ps1 -Update for the report PDF and TOC")
