"""Real browser checks against a running native website; no synthetic ads.
Run from the repository root after: cars web
python reference-parser-daa/scripts/smoke_native_web.py --url http://127.0.0.1:8000
"""
import argparse
import json
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.common.exceptions import StaleElementReferenceException

parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:8000')
parser.add_argument('--output', default='reference-parser-daa/runtime/diagnostics/web-smoke')
args = parser.parse_args()
out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
options = webdriver.ChromeOptions()
options.page_load_strategy = 'eager'
options.add_argument('--headless=new'); options.add_argument('--no-proxy-server')
options.add_argument('--window-size=1440,1100')
options.set_capability('goog:loggingPrefs', {'browser':'ALL'})
driver = webdriver.Chrome(options=options)
driver.set_page_load_timeout(20)
wait = WebDriverWait(driver, 25, ignored_exceptions=(StaleElementReferenceException,))
checks = []
def api(path):
    return driver.execute_async_script('const done=arguments[arguments.length-1];fetch(arguments[0]).then(r=>r.json()).then(done)', path)
def wait_for_photos():
    for card in driver.find_elements(By.CSS_SELECTOR,'.car-card'):
        if card.find_element(By.CSS_SELECTOR,'.gallery-button').is_enabled():
            driver.execute_script('arguments[0].scrollIntoView({block:"center"})',card)
            wait.until(lambda d: driver.execute_script('return arguments[0].querySelector("img").naturalWidth>0',card))
try:
    driver.get(args.url)
    wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '.car-card'))
    feed = api('/api/feed/')
    assert feed['total_all'] > 0
    checks.append({'check':'saved_feed', 'total':feed['total_all']})
    wait_for_photos()
    driver.execute_script('window.scrollTo(0,360)')
    driver.save_screenshot(str(out/'01-feed.png'))
    driver.find_element(By.CSS_SELECTOR, '[data-platform="drom"]').click()
    wait.until(lambda d: all(n.text in ('Дром', 'Drom') for n in d.find_elements(By.CSS_SELECTOR,'.platform-badge')))
    checks.append({'check':'source_filter', 'ok':True})
    Select(driver.find_element(By.ID,'sort')).select_by_value('cheap')
    wait.until(lambda d: api('/api/feed/?sort=cheap&platform=drom')['items'])
    prices = [n['price'] for n in api('/api/feed/?sort=cheap&platform=drom')['items']]
    assert prices == sorted(prices)
    checks.append({'check':'price_sort', 'ok':True})
    wait_for_photos()
    driver.find_element(By.ID,'open-advanced').click()
    wait.until(lambda d: d.find_element(By.ID,'advanced-filters').get_attribute('open') is not None)
    driver.save_screenshot(str(out/'02-filters.png'))
    checks.append({'check':'advanced_dialog', 'ok':True})
    driver.find_element(By.ID,'close-advanced').click()
    driver.find_element(By.CSS_SELECTOR,'[data-platform="all"]').click()
    link = api('/api/feed/')['items'][0]['detail_url']
    driver.get(args.url.rstrip('/') + link)
    wait.until(lambda d: d.find_elements(By.TAG_NAME,'h1'))
    assert driver.find_element(By.TAG_NAME,'h1').text
    if driver.find_elements(By.ID,'detail-photo'):
        wait.until(lambda d:d.find_element(By.ID,'detail-photo').get_property('naturalWidth')>0)
    driver.save_screenshot(str(out/'03-detail.png'))
    checks.append({'check':'detail_page', 'ok':True})
    driver.get(args.url)
    driver.set_window_size(390,1050)
    wait.until(lambda d: d.find_elements(By.CSS_SELECTOR,'.car-card'))
    assert driver.execute_script('return document.documentElement.scrollWidth <= window.innerWidth')
    driver.execute_script('document.getElementById("results-title").scrollIntoView({block:"start"})')
    if feed['items'][0]['photos']:
        wait.until(lambda d:d.find_element(By.CSS_SELECTOR,'.car-card img').get_property('naturalWidth')>0)
    driver.save_screenshot(str(out/'04-mobile.png'))
    checks.append({'check':'mobile_no_overflow', 'ok':True})
    errors=[row for row in driver.get_log('browser') if row['level']=='SEVERE' and 'favicon' not in row['message']]
    checks.append({'check':'browser_errors', 'count':len(errors)})
    assert not errors, errors
finally:
    driver.quit()
    (out/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(checks,ensure_ascii=False,indent=2))
