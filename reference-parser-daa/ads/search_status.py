SOURCE_NAMES = {'avito':'Avito','autoru':'Auto.ru','drom':'Дром'}
ERROR_MESSAGES = {
    'filters':'Проверьте значения фильтров: год, цену и числовые диапазоны.',
    'blocked':'Площадка ограничила доступ. Повторим после паузы.',
    'browser_session':'Браузерная сессия истекла. Восстанавливаем подключение.',
    'network':'Не удалось загрузить страницу. Попробуйте повторить поиск позже.',
    'markup':'Не удалось прочитать объявления на странице. Нужна проверка разметки.',
}


def coverage_text(counts, source_state):
    lines=[]
    for source,count in counts.items():
        state=source_state.get(source,{})
        if state.get('status') == 'доступ ограничен':
            label='доступ ограничен' + (f' · в базе {count}' if count else '')
        elif state.get('status') in ('ошибка','восстановление браузера'):
            label='временно недоступно' + (f' · в базе {count}' if count else '')
        else:
            label=str(count)
        lines.append(f'{SOURCE_NAMES[source]}: {label}')
    return '\n'.join(lines)


def selected_sources(filters):
    selected=filters.get('platform') or list(SOURCE_NAMES)
    return list(SOURCE_NAMES) if 'unknown' in selected else [s for s in selected if s in SOURCE_NAMES]


def source_responded(state, token):
    if state.get('attempt_at') != token and state.get('cycle') != token and state.get('requested_at') != token:
        return False
    return bool(state.get('pages') or state.get('requested_at') == token or state.get('status') in ('ошибка','доступ ограничен'))


def status_text(job):
    lines=[]
    for source,name in SOURCE_NAMES.items():
        if source not in selected_sources(job.filters):
            lines.append(f'{name}: не выбран')
            continue
        value=job.source_state.get(source,{})
        status=value.get('status','ожидает проверки')
        error=ERROR_MESSAGES.get(value.get('error_code'),'')
        # Historical rows can contain Selenium internals; never send raw error strings.
        if status == 'ошибка' and not error: error='Не удалось завершить сбор. Запустите поиск повторно.'
        if status == 'доступ ограничен': error=ERROR_MESSAGES['blocked']
        line=f'{name}: {status} · сохранено {value.get("count",0) or 0}, страниц {value.get("pages",0) or 0}'
        if value.get('detail_queue'): line+=f'\nУточняем характеристики: осталось {len(value["detail_queue"])}.'
        if error: line+='\n'+error
        lines.append(line)
    return '\n\n'.join(lines)
