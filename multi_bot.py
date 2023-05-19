import argparse
import asyncio
import datetime
import logging
import time
import traceback
import uuid
from logging.config import dictConfig

import aiohttp
import asyncpg
import orjson
from aio_pika import Message, ExchangeType, connect_robust

from clients.apollox import ApolloxClient
from clients.binance import BinanceClient
from clients.bitmex import BitmexClient
from clients.dydx import DydxClient
from clients.kraken import KrakenClient
from clients.okx import OkxClient
from config import Config
from core.enums import BotState, RabbitMqQueues
from core.queries import get_last_balance_jumps, get_total_balance
from tools.shifts import Shifts

dictConfig(Config.LOGGING)
logger = logging.getLogger(__name__)

CLIENTS_WITH_CONFIGS = {
    'BITMEX': [BitmexClient, Config.BITMEX, Config.LEVERAGE],
    'DYDX': [DydxClient, Config.DYDX, Config.LEVERAGE],
    'BINANCE': [BinanceClient, Config.BINANCE, Config.LEVERAGE],
    'APOLLOX': [ApolloxClient, Config.APOLLOX, Config.LEVERAGE],
    'OKX': [OkxClient, Config.OKX, Config.LEVERAGE],
    'KRAKEN': [KrakenClient, Config.KRAKEN, Config.LEVERAGE]
}


