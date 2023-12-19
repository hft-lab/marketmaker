import configparser
import logging
import sys
import time
from datetime import datetime
from logging.config import dictConfig
from typing import List


from arbitrage_finder import ArbitrageFinder, AP
from clients.core.all_clients import ALL_CLIENTS
from clients_markets_data import Clients_markets_data

from core.telegram import Telegram, TG_Groups
from core.wrappers import try_exc_regular
# from logger import Logging

config = configparser.ConfigParser()
config.read(sys.argv[1], "utf-8")

dictConfig({'version': 1, 'disable_existing_loggers': False, 'formatters': {
    'simple': {'format': '[%(asctime)s][%(threadName)s] %(funcName)s: %(message)s'}},
            'handlers': {'console': {'class': 'logging.StreamHandler', 'level': 'DEBUG', 'formatter': 'simple',
                                     'stream': 'ext://sys.stdout'}},
            'loggers': {'': {'handlers': ['console'], 'level': 'INFO', 'propagate': False}}})
logger = logging.getLogger(__name__)


class MultiParser:
    __slots__ = ['cycle_parser_delay', 'chosen_deal', 'profit_taker','leverage',
                 'telegram', 'state', 'start_time', 'session', 'clients', 'exchanges', 'ribs', 'env', 'tasks',
                 'loop_2', 'last_orderbooks', 'time_start', 'time_parser', 'bot_launch_id', 'max_position_part',
                 'setts', 'rates_file_name', 'markets', 'clients_markets_data', 'finder',
                 'clients_with_names','exchanges_in_ribs']

    def __init__(self):

        self.setts = config['SETTINGS']
        self.state = self.setts['STATE']
        self.leverage = float(self.setts['LEVERAGE'])
        self.cycle_parser_delay = float(self.setts['CYCLE_PARSER_DELAY'])
        self.env = self.setts['ENV']


        # ORDER CONFIGS

        self.profit_taker = float(self.setts['TARGET_PROFIT'])
        self.max_position_part = float(self.setts['PERCENT_PER_MARKET'])

        # CLIENTS
        self.exchanges = self.get_exchages_from_ribs()
        self.ribs = self.get_exchanges_ribs()
        self.clients = []

        for exchange in self.exchanges:
            client = ALL_CLIENTS[exchange](keys=config[exchange], leverage=self.leverage,
                                           max_pos_part=self.max_position_part)
            self.clients.append(client)
        self.clients_with_names = {}
        for client in self.clients:
            self.clients_with_names.update({client.EXCHANGE_NAME: client})

        self.start_time = datetime.utcnow().timestamp()

        # NEW REAL MULTI BOT
        self.clients_markets_data = Clients_markets_data(self.clients, self.setts['INSTANCE_NUM'])
        self.markets = self.clients_markets_data.coins_clients_symbols
        self.clients_markets_data = self.clients_markets_data.clients_data
        self.finder = ArbitrageFinder(self.markets, self.clients_with_names, self.profit_taker, self.profit_taker)
        self.chosen_deal: AP

        for client in self.clients:
            client.markets_list = list(self.markets.keys())
            client.run_updater()

        self.telegram = Telegram()
        self.launch()


    @try_exc_regular
    def get_exchanges_ribs(self):
        ribs_raw = self.setts['RIBS'].split(',')
        ribs = []
        for rib in ribs_raw:
            ex1,ex2 = rib.split('|')
            ribs.append([ex1, ex2])
            ribs.append([ex2, ex1])
        return ribs


    @try_exc_regular
    def get_exchages_from_ribs(self):
        ribs = self.setts['RIBS'].split(',')
        exchanges = []
        for rib in ribs:
            ex1, ex2 = rib.split('|')
            if ex1 not in exchanges:
                exchanges.append(ex1)
            if ex2 not in exchanges:
                exchanges.append(ex2)
        return exchanges

    @try_exc_regular
    def websocket_main_cycle(self):

        while True:
            time.sleep(self.cycle_parser_delay)
            if not round(datetime.utcnow().timestamp() - self.start_time) % 90:
                self.start_time -= 1
                self.telegram.send_message(f"PARSER IS WORKING", TG_Groups.MainGroup)
                print('PARSER IS WORKING')
            # Шаг 1 (Сбор данных с бирж по рынкам)
            time_start_parsing = time.time()
            results = self.get_data_for_parser()
            time_end_parsing = time.time()

            # logger_custom.log_rates(iteration, results)

            # Шаг 2 (Анализ маркет данных с бирж и поиск потенциальных AP)
            potential_deals = self.finder.arbitrage_possibilities(results, self.ribs)

            # print('Potential deals:', json.dumps(self.potential_deals, indent=2))
            time_end_define_potential_deals = time.time()

            if len(potential_deals):
                # Шаг 3 (Выбор лучшей AP, если их несколько)
                self.chosen_deal: AP = self.choose_deal(potential_deals)
                if self.chosen_deal:
                    time_end_choose = time.time()
                    self.chosen_deal.ts_define_potential_deals_end = time_end_define_potential_deals
                    self.chosen_deal.ts_choose_end = time.time()
                    self.chosen_deal.time_parser = time_end_parsing - time_start_parsing
                    self.chosen_deal.time_define_potential_deals = time_end_define_potential_deals - time_end_parsing
                    self.chosen_deal.time_choose = time_end_choose - time_end_define_potential_deals
                    # Шаг 4 (Проверка, что выбранная AP все еще действует, здесь заново запрашиваем OB)
                    if self.check_prices_still_good():
                        pass

                        # with open('ap_still_active_status.csv', 'a', newline='') as file:
                        #     writer = csv.writer(file)
                        #     row_data = [str(y) for y in chosen_deal.values()] + ['inactive clients']
                        #     writer.writerow(row_data)

    @try_exc_regular
    def launch(self):

        # Принтим показатели клиентов - справочно
        print('CLIENTS MARKET DATA:')
        for exchange, exchange_data in self.clients_markets_data.items():
            print(exchange, exchange_data['markets_amt'])
        print('PARSER STARTED')

        # with open(f'rates.txt', 'a') as file:
        #     file.write('')

        self.telegram.send_message('Parser WS Started', TG_Groups.MainGroup)
        self.websocket_main_cycle()
        # logger_custom = Logging()
        # logger_custom.log_launch_params(self.clients)

    @try_exc_regular
    def get_data_for_parser(self):
        data = dict()
        for client in self.clients:
            data.update(client.get_all_tops())
        return data

    @try_exc_regular
    def choose_deal(self, potential_deals: List[AP]) -> AP:
        max_profit = 0
        chosen_deal = None
        for deal in potential_deals:
            if deal.profit_rel_parser > max_profit:
                max_profit = deal.profit_rel_parser
                chosen_deal = deal
        return chosen_deal

    @try_exc_regular
    def check_prices_still_good(self):
        buy_market, sell_market = self.chosen_deal.buy_market, self.chosen_deal.sell_market
        ob_buy = self.chosen_deal.client_buy.get_orderbook(buy_market)
        ob_sell = self.chosen_deal.client_sell.get_orderbook(sell_market)

        self.chosen_deal.ob_buy = {key: value[:5] if key in ['asks', 'bids'] else value for key, value in
                                   ob_buy.items()}
        self.chosen_deal.ob_sell = {key: value[:5] if key in ['asks', 'bids'] else value for key, value in
                                    ob_sell.items()}

        buy_price, sell_price = self.chosen_deal.ob_buy['asks'][0][0], self.chosen_deal.ob_sell['bids'][0][0]
        profit_brutto = (sell_price - buy_price) / buy_price
        profit = profit_brutto - self.chosen_deal.buy_fee - self.chosen_deal.sell_fee

        self.chosen_deal.buy_max_amount_ob = self.chosen_deal.ob_buy['asks'][0][1]
        self.chosen_deal.sell_max_amount_ob = self.chosen_deal.ob_sell['bids'][0][1]
        self.chosen_deal.buy_price_target = buy_price
        self.chosen_deal.sell_price_target = sell_price

        self.chosen_deal.deal_max_amount_ob = min(self.chosen_deal.buy_max_amount_ob,
                                                  self.chosen_deal.sell_max_amount_ob)
        self.chosen_deal.deal_max_usd_ob = self.chosen_deal.deal_max_amount_ob * (buy_price + sell_price) / 2

        self.chosen_deal.profit_rel_target = profit
        self.chosen_deal.ts_check_still_good_end = time.time()
        self.chosen_deal.time_check_ob = self.chosen_deal.ts_check_still_good_end - self.chosen_deal.ts_choose_end

        if profit >= self.chosen_deal.target_profit:
            self.telegram.send_ap_still_active_parser(self.chosen_deal, TG_Groups.Alerts)
            time.sleep(3)
            return True
        else:
            self.telegram.send_ap_expired_message(self.chosen_deal, TG_Groups.Alerts)
            return False



if __name__ == '__main__':
    MultiParser()

