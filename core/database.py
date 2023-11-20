import asyncio
import json
import uuid
from datetime import datetime
from configparser import ConfigParser
from core.queries import get_last_balance_jumps, get_total_balance, get_last_launch, get_last_deals
from core.telegram import Telegram, TG_Groups
from core.enums import Context
# from queries import get_last_balance_jumps, get_total_balance, get_last_launch, get_last_deals
# from telegram import Telegram, TG_Groups
# from enums import Context
import requests
import asyncpg

config = ConfigParser()
config.read('config.ini', "utf-8")


class DB:
    def __init__(self, rabbit):
        self.telegram = Telegram()
        self.setts = config['SETTINGS']
        self.db = None

        self.loop = asyncio.new_event_loop()
        self.rabbit = rabbit

    async def setup_postgres(self) -> None:
        print(f"SETUP POSTGRES START")
        postgres = config['POSTGRES']
        try:
            with await asyncpg.create_pool(database=postgres['NAME'],
                                            user=postgres['USER'],
                                            password=postgres['PASSWORD'],
                                            host=postgres['HOST'],
                                            port=postgres['PORT']) as conn:
                self.db = conn
        except Exception as e:
            print(e)
        print(f"SETUP POSTGRES ENDED")
    def save_arbitrage_possibilities(self,_id, client_buy, client_sell, max_buy_vol, max_sell_vol, expect_buy_px,
                                     expect_sell_px, time_choose, shift, time_parser, symbol):
        expect_profit_usd = ((expect_sell_px - expect_buy_px) / expect_buy_px - (
                client_buy.taker_fee + client_sell.taker_fee)) * client_buy.amount
        expect_amount_usd = client_buy.amount * (expect_sell_px + expect_buy_px) / 2
        message = {
            'id': _id,
            'datetime': datetime.utcnow(),
            'ts': int(round(datetime.utcnow().timestamp())),
            'buy_exchange': client_buy.EXCHANGE_NAME,
            'sell_exchange': client_sell.EXCHANGE_NAME,
            'symbol': symbol,
            'buy_order_id': client_buy.LAST_ORDER_ID,
            'sell_order_id': client_sell.LAST_ORDER_ID,
            'max_buy_vol_usd': round(max_buy_vol * expect_buy_px),
            'max_sell_vol_usd': round(max_sell_vol * expect_sell_px),
            'expect_buy_price': expect_buy_px,
            'expect_sell_price': expect_sell_px,
            'expect_amount_usd': expect_amount_usd,
            'expect_amount_coin': client_buy.amount,
            'expect_profit_usd': expect_profit_usd,
            'expect_profit_relative': expect_profit_usd / expect_amount_usd,
            'expect_fee_buy': client_buy.taker_fee,
            'expect_fee_sell': client_sell.taker_fee,
            'time_parser': time_parser,
            'time_choose': time_choose,
            'chat_id': 12345678,
            'bot_token': 'placeholder',
            'status': 'Processing',
            'bot_launch_id': 12345678
        }
        self.rabbit.add_task_to_queue(message, "ARBITRAGE_POSSIBILITIES")

    def save_orders(self, client, side, parent_id, order_place_time, expect_price, symbol, env) -> None:
        order_id = uuid.uuid4()
        message = {
            'id': order_id,
            'datetime': datetime.utcnow(),
            'ts': int(round((datetime.utcnow().timestamp()) * 1000)),
            'context': 'bot',
            'parent_id': parent_id,
            'exchange_order_id': client.LAST_ORDER_ID,
            'type': 'GTT' if client.EXCHANGE_NAME == 'DYDX' else 'GTC',
            'status': 'Processing',
            'exchange': client.EXCHANGE_NAME,
            'side': side,
            'symbol': symbol.upper(),
            'expect_price': expect_price,
            'expect_amount_coin': client.amount,
            'expect_amount_usd': client.amount * client.price,
            'expect_fee': client.taker_fee,
            'factual_price': 0,
            'factual_amount_coin': 0,
            'factual_amount_usd': 0,
            'factual_fee': client.taker_fee,
            'order_place_time': order_place_time,
            'env': env,
        }

        self.rabbit.add_task_to_queue(message, "ORDERS")

        if client.LAST_ORDER_ID == 'default':
            self.telegram.send_message(self.telegram.order_error_message(env, symbol, client, order_id),
                                       TG_Groups.Alerts)

    # ex __check_start_launch_config
    # Смотрится есть ли в базе неиспользованные настройки, если есть используются они, если нет,
    # то берутся уже использованные и заносятся через вызов метода config_api.
    async def log_launch_config(self, multibot):
        async with self.db.acquire() as cursor:
            # Поиск, что есть подходящие еще не использованные настройки
            if not await get_last_launch(cursor,
                                         multibot.clients[0].EXCHANGE_NAME,
                                         multibot.clients[1].EXCHANGE_NAME,
                                         multibot.setts['COIN']):
                # Если таких нет, то поиск последней подходящей использованной настройки
                if launch := await get_last_launch(cursor,
                                                   multibot.clients[0].EXCHANGE_NAME,
                                                   multibot.clients[1].EXCHANGE_NAME,
                                                   multibot.setts['COIN'], 1):

                    launch = launch[0]
                    data = json.dumps({
                        "env": multibot.setts['ENV'],
                        "shift_use_flag": launch['shift_use_flag'],
                        "target_profit": launch['target_profit'],
                        "orders_delay": launch['orders_delay'],
                        "max_order_usd": launch['max_order_usd'],
                        "max_leverage": launch['max_leverage'],
                        'exchange_1': multibot.clients[0].EXCHANGE_NAME,
                        'exchange_2': multibot.clients[1].EXCHANGE_NAME,
                    })
                else:
                    data = multibot.base_launch_config
                headers = {
                    'token': 'jnfXhfuherfihvijnfjigt',
                    'context': 'bot-start'
                }
                url = f"http://{self.setts['CONFIG_API_HOST']}:{self.setts['CONFIG_API_PORT']}/api/v1/configs"

                requests.post(url=url, headers=headers, json=data)

    # раньше называлось start_db_update. В конце добавление в очередь.
    async def update_launch_config(self, multibot):
        async with self.db.acquire() as cursor:
            if launches := await get_last_launch(cursor,
                                                 multibot.clients[0].EXCHANGE_NAME,
                                                 multibot.clients[1].EXCHANGE_NAME,
                                                 multibot.setts['COIN']):
                launch = launches.pop(0)
                multibot.bot_launch_id = str(launch['id'])

                for field in launch:
                    if not launch.get('field') and field not in ['id', 'datetime', 'ts', 'bot_config_id',
                                                                 'coin', 'shift']:
                        launch[field] = multibot.base_launch_config[field]

                launch['launch_id'] = str(launch.pop('id'))
                launch['bot_config_id'] = str(launch['bot_config_id'])

                # if not launch.get('shift_use_flag'):
                #     for client_1, client_2 in self.ribs:
                #         self.shifts.update({f'{client_1.EXCHANGE_NAME} {client_2.EXCHANGE_NAME}': 0})
                # else:
                #     self.shifts = start_shifts
                message = "launch"
                self.rabbit.add_task_to_queue(message, "UPDATE_LAUNCH")
                try:
                    self.telegram.send_message('Launch Message',TG_Groups.DebugDima)
                except:
                    print('Label0, проблема с отправкой сообщения в телегу')
                for launch in launches:
                    launch['datetime_update'] = multibot.base_launch_config['datetime_update']
                    launch['ts_update'] = multibot.base_launch_config['ts_update']
                    launch['updated_flag'] = -1
                    launch['launch_id'] = str(launch.pop('id'))
                    launch['bot_config_id'] = str(launch['bot_config_id'])
                    message = "launch"
                    self.rabbit.add_task_to_queue(message, "UPDATE_LAUNCH")
                    try:
                        self.telegram.send_message('Launch Message' + str(launch), TG_Groups.DebugDima)
                    except:
                        print('Label1, проблема с отправкой сообщения в телегу')
                    self.update_balance_trigger('bot-config-update', multibot.bot_launch_id, multibot.env)

    def update_balance_trigger(self, context: str, parent_id, env: str):
        message = {
            'parent_id': parent_id,
            'context': context,
            'env': env,
            'chat_id': 12345678,
            'telegram_bot': 'placeholder',
        }
        self.rabbit.add_task_to_queue(message, "CHECK_BALANCE")

    # async def save_new_balance_jump(self):
    #     if self.start and self.finish:
    #         message = {
    #             'timestamp': int(round(datetime.utcnow().timestamp())),
    #             'total_balance': self.finish,
    #             'env': self.env
    #         },
    #         self.messaging.add_task_to_queue(message, "BALANCE_JUMP")
    #