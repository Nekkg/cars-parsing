"""Read-only diagnostic of current listing markup; never prints cookies or tokens."""
import json
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

options = webdriver.ChromeOptions()
options.add_argument('--headless=new')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--window-size=1440,1000')
driver = webdriver.Remote('http://chrome_driver:4444/wd/hub', options=options)
driver.set_page_load_timeout(45)
out = Path('diagnostics')
out.mkdir(exist_ok=True)
try:
    for source, url, selector in [
        ('avito', 'https://www.avito.ru/all/avtomobili?s=104', '[data-marker="item"]'),
        ('drom', 'https://auto.drom.ru/bmw/x5/', '[data-ftid="bulls-list_bull"]'),
    ]:
        try:
            driver.get(url)
            try:
                WebDriverWait(driver, 15).until(lambda d: d.find_elements(By.CSS_SELECTOR, selector))
            except Exception:
                pass
            cards = driver.find_elements(By.CSS_SELECTOR, selector)
            (out / f'{source}.html').write_text(driver.page_source, encoding='utf-8')
            print(json.dumps({'source': source, 'title': driver.title, 'cards': len(cards),
                  'first': cards[0].get_attribute('outerHTML')[:13000] if cards else driver.find_element(By.TAG_NAME, 'body').text[:1300]}, ensure_ascii=False))
        except Exception as exc:
            print(source, type(exc).__name__)
finally:
    driver.quit()
