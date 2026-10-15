"""Capture compact, readable figures from a running local GridPulse instance."""
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

out=Path(__file__).resolve().parents[1]/'docs/report-assets'
out.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1200},device_scale_factor=1)
    page.goto('http://127.0.0.1:8050',wait_until='networkidle')
    page.locator('#kpi-load').filter(has_text='ГВт').wait_for(timeout=120000)
    expect(page.locator('#overview-panel')).to_have_attribute('aria-busy','false',timeout=60000)
    top=page.locator('#view-overview').bounding_box();chart=page.locator('#overview-panel').bounding_box()
    page.screenshot(path=str(out/'overview.png'),clip={'x':top['x'],'y':top['y'],'width':top['width'],'height':chart['y']+chart['height']-top['y']})
    page.locator('[data-view="forecast"]').click();page.locator('#peak-table tr').first.wait_for()
    page.set_viewport_size({'width':900,'height':1400})
    page.locator('.scenario-panel').screenshot(path=str(out/'scenario.png'))
    page.set_viewport_size({'width':1440,'height':1200})
    page.locator('[data-view="models"]').click()
    top=page.locator('#view-models').bounding_box();last=page.locator('#horizon-table').locator('xpath=ancestor::article').bounding_box()
    page.screenshot(path=str(out/'models.png'),clip={'x':top['x'],'y':top['y'],'width':top['width'],'height':last['y']+last['height']-top['y']})
    page.locator('[data-view="operations"]').click();page.locator('#runs-table tr').first.wait_for()
    page.locator('#quality-grid').locator('xpath=ancestor::article').screenshot(path=str(out/'operations.png'))
    browser.close()
print('Captured four report figures')
