"""Browser acceptance test using an installed Edge; no external CDN is needed."""
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

OUT = Path("test-results")
OUT.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8050", wait_until="networkidle")
    page.locator("#kpi-load").filter(has_text="ГВт").wait_for(timeout=120000)
    # Check every supported model/horizon against the chart's actual data and API totals.
    for model, label in [("seasonal", "Недельный профиль"), ("boosting", "Градиентный бустинг")]:
        page.locator("#overview-model").select_option(model)
        for hours in (24, 72, 168):
            page.locator("#overview-horizon").select_option(str(hours))
            expect(page.locator("#overview-panel")).to_have_attribute("aria-busy", "false", timeout=60000)
            expect(page.locator("#overview-forecast-label")).to_have_text(f"{label} · {hours} ч")
            expect(page.locator("#kpi-energy-label")).to_have_text(f"Энергия · следующие {hours} ч")
            expect(page.locator("#temperature-period")).to_have_text(f"на следующие {hours} ч")
            assert page.evaluate("() => state.charts.get('overview-chart').rows.filter(r => r.prediction != null).length") == hours
            assert page.evaluate("() => document.querySelector('#kpi-energy').textContent === num(state.overviewForecast.summary.energy_mwh/1000,0)+'ГВт·ч'")
            assert page.evaluate("() => document.querySelector('#kpi-error').textContent === num(state.overviewForecast.meta.metrics.mape_pct,2)+'%'")
    page.locator('#history-controls [data-days="7"]').click()
    expect(page.locator("#overview-panel")).to_have_attribute("aria-busy", "false", timeout=60000)
    assert page.evaluate("() => state.charts.get('overview-chart').rows.filter(r => 'actual' in r).length") == 168
    expect(page.locator("#overview-horizon")).to_have_value("168")
    page.screenshot(path=str(OUT / "overview-desktop.png"), full_page=True)
    page.locator('[data-view="forecast"]').click()
    page.locator("#peak-table tr").first.wait_for()
    page.locator('[data-hours="168"]').click()
    page.wait_for_timeout(1000)
    page.locator("#temperature-delta").fill("5")
    page.locator("#apply-scenario").click()
    expect(page.locator('#forecast-export')).to_have_attribute('href', re.compile('temperature_delta=5'), timeout=60000)
    assert page.locator("#peak-table tr").count() == 5
    with page.expect_download() as download:
        page.locator("#forecast-export").click()
    download.value.save_as(str(OUT / "forecast-scenario.csv"))
    assert len((OUT / "forecast-scenario.csv").read_text(encoding="utf-8-sig").splitlines()) == 169
    page.screenshot(path=str(OUT / "forecast-desktop.png"), full_page=True)
    page.locator('[data-view="models"]').click()
    assert page.locator("#model-cards .model-card").count() == 2
    assert page.locator("#horizon-table tr").count() == 6
    page.screenshot(path=str(OUT / "models-desktop.png"), full_page=True)
    page.locator('[data-view="operations"]').click()
    page.locator("#runs-table tr").first.wait_for()
    page.locator("#lineage-button").click()
    page.locator(".lineage-step").first.wait_for()
    page.screenshot(path=str(OUT / "operations-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator('[data-view="overview"]').click()
    page.wait_for_timeout(500)
    expect(page.locator("#overview-horizon")).to_have_value("168")
    expect(page.locator("#overview-model")).to_have_value("boosting")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile horizontal overflow"
    page.screenshot(path=str(OUT / "overview-mobile.png"), full_page=True)
    assert not errors, errors
    browser.close()
print(json.dumps({"status": "ok", "console_errors": errors, "screenshots": str(OUT)}))