class MultiBot:
    __slots__ = ['rabbit_url', 'deal_pause', 'max_order_size', 'profit_taker', 'shifts', 'telegram_bot', 'chat_id',
                 'daily_chat_id', 'inv_chat_id', 'state', 'loop', 'client_1', 'client_2', 'start_time', 'last_message',
                 'last_max_deal_size', 'potential_deals', 'deals_counter', 'deals_executed', 'available_balances',
                 'session', 'clients', 'exchanges', 'mq', 'min_disbalance', 'ribs', 'env', 'exchanges_len', 'db',
                 'start', 'finish', 's_time', 'f_time']

    def __init__(self, client_1: str, client_2: str):
        self.start = None
        self.finish = None
        self.db = None
        self.mq = None
        self.rabbit_url = f"amqp://{Config.RABBIT['username']}:{Config.RABBIT['password']}@{Config.RABBIT['host']}:{Config.RABBIT['port']}/"

        self.env = Config.ENV

        self.s_time = ''
        self.f_time = ''

        # ORDER CONFIGS
        self.deal_pause = Config.DEALS_PAUSE
        self.max_order_size = Config.ORDER_SIZE
        self.profit_taker = Config.TARGET_PROFIT
        self.shifts = {'TAKER': Config.LIMIT_SHIFTS}

        # TELEGRAM
        self.telegram_bot = Config.TELEGRAM_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.daily_chat_id = Config.TELEGRAM_DAILY_CHAT_ID
        self.inv_chat_id = Config.TELEGRAM_INV_CHAT_ID

        self.state = Config.STATE
        self.loop = None
        self.exchanges_len = len(Config.EXCHANGES)

        # CLIENTS
        client_1 = CLIENTS_WITH_CONFIGS[client_1.upper()]
        client_2 = CLIENTS_WITH_CONFIGS[client_2.upper()]

        self.client_1 = client_1[0](client_1[1], client_1[2])
        self.client_2 = client_2[0](client_2[1], client_2[2])
        self.clients = [self.client_1, self.client_2]

        self.exchanges = [x.EXCHANGE_NAME for x in self.clients]
        self.ribs = [self.clients, list(reversed(self.clients))]

        self.start_time = int(round(time.time()))
        self.last_message = None
        self.last_max_deal_size = 0
        self.potential_deals = []
        self.deals_counter = []
        self.deals_executed = []
        self.available_balances = {'+DYDX-OKEX': 0}
        self.min_disbalance = 100
        self.session = None

        for client in self.clients:
            client.run_updater()

        time.sleep(10)
        self.get_sizes()

    @staticmethod
    def day_deals_count(base_data):
        timestamp = int(round(time.time() - 86400))
        data = {'deal_count': 0,
                'volume': 0,
                'theory_profit': 0,
                }
        for deal in base_data[::-1]:
            if deal[1] < timestamp:
                break
            data['deal_count'] += 1
            data['volume'] += deal[8]
            data['theory_profit'] += deal[10]
            if not data.get(deal[2] + 'SELL'):
                data.update({deal[2] + 'SELL': 1})
            else:
                data[deal[2] + 'SELL'] += 1
            if not data.get(deal[3] + 'BUY'):
                data.update({deal[3] + 'BUY': 1})
            else:
                data[deal[3] + 'BUY'] += 1
        return data

    def __prepare_shifts(self):
        time.sleep(10)
        self.__rates_update()

        for x, y in Shifts().get_shifts().items():
            self.shifts.update({x: y})

    def find_position_gap(self):
        position_gap = 0

        for client in self.clients:
            if res := client.get_positions().get(client.symbol):
                position_gap += res['amount']

        return position_gap

    def find_balancing_elements(self):
        position_gap = self.find_position_gap()
        amount_to_balancing = abs(position_gap) / len(self.clients)
        return position_gap, amount_to_balancing

    async def balancing_bd_update(self, exchanges, client, position_gap, price, side, taker_fee):
        coin = client.symbol.split('USD')[0].replace('-', '').replace('/', '')
        size_usd = abs(round(position_gap * price, 2))
        to_base = {
            'timestamp': int(round(time.time() * 1000)),
            'exchange_name': exchanges,
            'side': side,
            'price': price,
            'taker_fee': taker_fee,
            'position_gap': position_gap,
            'size_usd': size_usd,
            'coin': coin,
            'env': self.env,
            'chat_id': Config.TELEGRAM_CHAT_ID,
            'bot_token': Config.TELEGRAM_TOKEN
        }

        await self.publish_message(connect=self.mq,
                                   message=to_base,
                                   routing_key=RabbitMqQueues.BALANCING_REPORTS,
                                   exchange_name=RabbitMqQueues.get_exchange_name(RabbitMqQueues.BALANCING_REPORTS),
                                   queue_name=RabbitMqQueues.BALANCING_REPORTS
                                   )

    def available_balance_update(self, client_buy, client_sell):
        max_deal_size = self.avail_balance_define(client_buy, client_sell)
        self.available_balances.update({f"+{client_buy.EXCHANGE_NAME}-{client_sell.EXCHANGE_NAME}": max_deal_size})

    async def cycle_parser(self):
        for client_buy, client_sell in self.ribs:
            self.available_balance_update(client_buy, client_sell)
            orderbook_sell, orderbook_buy = self.get_orderbooks(client_sell, client_buy)
            sell_price = orderbook_sell['bids'][0][0]  # * (1 + shift)
            buy_price = orderbook_buy['asks'][0][0]  # * (1 - shift)
            if sell_price > buy_price:
                self.taker_order_profit(client_sell, client_buy, sell_price, buy_price)

            await self.potential_real_deals(client_sell, client_buy, orderbook_buy, orderbook_sell)

    async def find_price_diffs(self):
        time_start = time.time()
        await self.cycle_parser()
        time_parser = time.time() - time_start
        chosen_deal = None

        if len(self.potential_deals):
            chosen_deal = self.choose_deal()

        if self.state == BotState.BOT:
            position_gap, amount_to_balancing = self.find_balancing_elements()
            if chosen_deal and amount_to_balancing < self.max_order_size:  # todo REFACTOR THIS
                time_choose = time.time() - time_start - time_parser
                await self.execute_deal(chosen_deal['buy_exch'],
                                        chosen_deal['sell_exch'],
                                        chosen_deal['orderbook_buy'],
                                        time_start,
                                        time_parser,
                                        time_choose)

    def choose_deal(self):
        max_profit = 0
        chosen_deal = None

        for deal in self.potential_deals:
            self.deals_counter.append({'buy_exch': deal['buy_exch'],
                                       "sell_exch": deal['sell_exch'],
                                       "profit": deal['profit']})

            if deal['profit'] > max_profit:
                if self.available_balances[
                    f"+{deal['buy_exch'].EXCHANGE_NAME}-{deal['sell_exch'].EXCHANGE_NAME}"] >= self.max_order_size:
                    if deal['buy_exch'].EXCHANGE_NAME in self.exchanges or deal[
                        'sell_exch'].EXCHANGE_NAME in self.exchanges:
                        max_profit = deal['profit']
                        chosen_deal = deal

        self.potential_deals = []
        return chosen_deal

    def taker_order_profit(self, client_sell, client_buy, sell_price, buy_price):
        orderbook_sell, orderbook_buy = self.get_orderbooks(client_sell, client_buy)
        profit = (sell_price - buy_price) / buy_price

        if profit > self.profit_taker + client_sell.taker_fee + client_buy.taker_fee:
            self.potential_deals.append({'buy_exch': client_buy,
                                         "sell_exch": client_sell,
                                         "orderbook_buy": orderbook_buy,
                                         "orderbook_sell": orderbook_sell,
                                         'max_deal_size': self.available_balances[
                                             f"+{client_buy.EXCHANGE_NAME}-{client_sell.EXCHANGE_NAME}"],
                                         "profit": profit})

    async def execute_deal(self, client_buy, client_sell, orderbook_buy, time_start, time_parser, time_choose):
        max_deal_size = self.available_balances[f"+{client_buy.EXCHANGE_NAME}-{client_sell.EXCHANGE_NAME}"]
        self.deals_executed.append([f'+{client_buy.EXCHANGE_NAME}-{client_sell.EXCHANGE_NAME}', max_deal_size])
        max_deal_size = max_deal_size / ((orderbook_buy['asks'][1][0] + orderbook_buy['bids'][1][0]) / 2)
        await self.create_orders(client_buy, client_sell, max_deal_size, time_start, time_parser, time_choose)

    async def create_orders(self, client_buy, client_sell, max_deal_size, time_start, time_parser, time_choose):
        orderbook_sell, orderbook_buy = self.get_orderbooks(client_sell, client_buy)
        expect_buy_px = orderbook_buy['asks'][1][0]
        expect_sell_px = orderbook_sell['bids'][1][0]
        price_buy = orderbook_buy['asks'][1][0]
        max_buy_vol = orderbook_buy['asks'][1][1]
        price_sell = orderbook_sell['bids'][1][0]
        max_sell_vol = orderbook_sell['bids'][1][1]
        price_buy_limit_taker = price_buy * self.shifts['TAKER']
        price_sell_limit_taker = price_sell / self.shifts['TAKER']
        timer = time.time() * 1000
        arbitrage_possibilities_id = uuid.uuid4()
        balance_buy_id = uuid.uuid4()
        balance_sell_id = uuid.uuid4()
        balance_message_buy = {
            'id': balance_buy_id,
            'context': 'pre-deal',
            'parent_id': arbitrage_possibilities_id,
            'client': client_buy,
            'exchange_balance': client_buy.get_real_balance(),
            'exchange_available_for_buy': client_buy.get_available_balance('buy'),
            'exchange_available_for_sell': client_buy.get_available_balance('sell'),
            'available_for_buy': client_buy.get_real_balance() * 10 - sum(
                [x.get('amount_usd', 0) for _, x in client_buy.get_positions().items()]),
            'available_for_sell': client_buy.get_real_balance() * 10 + sum(
                [x.get('amount_usd', 0) for _, x in client_buy.get_positions().items()])
        }

        balance_message_sell = {
            'id': balance_sell_id,
            'context': 'pre-deal',
            'parent_id': arbitrage_possibilities_id,
            'client': client_sell,
            'exchange_balance': client_sell.get_real_balance(),
            'exchange_available_for_buy': client_sell.get_available_balance('buy'),
            'exchange_available_for_sell': client_sell.get_available_balance('sell'),
            'available_for_buy': client_sell.get_real_balance() * 10 - sum(
                [x.get('amount_usd', 0) for _, x in client_sell.get_positions().items()]),
            'available_for_sell': client_sell.get_real_balance() * 10 + sum(
                [x.get('amount_usd', 0) for _, x in client_sell.get_positions().items()])
        }

        print('CREATE ORDER', max_deal_size, price_buy_limit_taker)

        responses = await asyncio.gather(*[
            self.loop.create_task(
                client_buy.create_order(max_deal_size, price_buy_limit_taker, 'buy', self.session)),
            self.loop.create_task(
                client_sell.create_order(max_deal_size, price_sell_limit_taker, 'sell', self.session))
        ], return_exceptions=True)
        print(f"FULL POOL ADDING AND CALLING TIME: {time.time() * 1000 - timer}")

        deal_time = time.time() - time_start - time_parser - time_choose
        for response in responses:
            try:
                await self.save_order_timestamps(response['exchange_name'], deal_time, response['timestamp'],
                                                 time.time() * 1000, response['status'])

            except Exception:
                traceback.print_exc()

        await self.deal_details(client_buy, client_sell, expect_buy_px, expect_sell_px, max_deal_size, deal_time,
                                time_parser, time_choose)

        await asyncio.sleep(3)
        await self.balance_message(client_buy)
        await self.balance_message(client_sell)

        await self.save_orders(client_buy, price_buy_limit_taker, 'buy', arbitrage_possibilities_id, max_deal_size,
                               deal_time)
        await self.save_orders(client_sell, price_sell_limit_taker, 'sell', arbitrage_possibilities_id, max_deal_size,
                               deal_time)

        await self.save_arbitrage_possibilities(arbitrage_possibilities_id, client_buy, client_sell, max_buy_vol,
                                                max_sell_vol, expect_buy_px, expect_sell_px, max_deal_size, time_parser,
                                                time_choose)

        time.sleep(10)
        await self.save_balance(**balance_message_buy)
        await self.save_balance_detalization(balance_buy_id, client_buy, 'buy')
        await self.save_balance(**balance_message_sell)
        await self.save_balance_detalization(balance_sell_id, client_sell, 'sell')

    async def save_arbitrage_possibilities(self, _id, client_buy, client_sell, max_buy_vol, max_sell_vol, expect_buy_px,
                                           expect_sell_px, expect_amount_coin, time_parser, time_choose):
        expect_profit_usd = (expect_sell_px - expect_buy_px) * expect_amount_coin - (
                    client_buy.taker_fee + client_sell.taker_fee)
        message = {
            'id': _id,
            'datetime': datetime.datetime.utcnow(),
            'ts': time.time(),
            'buy_exchange': client_buy.EXCHANGE_NAME,
            'sell_exchange': client_sell.EXCHANGE_NAME,
            'symbol': client_buy.symbol,
            'buy_order_id': client_buy.LAST_ORDER_ID,
            'sell_order_id': client_sell.LAST_ORDER_ID,
            'max_buy_vol_usd': max_buy_vol * expect_buy_px,
            'max_sell_vol_usd': max_sell_vol * expect_sell_px,
            'expect_buy_price': expect_buy_px,
            'expect_sell_price': expect_sell_px,
            'expect_amount_usd': expect_amount_coin * (expect_sell_px + expect_buy_px) / 2,
            'expect_amount_coin': expect_amount_coin,
            'expect_profit_usd': expect_profit_usd,
            'expect_profit_relative': expect_profit_usd / expect_amount_coin,
            'expect_fee_buy': client_buy.taker_fee,
            'expect_fee_sell': client_sell.taker_fee,
            'time_parser': time_parser,
            'time_choose': time_choose,
            'chat_id': self.chat_id,
            'bot_token': self.telegram_bot
        }

        await self.publish_message(connect=self.mq,
                                   message=message,
                                   routing_key=RabbitMqQueues.ARBITRAGE_POSSIBILITIES,
                                   exchange_name=RabbitMqQueues.get_exchange_name(
                                       RabbitMqQueues.ARBITRAGE_POSSIBILITIES),
                                   queue_name=RabbitMqQueues.ARBITRAGE_POSSIBILITIES
                                   )

    async def save_orders(self, client, expect_price, side, parent_id, expect_amount_coin, order_place_time) -> None:
        message = {
            'id': uuid.uuid4(),
            'datetime': datetime.datetime.utcnow(),
            'ts': time.time(),
            'context': 'bot',
            'parent_id': parent_id,
            'exchange_order_id': client.LAST_ORDER_ID,
            'type': 'GTT' if client.EXCHANGE_NAME == 'DYDX' else 'GTC',
            'status': 'Processing',
            'exchange': client.EXCHANGE_NAME,
            'side': side,
            'symbol': client.symbol,
            'expect_price': expect_price,
            'expect_amount_coin': expect_amount_coin,
            'expect_amount_usd': expect_amount_coin * expect_price,
            'expect_fee': client.taker_fee,
            'factual_price': 0,
            'factual_amount_coin': 0,
            'factual_amount_usd': 0,
            'factual_fee': client.taker_fee,
            'order_place_time': order_place_time,
            'env': self.env,
        }

        await self.publish_message(connect=self.mq,
                                   message=message,
                                   routing_key=RabbitMqQueues.ORDERS,
                                   exchange_name=RabbitMqQueues.get_exchange_name(RabbitMqQueues.ORDERS),
                                   queue_name=RabbitMqQueues.ORDERS
                                   )

    async def save_balance(self, id, context, parent_id, client, exchange_balance, exchange_available_for_buy,
                           exchange_available_for_sell, available_for_buy, available_for_sell):
        message = {
            'id': id,
            'datetime': datetime.datetime.utcnow(),
            'ts': time.time(),
            'context': context,
            'parent_id': parent_id,
            'exchange': client.EXCHANGE_NAME,
            'exchange_balance': exchange_balance,
            'exchange_available_for_buy': exchange_available_for_buy,
            'exchange_available_for_sell': exchange_available_for_sell,
            'available_for_buy': available_for_buy,
            'available_for_sell': available_for_sell,
            'env': self.env,
            'chat_id': self.chat_id,
            'bot_token': self.telegram_bot,
        }

        await self.publish_message(connect=self.mq,
                                   message=message,
                                   routing_key=RabbitMqQueues.BALANCES,
                                   exchange_name=RabbitMqQueues.get_exchange_name(RabbitMqQueues.BALANCES),
                                   queue_name=RabbitMqQueues.BALANCES
                                   )

    async def save_balance_detalization(self, parent_id, client, side):
        client_position_by_symbol = client.get_positions()[client.symbol]
        print(client_position_by_symbol) # todo
        message = {
            'id': uuid.uuid4(),
            'datetime': datetime.datetime.utcnow(),
            'ts': time.time(),
            'context': 'balance',
            'parent_id': parent_id,
            'exchange': client.EXCHANGE_NAME,
            'side': side,
            'symbol': client.symbol,
            'max_margin': client.leverage,
            'current_margin': client.get_real_balance() * client.leverage / sum(
                [x.get('amount_usd', 0) for _, x in client.get_positions().items()]),
            'position_coin': client_position_by_symbol['amount'],
            'position_usd': client_position_by_symbol['amount'],
            'entry_price': client_position_by_symbol['entry_price'],
            'mark_price': (client.get_orderbook()[client.symbol]['asks'][0][0] +
                           client.get_orderbook()[client.symbol]['bids'][0][0]) / 2
        }
        await self.publish_message(connect=self.mq,
                                   message=message,
                                   routing_key=RabbitMqQueues.BALANCE_DETALIZATION,
                                   exchange_name=RabbitMqQueues.get_exchange_name(RabbitMqQueues.BALANCE_DETALIZATION),
                                   queue_name=RabbitMqQueues.BALANCE_DETALIZATION
                                   )

    async def deal_details(self, client_buy, client_sell, expect_buy_px, expect_sell_px, deal_size, deal_time,
                           time_parser, time_choose):
        orderbook_sell, orderbook_buy = self.get_orderbooks(client_sell, client_buy)
        time.sleep(self.deal_pause)
        await self.send_data_for_base(client_buy,
                                      client_sell,
                                      expect_buy_px,
                                      expect_sell_px,
                                      deal_size,
                                      orderbook_sell['asks'][1][0],
                                      orderbook_buy['bids'][1][0],
                                      deal_time,
                                      time_parser,
                                      time_choose
                                      )

    async def publish_message(self, connect, message, routing_key, exchange_name, queue_name):
        try:
            channel = await connect.channel()
            exchange = await channel.declare_exchange(exchange_name, type=ExchangeType.DIRECT, durable=True)
            queue = await channel.declare_queue(queue_name, durable=True)
            await queue.bind(exchange, routing_key=routing_key)
            message_body = orjson.dumps(message)
            message = Message(message_body)
            await exchange.publish(message, routing_key=routing_key)
            await channel.close()
            return True

        except Exception as e:
            traceback.print_exc()

            if 'RuntimeError' in str(e):
                print(f"RABBIT MQ RESTART")
                await self.setup_mq(self.loop)

    async def balance_message(self, client):
        orderbook = client.get_orderbook()[client.symbol]
        to_base = {
            'timestamp': int(round(time.time() * 1000)),
            'exchange_name': client.EXCHANGE_NAME,
            # 'side': 'sell' if position <= 0 else 'long',
            'total_balance': round(client.get_real_balance()),
            'position': round(client.get_positions()[client.symbol].get('amount', 0), 4),
            'available_for_buy': round(client.get_available_balance('buy')),
            'available_for_sell': round(client.get_available_balance('sell')),
            'ask': orderbook['asks'][1][0],
            'bid': orderbook['bids'][1][0],
            'symbol': client.symbol,
            'env': self.env,
            'chat_id': Config.TELEGRAM_CHAT_ID,
            'bot_token': Config.TELEGRAM_TOKEN
        }

        await self.publish_message(connect=self.mq,
                                   message=to_base,
                                   routing_key=RabbitMqQueues.BALANCE_CHECK,
                                   exchange_name=RabbitMqQueues.get_exchange_name(RabbitMqQueues.BALANCE_CHECK),
                                   queue_name=RabbitMqQueues.BALANCE_CHECK
                                   )

    async def send_data_for_base(self, client_buy, client_sell, expect_buy_px, expect_sell_px, deal_size, sell_ob_ask,
                                 buy_ob_bid, deal_time, time_parser, time_choose):
        price_buy = client_buy.get_last_price('buy')
        price_sell = client_sell.get_last_price('sell')
        orderbook = client_buy.get_orderbook()[client_buy.symbol]
        change = ((orderbook['asks'][1][0] + orderbook['bids'][1][0]) / 2)

        if price_buy and price_sell:
            real_profit = (price_sell - price_buy) / price_buy
            real_profit = real_profit - self.client_1.taker_fee + self.client_2.taker_fee
            real_profit_usd = real_profit * deal_size * change
        else:
            real_profit = 0
            real_profit_usd = 0

        if client_buy.get_positions()[client_buy.symbol].get('side') == 'LONG':
            long = client_buy.EXCHANGE_NAME
        else:
            long = client_sell.EXCHANGE_NAME

        to_base = {
            'timestamp': int(round(time.time() * 1000)),
            'sell_exch': client_sell.EXCHANGE_NAME,
            'buy_exch': client_buy.EXCHANGE_NAME,
            'sell_order_id': str(client_sell.LAST_ORDER_ID),
            'buy_order_id': str(client_buy.LAST_ORDER_ID),
            'sell_px': price_sell,
            'expect_sell_px': expect_sell_px,
            'buy_px': price_buy,
            'expect_buy_px': expect_buy_px,
            'amount_USD': deal_size * change,
            'amount_coin': deal_size,
            'profit_USD': real_profit_usd,
            'profit_relative': real_profit,
            'fee_sell': client_sell.taker_fee,
            'fee_buy': client_buy.taker_fee,
            'long_side': long,
            'sell_ob_ask': sell_ob_ask,
            'buy_ob_bid': buy_ob_bid,
            'deal_time': deal_time,
            'time_parser': time_parser,
            'time_choose': time_choose,
            'env': self.env,
            'coin': client_sell.symbol,
            'date_utc': str(datetime.datetime.utcnow()),
            'chat_id': Config.TELEGRAM_CHAT_ID,
            'bot_token': Config.TELEGRAM_TOKEN
        }
        await self.publish_message(connect=self.mq,
                                   message=to_base,
                                   routing_key=RabbitMqQueues.DEALS_REPORT,
                                   exchange_name=RabbitMqQueues.get_exchange_name(RabbitMqQueues.DEALS_REPORT),
                                   queue_name=RabbitMqQueues.DEALS_REPORT
                                   )

    def avail_balance_define(self, client_buy, client_sell):
        return min(client_buy.get_available_balance('buy'), client_sell.get_available_balance('sell'),
                   self.max_order_size)

    def __rates_update(self):
        message = ''
        for client in self.clients:
            message += f"{client.EXCHANGE_NAME} | {client.get_orderbook()[client.symbol]['asks'][1][0]}\n"

        with open('rates.txt', 'a') as file:
            file.write(message + '\n')

    def _update_log(self, sell_exch, buy_exch, orderbook_buy, orderbook_sell):
        message = f"{buy_exch} BUY: {orderbook_buy['asks'][1]}\n"
        message += f"{sell_exch} SELL: {orderbook_sell['bids'][1]}\n"
        shift = self.shifts[sell_exch + ' ' + buy_exch] / 2
        message += f"Shifts: {sell_exch}={shift}, {buy_exch}={-shift}\n"
        message += f"Max deal size: {self.available_balances[f'+{buy_exch}-{sell_exch}']} USD\n"
        message += f"Datetime: {datetime.datetime.now()}\n\n"

        if message != self.last_message:
            with open('arbi.txt', 'a') as file:  # TODO send to DB, not txt
                file.write(message)
                self.last_message = message

    @staticmethod
    def get_orderbooks(client_sell, client_buy):
        time_start = time.time()
        while True:
            try:
                orderbook_sell = client_sell.get_orderbook()[client_sell.symbol]
                orderbook_buy = client_buy.get_orderbook()[client_buy.symbol]
                if orderbook_sell['timestamp'] > 10 * orderbook_buy['timestamp']:
                    orderbook_sell['timestamp'] = orderbook_sell['timestamp'] / 1000
                elif orderbook_buy['timestamp'] > 10 * orderbook_sell['timestamp']:
                    orderbook_buy['timestamp'] = orderbook_buy['timestamp'] / 1000
                func_time = time.time() - time_start
                if func_time > 0.001:
                    print(f"GET ORDERBOOKS FUNC TIME: {func_time} sec")
                return orderbook_sell, orderbook_buy
            except Exception as e:
                print(f"Exception with orderbooks: {e}")

    async def save_order_timestamps(self, exchange_name: str, ts_of_request: float, ts_from_response: float,
                                    ts_received_response: float, status: str) -> None:
        """
        Prepare and send data to rabbitmq
        :param exchange_name:
        :param ts_of_request:
        :param ts_from_response:
        :param ts_received_response:
        :param status:
        :return:
        """
        data = {
            "server_name": self.env,
            "exchange_name": exchange_name,
            "status_of_ping": status,
            "ts_of_request": ts_of_request,
            "ts_from_response": ts_from_response,
            "ts_received_response": ts_received_response,
            'chat_id': Config.TELEGRAM_CHAT_ID,
            'bot_token': Config.TELEGRAM_TOKEN
        }

        await self.publish_message(connect=self.mq,
                                   message=data,
                                   routing_key=RabbitMqQueues.PING,
                                   exchange_name=RabbitMqQueues.get_exchange_name(RabbitMqQueues.PING),
                                   queue_name=RabbitMqQueues.PING
                                   )

    async def start_message(self):
        coin = self.client_1.symbol.split('USD')[0].replace('-', '').replace('/', '')
        message = f'MULTIBOT STARTED\n{self.client_1.EXCHANGE_NAME} | {self.client_2.EXCHANGE_NAME}\n'
        message += f"COIN: {coin}\n"
        message += f"ENV: {self.env}\n"
        message += f"STATE: {Config.STATE}\n"
        message += f"LEVERAGE: {Config.LEVERAGE}\n"
        message += f"EXCHANGES: {self.client_1.EXCHANGE_NAME} {self.client_2.EXCHANGE_NAME}\n"
        message += f"DEALS_PAUSE: {Config.DEALS_PAUSE}\n"
        message += f"ORDER_SIZE: {Config.ORDER_SIZE}\n"
        message += f"TARGET_PROFIT: {Config.TARGET_PROFIT}\n"
        message += f"START BALANCE: {self.start}\n"
        message += f"CURRENT BALANCE: {self.finish}\n"

        for exchange, shift in self.shifts.items():
            message += f"{exchange}: {round(shift, 6)}\n"

        await self.send_message(message, Config.TELEGRAM_CHAT_ID, Config.TELEGRAM_TOKEN)

    async def time_based_messages(self):
        time_from = (int(round(time.time())) - 10 - self.start_time) % 180
        if not time_from:
            if self.state == BotState.BOT:
                print(f"STARTED POSITION BALANCING")
                await self.position_balancing()  # no need

            self.start_time -= 1

    def create_result_message(self, deals_potential: dict, deals_executed: dict, time: int) -> str:
        message = f"For last 3 min\n"
        message += f'ENV: {Config.ENV}\n'

        if self.__check_env():
            message += f'SYMBOL: {self.client_1.symbol}'

        message += f"\n\nPotential deals:"
        for side, values in deals_potential.items():
            message += f"\n   {side}:"
            for exchange, deals in values.items():
                message += f"\n{exchange}: {deals}"
        message += f"\n\nExecuted deals:"
        for side, values in deals_executed.items():
            message += f"\n   {side}:"
            for exchange, deals in values.items():
                message += f"\n{exchange}: {deals}"
        return message

    async def potential_real_deals(self, sell_client, buy_client, orderbook_buy, orderbook_sell):
        if not (int(round(time.time())) - self.start_time) % 15:
            deals_potential = {'SELL': {x: 0 for x in self.exchanges}, 'BUY': {x: 0 for x in self.exchanges}}
            deals_executed = {'SELL': {x: 0 for x in self.exchanges}, 'BUY': {x: 0 for x in self.exchanges}}

            deals_potential['SELL'][sell_client.EXCHANGE_NAME] += len(self.deals_counter)
            deals_potential['BUY'][buy_client.EXCHANGE_NAME] += len(self.deals_counter)

            deals_executed['SELL'][sell_client.EXCHANGE_NAME] += len(self.deals_executed)
            deals_executed['BUY'][buy_client.EXCHANGE_NAME] += len(self.deals_executed)

            self.__rates_update()
            self._update_log(sell_client.EXCHANGE_NAME, buy_client.EXCHANGE_NAME, orderbook_buy, orderbook_sell)

            if not (int(round(time.time())) - self.start_time) % 600:
                message = self.create_result_message(deals_potential, deals_executed, 600)
                await self.send_message(message, Config.TELEGRAM_CHAT_ID, Config.TELEGRAM_TOKEN)
                self.deals_counter = []
                self.deals_executed = []

            self.start_time -= 1

    async def send_message(self, message: str, chat_id: int, bot_token: str) -> None:
        await self.publish_message(connect=self.mq,
                                   message={"chat_id": chat_id, "msg": message, 'bot_token': bot_token},
                                   routing_key=RabbitMqQueues.TELEGRAM,
                                   exchange_name=RabbitMqQueues.get_exchange_name(RabbitMqQueues.TELEGRAM),
                                   queue_name=RabbitMqQueues.TELEGRAM
                                   )

    async def create_balancing_order(self, client, position_gap, price, side):
        time_start = time.time()
        response = await client.create_order(abs(position_gap), price, side, self.session)
        deal_time = time.time() - time_start

        await self.save_order_timestamps(response['exchange_name'], deal_time, response['timestamp'],
                                         time.time() * 1000, response['status'])

        print('CREATE BALANCING ORDER:', f'{position_gap} {price} {side}', client.EXCHANGE_NAME, response)

        await asyncio.sleep(3)
        await self.balance_message(client)

    async def position_balancing(self):
        position_gap, amount_to_balancing = self.find_balancing_elements()
        position_gap = position_gap / len(self.clients)

        ob_side = 'bids' if position_gap > 0 else 'asks'
        side = 'sell' if position_gap > 0 else 'buy'
        exchanges = ''
        av_price = 0
        av_fee = 0

        for client in self.clients:
            # CREATE ORDER PRICE TO BE SURE IT CLOSES
            orderbook = client.get_orderbook()[client.symbol]
            price = orderbook[ob_side][1][0]
            av_price += price
            av_fee += client.taker_fee
            await self.create_balancing_order(client, position_gap, price, side)
            exchanges += client.EXCHANGE_NAME + ' '
            await self.balance_message(client)

        if amount_to_balancing < self.min_disbalance:
            return

        price = av_price / len(self.clients)
        taker_fee = av_fee / len(self.clients)

        await self.balancing_bd_update(exchanges, client, position_gap, price, side, taker_fee)

    async def setup_mq(self, loop) -> None:
        print(f"SETUP MQ START")
        self.mq = await connect_robust(self.rabbit_url, loop=loop)
        print(f"SETUP MQ ENDED")

    async def setup_postgres(self) -> None:
        self.db = await asyncpg.create_pool(**Config.POSTGRES)

    def get_sizes(self):
        tick_size = max([x.tick_size for x in self.clients if x.tick_size], default=0.01)
        step_size = max([x.step_size for x in self.clients if x.step_size], default=0.01)
        quantity_precision = max([x.quantity_precision for x in self.clients if x.quantity_precision])

        self.client_1.quantity_precision = quantity_precision
        self.client_2.quantity_precision = quantity_precision

        self.client_1.tick_size = tick_size
        self.client_2.tick_size = tick_size

        self.client_1.step_size = step_size
        self.client_2.step_size = step_size

    async def save_new_balance_jump(self):
        if self.start and self.finish:
            to_base = {
                'timestamp': int(round(time.time() * 1000)),
                'total_balance': self.finish,
                'env': self.env
            }

            await self.publish_message(connect=self.mq,
                                       message=to_base,
                                       routing_key=RabbitMqQueues.BALANCE_JUMP,
                                       exchange_name=RabbitMqQueues.get_exchange_name(RabbitMqQueues.BALANCE_JUMP),
                                       queue_name=RabbitMqQueues.BALANCE_JUMP
                                       )

    async def get_total_balance_calc(self, cursor, asc_desc):
        result = 0
        exchanges = []
        time_ = 0
        for r in await get_total_balance(cursor, asc_desc):
            if not r['exchange_name'] in exchanges:
                result += r['total_balance']
                exchanges.append(r['exchange_name'])
                time_ = max(time_, r['ts'])

            if len(exchanges) >= self.exchanges_len:
                break

        return result, str(datetime.datetime.fromtimestamp(time_ / 1000).strftime('%Y-%m-%d %H:%M:%S'))

    async def get_balance_percent(self) -> float:
        async with self.db.acquire() as cursor:
            self.finish, self.f_time = await self.get_total_balance_calc(cursor, 'desc')  # todo

            if res := await get_last_balance_jumps(cursor):
                self.start, self.s_time = res[0], res[1]
            else:
                self.start, self.s_time = await self.get_total_balance_calc(cursor, 'asc')
                await self.save_new_balance_jump()

            if self.start and self.finish:
                return abs(100 - self.finish * 100 / self.start)

            return 0

    async def start_balance_message(self):
        message = f'START BALANCES AND POSITION\n'
        total_balance = 0
        total_position = 0
        index_price = []

        for client in self.clients:
            try:
                coin = client.symbol.split('USD')[0].replace('-', '').replace('/', '')
                message += f"   EXCHANGE: {client.EXCHANGE_NAME}\n"
                message += f"ENV: {Config.ENV}\n"
                message += f"TOT BAL: {client.get_real_balance()} USD\n"
                message += f"POS: {round(client.get_positions()[client.symbol]['amount'], 4)} {coin}\n"
                message += f"AVL BUY:  {round(client.get_available_balance('buy'))}\n"
                message += f"AVL SELL: {round(client.get_available_balance('sell'))}\n"
                index_price.append((client.get_orderbook()[client.symbol]['bids'][1][0] +
                                    client.get_orderbook()[client.symbol]['asks'][1][0]) / 2)
                total_position += client.get_positions()[client.symbol]['amount']
                total_balance += client.get_real_balance()
            except:
                traceback.print_exc()
        try:
            message += f"   TOTAL:\n"
            message += f"START BALANCE: {round(total_balance, 2)} USD\n"
            message += f"POSITION: {round(total_position, 4)} {coin}\n"
            message += f"INDEX PX: {round(sum(index_price) / len(index_price), 2)} USD\n"
        except:
            traceback.print_exc()

        await self.send_message(message, Config.TELEGRAM_CHAT_ID, Config.TELEGRAM_TOKEN)

    async def close_all_positions(self):
        async with aiohttp.ClientSession() as session:
            print('START')
            while abs(self.client_1.get_positions().get(self.client_1.symbol, {}).get('amount_usd', 0)) > 50 \
                    or abs(self.client_2.get_positions().get(self.client_2.symbol, {}).get('amount_usd', 0)) > 50:
                print('START WHILE')
                time.sleep(7)

                for client in self.clients:
                    print(f'START CLIENT {client.EXCHANGE_NAME}')
                    client.cancel_all_orders()
                    if res := client.get_positions().get(client.symbol, {}).get('amount'):
                        print('1212212')
                        orderbook = client.get_orderbook()[client.symbol]
                        side = 'buy' if res < 0 else 'sell'
                        price = orderbook['bids'][0][0] if side == 'buy' else orderbook['asks'][0][0]
                        await client.create_order(abs(res), price, side, session)
                        time.sleep(7)
                    print('- ' * 30)

    def __check_env(self) -> bool:
        return 'DEV_' in self.env.upper()

    async def prepare_alert(self):
        percent_change = round(100 - self.finish * 100 / self.start, 2)
        usd_change = self.finish - self.start

        message = f"ALERT NAME: BALANCE JUMP {'🔴' if usd_change < 0 else '🟢'}\n"
        message += f"MULTIBOT {self.client_1.EXCHANGE_NAME}-{self.client_2.EXCHANGE_NAME}\n"
        message += f"ENV: {self.env}\n"

        if not self.__check_env():
            message += "CHANGE STATE TO PARSER\n"

        message += f"BALANCE CHANGE %: {percent_change}\n"
        message += f"BALANCE CHANGE USD: {usd_change}\n"
        message += f"PREVIOUS BAL, USD: {self.start}\n"
        message += f"CURRENT BAL, USD: {self.finish}\n"
        message += f"PREVIOUS DT: {self.s_time}\n"
        message += f"CURRENT DT: {self.f_time}"

        await self.send_message(message, Config.ALERT_CHAT_ID, Config.ALERT_BOT_TOKEN)

    async def run(self, loop):
        self.loop = loop
        while not self.shifts.get(self.client_1.EXCHANGE_NAME + ' ' + self.client_2.EXCHANGE_NAME):
            print('Wait shifts for', self.client_1.EXCHANGE_NAME + ' ' + self.client_2.EXCHANGE_NAME)
            self.__prepare_shifts()

        await self.setup_mq(loop)
        await self.setup_postgres()

        async with aiohttp.ClientSession() as session:
            self.session = session
            time.sleep(3)
            start_message = False
            while True:
                # time.sleep(0.005)

                if self.state == BotState.PARSER:
                    time.sleep(1)

                if self.state == BotState.BOT and Config.STOP_PERCENT < await self.get_balance_percent():
                    self.state = BotState.PARSER

                    if self.__check_env():
                        self.state = BotState.BOT

                    await self.save_new_balance_jump()
                    await self.prepare_alert()

                if not start_message:
                    await self.start_message()
                    await self.start_balance_message()
                    start_message = True
                await self.find_price_diffs()
                # await self.time_based_messages()

                if int(round(time.time())) - self.start_time >= 180:
                    # print(f"STARTED POSITION BALANCING")
                    # await self.position_balancing()
                    self.start_time = int(round(time.time()))
                # if int(round(time.time())) - self.start_time >= 35:
                #     print(f"False order started to create")
                # await self.create_orders(self.client_1, self.client_2, 0.5, 0, 0, 0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-c1', nargs='?', const=True, default='dydx', dest='client_1')
    parser.add_argument('-c2', nargs='?', const=True, default='bitmex', dest='client_2')
    args = parser.parse_args()

    loop = asyncio.get_event_loop()
    worker = MultiBot(args.client_1, args.client_2)
    loop.run_until_complete(worker.close_all_positions())

    try:
        loop.run_forever()
    finally:
        loop.close()
