import time

active_searches=set()
last_click={}


def duplicate_click(user_id, value):
    previous=last_click.get(user_id)
    now=time.monotonic()
    if previous and previous[0]==value and now-previous[1]<3:
        return True
    last_click[user_id]=(value,now)
    return False
