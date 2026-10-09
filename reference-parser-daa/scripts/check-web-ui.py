"""Browser E2E for local web UI; uses isolated Chrome, never Telegram credentials.
Run: docker compose exec -T web python scripts/check-web-ui.py
"""
import json
import time
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select

out=Path('/app/runtime/diagnostics')
out.mkdir(parents=True,exist_ok=True)
options=webdriver.ChromeOptions()
options.add_argument('--headless=new')
options.add_argument('--no-proxy-server')
options.add_argument('--window-size=1440,1150')
options.set_capability('goog:loggingPrefs',{'browser':'ALL'})
driver=webdriver.Remote('http://chrome_driver:4444/wd/hub',options=options)
wait=WebDriverWait(driver,40)
checks=[]

def node(id):return driver.find_element(By.ID,id)
def click(selector):driver.execute_script('arguments[0].click()',driver.find_element(By.CSS_SELECTOR,selector))
def enter(id,value):
    field=node(id);field.clear();field.send_keys(value)
def checked(field,value):return driver.find_element(By.CSS_SELECTOR,f'[data-filter="{field}"][value="{value}"]').is_selected()
def api(path):
    return driver.execute_async_script("const done=arguments[arguments.length-1];fetch(arguments[0]).then(r=>r.json()).then(done).catch(e=>done({error:e.message}));",path)
def current():
    # Use resource metadata for the current session-owned result endpoint only.
    path=driver.execute_script(r"return performance.getEntriesByType('resource').map(r=>r.name).filter(n=>/\/api\/searches\/[^/]+\//.test(n)).at(-1)")
    assert path,'Results endpoint was not requested'
    saved=driver.execute_script('return window.__lastWebResponse||null')
    return saved or api(path)
def submit():
    click('#advanced-submit')
    wait.until(lambda d:not node('advanced-filters').get_attribute('open'))
    wait.until(lambda d:node('filter-submit').is_enabled())
    return current()
def show_advanced():
    driver.execute_script('arguments[0].scrollIntoView({block:"center"})',node('open-advanced'))
    click('#open-advanced')
    wait.until(lambda d:node('advanced-filters').get_attribute('open') is not None)

