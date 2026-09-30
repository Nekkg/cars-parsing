import asyncio
import os
from urllib.error import HTTPError
from urllib.request import Request
from dotenv import load_dotenv
load_dotenv()
from core.network import telegram_session, source_opener
from aiogram import Bot


async def main():
    try:
        async with Bot(os.environ['TELEGRAM_BOT_TOKEN'],session=telegram_session()) as bot:
            me = await bot.get_me()
            print(f'Telegram configured route: OK (@{me.username})')
    except Exception as error:
        print('Telegram configured route:',type(error).__name__)
    for source,url in [('drom','https://auto.drom.ru/bmw/x5/'),('autoru','https://auto.ru/rossiya/cars/bmw/x5/all/'),('avito','https://www.avito.ru/all/avtomobili?q=BMW+X5')]:
        try:
            with source_opener().open(Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=15) as response:
                print(source,'HTTP',response.status,'(HTTP success alone does not verify listing extraction)')
        except HTTPError as error:
            print(source,'HTTP',error.code)
        except Exception as error:
            print(source,type(error).__name__)


if __name__ == '__main__':
    asyncio.run(main())
