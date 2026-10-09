"""Small editable demo sample; UI priorities, not a claim of measured sales ranks."""
import json
from uuid import uuid4
from django.utils import timezone
from core.paths import RUNTIME_DIR

POPULAR_MODELS={
    'bmw':('3-series','5-series','x5'),
    'audi':('a4','a6','q5'),
    'mercedes-benz':('e-class','c-class','s-class'),
    'toyota':('camry','corolla','rav4'),
    'volkswagen':('polo','tiguan','passat'),
    'lada':('granta','vesta','priora'),
    'kia':('rio','sportage','cee~d'),
    'hyundai':('solaris','creta','santa_fe'),
    'nissan':('qashqai','x-trail','almera'),
    'honda':('civic','accord','cr-v'),
    'lexus':('rx350','nx200','lx570','is250'),
    'skoda':('octavia','rapid','superb'),
    'volvo':('s60','xc60','xc90'),
    'haval':('jolion','f7','dargo'),
    'chery':('tiggo_7_pro','tiggo_4_pro','tiggo_8_pro'),
    'geely':('sx11','monjaro','atlas'),
}


def scope_path():return RUNTIME_DIR/'reports/audit-scope.json'


def read_scope():
    path=scope_path()
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def save_scope(value):
    path=scope_path();path.parent.mkdir(parents=True,exist_ok=True)
    value={**value,'selected_at':timezone.now().isoformat()}
    temp=path.with_name('audit-scope-'+uuid4().hex+'.json.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)