try:
    driver.get('http://web:8000/')
    wait.until(EC.presence_of_element_located((By.ID,'query')))
    wait.until(lambda d:node('results-title').text=='Последние объявления' and len(d.find_elements(By.CSS_SELECTOR,'.car-card'))>0)
    assert node('make-id').get_attribute('value')==''
    assert 'сохранённые' in node('live-text').text
    checks.append('Initial database feed with empty filters')
    driver.save_screenshot(str(out/'web-initial-feed.png'))
    click('[data-platform=avito]')
    wait.until(lambda d:driver.execute_script("return [...document.querySelectorAll('.platform-badge')].every(n=>n.textContent==='Avito')"))
    click('[data-platform=all]')
    Select(node('sort')).select_by_value('cheap')
    wait.until(lambda d:len(d.find_elements(By.CSS_SELECTOR,'.car-card'))>0)
    Select(node('sort')).select_by_value('newest')
    click('#next-page')
    wait.until(lambda d:node('page-info').text.startswith('2 /'))
    click('#prev-page')
    wait.until(lambda d:node('page-info').text.startswith('1 /'))
    checks.append('Initial feed platform, sort and pagination')
    driver.execute_script("const orig=window.fetch;window.fetch=async(...args)=>{const r=await orig(...args);if(String(args[0]).includes('/api/searches/')&&r.ok&&!String(args[0]).endsWith('/refresh/'))window.__lastWebResponse=await r.clone().json();return r;}")
    enter('query','бмв x5 2015–2018 самара +200 до 4 млн')
    started=time.monotonic();click('#quick-submit')
    wait.until(lambda d:node('filter-submit').is_enabled() and node('make-id').get_attribute('value'))
    wait.until(lambda d:node('live-strip').is_displayed())
    first=time.monotonic()-started
    assert len(driver.find_elements(By.CSS_SELECTOR,'.car-card'))>0
    baseline=int(node('count-all').text)
    time.sleep(1)
    driver.save_screenshot(str(out/'web-results-desktop.png'))
    checks.append('Russian query, cached initial results and real cards')

    click('[data-view=list]');assert 'list-view' in node('results').get_attribute('class')
    click('[data-view=grid]');assert 'list-view' not in node('results').get_attribute('class')
    click('[data-platform=avito]')
    wait.until(lambda d:driver.execute_script("return [...document.querySelectorAll('.platform-badge')].every(n=>n.textContent==='Avito')"))
    click('[data-platform=all]')
    wait.until(lambda d:len(driver.find_elements(By.CSS_SELECTOR,'.car-card'))>1)
    Select(node('sort')).select_by_value('cheap')
    wait.until(lambda d:current().get('items',[{}])[0].get('price')==min(i['price'] for i in current()['items']))
    Select(node('sort')).select_by_value('newest')
    checks.append('Source tabs, list/cards and price sort')

    # Generations allow multiple selections and reset with a different make.
    click('#generation-details summary')
    wait.until(lambda d:len(driver.find_elements(By.CSS_SELECTOR,'#generations input'))>=2)
    click('#generations input:nth-of-type(1)')
    inputs=driver.find_elements(By.CSS_SELECTOR,'#generations input')
    driver.execute_script('arguments[0].click();arguments[1].click()',inputs[0],inputs[1]) if not inputs[0].is_selected() else driver.execute_script('arguments[0].click()',inputs[1])
    assert sum(i.is_selected() for i in inputs)==2
    for i in inputs:
        if i.is_selected():driver.execute_script('arguments[0].click()',i)
    checks.append('Multiple generation selection')

    show_advanced()
    assert len(driver.find_elements(By.CSS_SELECTOR,'.choice-field'))==9
    driver.save_screenshot(str(out/'web-advanced-desktop.png'))
    enter('owners-min','4');enter('owners-max','2');click('#advanced-submit')
    wait.until(lambda d:node('advanced-error').is_displayed())
    assert node('advanced-filters').get_attribute('open') is not None
    assert node('owners-max').get_attribute('class').find('invalid')>=0
    click('#clear-advanced')
    assert node('owners-min').get_attribute('value')==''
    checks.append('Validation stays in dialog and clear button resets fields')

    enter('mileage-min','100 000');enter('mileage-max','400.000')
    enter('owners-min','1');enter('owners-max','4')
    for field,values in {'engine':['дизель','бензин'],'gearbox':['автомат'],'drive':['полный'],
                         'body_type':['внедорожник'],'color':['черный'],'steering':['левый'],
                         'condition':['не битый'],'seller':['частное лицо']}.items():
        for value in values:click(f'[data-filter="{field}"][value="{value}"]')
    enter('color-custom','бордовый')
    # One source for this restrictive UI check, avoiding extra upstream collection.
    click('[name=source][value=avito]');click('[name=source][value=autoru]')
    data=submit();f=data['filters']
    assert f['mileage']=='100000-400000' and f['owners']=='1-4'
    assert set(f['engine'])=={'бензин','дизель'}
    assert set(f['color'])=={'черный','бордовый'}
    assert f['seller']==['частное лицо'] and f['platform']==['drom']
    checks.append('All eight option groups and numeric bounds survive submission')

    driver.refresh()
    wait.until(lambda d:node('owners-max').get_attribute('value')=='4')
    assert checked('engine','дизель') and checked('engine','бензин')
    assert node('color-custom').get_attribute('value')=='бордовый'
    assert not driver.find_element(By.CSS_SELECTOR,'[name=source][value=avito]').is_selected()
    assert int(node('advanced-count').text)>=10
    checks.append('Browser reload restores every filter and selected sources')

    driver.set_window_size(390,900)
    click('#toggle-filters');show_advanced()
    driver.save_screenshot(str(out/'web-advanced-mobile.png'))
    overflow=driver.execute_script('return document.documentElement.scrollWidth>innerWidth')
    assert not overflow
    assert driver.execute_script('const r=document.getElementById("advanced-submit").getBoundingClientRect();return r.bottom<=innerHeight&&r.top>=0')
    click('#clear-advanced')
    assert all(i.is_selected() for i in driver.find_elements(By.CSS_SELECTOR,'[name=source]'))
    assert not any(i.is_selected() for i in driver.find_elements(By.CSS_SELECTOR,'[data-filter]'))
    click('#close-advanced');click('#reset')
    assert node('make-id').get_attribute('value')==''
    assert node('mileage-min').get_attribute('value')==''
    assert not node('advanced-count').is_displayed()
    checks.append('Mobile dialog, visible footer, no overflow and full reset')

    driver.execute_script("const orig=window.fetch;window.fetch=async(...args)=>{const r=await orig(...args);if(String(args[0]).includes('/api/searches/')&&r.ok&&!String(args[0]).endsWith('/refresh/'))window.__lastWebResponse=await r.clone().json();return r;}")
    enter('query','бмв x5 2015–2018 самара +200 до 4 млн')
    click('#quick-submit');wait.until(lambda d:node('live-strip').is_displayed() and int(node('count-all').text)>=baseline)
    driver.execute_script('window.scrollTo(0,0)');time.sleep(1)
    driver.save_screenshot(str(out/'web-results-mobile.png'))
    errors=[r for r in driver.get_log('browser') if any(s in r.get('message','') for s in ('Uncaught','SyntaxError'))]
    assert not errors
    print(json.dumps({'checks':checks,'first_results_seconds':round(first,2),'javascript_errors':len(errors),'mobile_overflow':overflow},ensure_ascii=False),flush=True)
except Exception:
    driver.save_screenshot(str(out/'web-ui-failure.png'))
    raise
finally:
    driver.quit()
