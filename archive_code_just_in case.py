import csv
import time
from clients.enums import BotState
def check_ob_slippage(multibot, client_sell, client_buy):
    client_slippage = None
    while True:
        ob_sell = client_sell.get_orderbook(client_sell.symbol)
        ob_buy = client_buy.get_orderbook(client_buy.symbol)
        if client_sell.EXCHANGE_NAME == 'APOLLOX':
            ob_sell_time_shift = 5 * multibot.deal_pause * 1000
        else:
            ob_sell_time_shift = multibot.deal_pause * 1000
        if client_buy.EXCHANGE_NAME == 'APOLLOX':
            ob_buy_time_shift = 5 * multibot.deal_pause * 1000
        else:
            ob_buy_time_shift = multibot.deal_pause * 1000
        current_timestamp = int(time.time() * 1000)
        if current_timestamp - ob_sell['timestamp'] > ob_sell_time_shift:
            if multibot.state == BotState.BOT:
                multibot.state = BotState.SLIPPAGE
                multibot.ob_alert_send(client_sell, client_buy, ob_sell['timestamp'])
                client_slippage = client_sell
            time.sleep(5)
            continue
        elif current_timestamp - ob_buy['timestamp'] > ob_buy_time_shift:
            if multibot.state == BotState.BOT:
                multibot.state = BotState.SLIPPAGE
                multibot.ob_alert_send(client_buy, client_sell, ob_buy['timestamp'])
                client_slippage = client_buy
            time.sleep(5)
            continue
        elif ob_sell['asks'] and ob_sell['bids'] and ob_buy['asks'] and ob_buy['bids']:
            if multibot.state == BotState.SLIPPAGE:
                multibot.state = BotState.BOT
                multibot.ob_alert_send(client_sell, client_buy, ob_sell['timestamp'], client_slippage)
                client_slippage = None
            return ob_sell, ob_buy


    @staticmethod
    def create_csv(filename):
        # Open the CSV file in write mode
        with open(filename, 'w', newline='') as file:
            writer = csv.writer(file)
            # Write header row
            writer.writerow(['TimestampUTC', 'Time Stamp', 'Exchange', 'Coin', 'Flag'])

    @staticmethod
    def append_to_csv(filename, record):
        # Open the CSV file in append mode
        with open(filename, 'a', newline='') as file:
            writer = csv.writer(file)
            # Append new record
            writer.writerow(record)

        # parser = argparse.ArgumentParser()
        # parser.add_argument('-c1', nargs='?', const=True, default='apollox', dest='client_1')
        # parser.add_argument('-c2', nargs='?', const=True, default='binance', dest='client_2')
        # args = parser.parse_args()

        # import cProfile
        #
        #
        # def your_function():
        #
        #
        # # Your code here
        #
        # # Start the profiler
        # profiler = cProfile.Profile()
        # profiler.enable()
        #
        # # Run your code
        # your_function()
        #
        # # Stop the profiler
        # profiler.disable()
        #
        # # Print the profiling results
        # profiler.print_stats(sort='time')