# This file is part of Maker Keeper Framework.
#
# Copyright (C) 2017 reverendus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import _jsonnet
import argparse
import datetime
import json
import os
import signal
import sys
import threading
import time

import zlib

import pkg_resources

from keeper.api import Address, register_filter_thread, all_filter_threads_alive, stop_all_filter_threads, \
    any_filter_thread_present, Wad, Contract
from keeper.api.gas import FixedGasPrice, DefaultGasPrice, GasPrice
from keeper.api.logger import Logger, Event
from keeper.api.util import AsyncCallback, chain
from web3 import Web3, HTTPProvider

from keeper.api.token import ERC20Token
from keeper.lifecycle import Web3Lifecycle


class Keeper:
    def __init__(self, args: list, **kwargs):
        parser = argparse.ArgumentParser(prog=self.executable_name())
        parser.add_argument("--rpc-host", help="JSON-RPC host (default: `localhost')", default="localhost", type=str)
        parser.add_argument("--rpc-port", help="JSON-RPC port (default: `8545')", default=8545, type=int)
        parser.add_argument("--eth-from", help="Ethereum account from which to send transactions", required=True, type=str)
        parser.add_argument("--gas-price", help="Static gas pricing: Gas price in Wei", default=0, type=int)
        parser.add_argument("--debug", help="Enable debug output", dest='debug', action='store_true')
        parser.add_argument("--trace", help="Enable trace output", dest='trace', action='store_true')
        self.args(parser)
        self.arguments = parser.parse_args(args)
        self.web3 = kwargs['web3'] if 'web3' in kwargs else Web3(HTTPProvider(endpoint_uri=f"http://{self.arguments.rpc_host}:{self.arguments.rpc_port}"))
        self.web3.eth.defaultAccount = self.arguments.eth_from
        self.our_address = Address(self.arguments.eth_from)
        self.chain = chain(self.web3)
        self.config = kwargs['config'] if 'config' in kwargs else Config.load_config(self.chain)
        self.gas_price = self._get_gas_price()
        self.terminated_internally = False
        self.terminated_externally = False
        self.fatal_termination = False
        self._at_least_one_every = False
        self._last_block_time = None
        self._on_block_callback = None
        self._config_checksum = {}
        _json_log = os.path.abspath(pkg_resources.resource_filename(__name__, f"../logs/{self.executable_name()}_{self.chain}_{self.our_address}.json.log".lower()))
        self.logger = Logger(self.keeper_name(), self.chain, _json_log, self.arguments.debug, self.arguments.trace)
        Contract.logger = self.logger

    def start(self):
        with Web3Lifecycle(self.web3, self.logger) as lifecycle:
            lifecycle.on_startup(self.startup)
            lifecycle.on_shutdown(self.shutdown)

        # self.logger.info(f"{self.executable_name()}")
        # self.logger.info(f"{'-' * len(self.executable_name())}")
        # self.logger.info(f"Keeper on {self.chain}, connected to {self.web3.providers[0].endpoint_uri}")
        # self.logger.info(f"Keeper operating as {self.our_address}")
        # self._check_account_unlocked()
        # self._wait_for_init()
        # # self.print_eth_balance()
        # self.logger.info("Keeper started")
        # self.startup()
        # self._main_loop()
        # self.logger.info("Shutting down the keeper")
        # if any_filter_thread_present():
        #     self.logger.info("Waiting for all threads to terminate...")
        #     stop_all_filter_threads()
        # if self._on_block_callback is not None:
        #     self.logger.info("Waiting for outstanding callback to terminate...")
        #     self._on_block_callback.wait()
        # self.logger.info("Executing keeper shutdown logic...")
        # self.shutdown()
        # self.logger.info("Keeper terminated")
        # exit(10 if self.fatal_termination else 0)

    def args(self, parser: argparse.ArgumentParser):
        pass

    def startup(self, lifecycle: Web3Lifecycle):
        raise NotImplementedError("Please implement the startup() method")

    def shutdown(self, lifecycle: Web3Lifecycle):
        pass
    
    def terminate(self, message=None):
        if message is not None:
            self.logger.warning(message)

        self.terminated_internally = True

    def sigint_sigterm_handler(self, sig, frame):
        if self.terminated_externally:
            self.logger.warning("Graceful keeper termination due to SIGINT/SIGTERM already in progress")
        else:
            self.logger.warning("Keeper received SIGINT/SIGTERM signal, will terminate gracefully")
            self.terminated_externally = True

    @staticmethod
    def keeper_name():
        return os.path.basename(sys.argv[0]).replace('_', '-').replace('.py', '')

    def executable_name(self):
        return "keeper-" + self.keeper_name()

    def eth_balance(self, address: Address) -> Wad:
        assert(isinstance(address, Address))
        return Wad(self.web3.eth.getBalance(address.address))

    # def print_eth_balance(self):
    #     balance = self.eth_balance(self.our_address)
    #     self.logger.info(f"Keeper account balance is {balance} ETH", Event.eth_balance(self.our_address, balance))

    def get_config(self, filename):
        with open(filename) as data_file:
            content_file = data_file.read()
            content_config = _jsonnet.evaluate_snippet("snippet", content_file, ext_vars={})
            result = json.loads(content_config)

            # Report if file has been newly loaded or reloaded
            checksum = zlib.crc32(content_config.encode('utf-8'))
            if filename not in self._config_checksum:
                self.logger.info(f"Loaded configuration from '{filename}'")
                self.logger.debug(f"Config file is: " + json.dumps(result, indent=4))
            elif self._config_checksum[filename] != checksum:
                self.logger.info(f"Reloaded configuration from '{filename}'")
                self.logger.debug(f"Reloaded config file is: " + json.dumps(result, indent=4))
            self._config_checksum[filename] = checksum

            return result

    def on_block(self, callback):
        def new_block_callback(block_hash):
            self._last_block_time = datetime.datetime.now()
            block = self.web3.eth.getBlock(block_hash)
            block_number = block['number']
            if not self.web3.eth.syncing:
                max_block_number = self.web3.eth.blockNumber
                if block_number == max_block_number:
                    def on_start():
                        self.logger.debug(f"Processing block #{block_number} ({block_hash})")

                    def on_finish():
                        self.logger.debug(f"Finished processing block #{block_number} ({block_hash})")

                    if not self._on_block_callback.trigger(on_start, on_finish):
                        self.logger.info(f"Ignoring block #{block_number} ({block_hash}),"
                                         f" as previous callback is still running")
                else:
                    self.logger.info(f"Ignoring block #{block_number} ({block_hash}),"
                                     f" as there is already block #{max_block_number} available")
            else:
                self.logger.info(f"Ignoring block #{block_number} ({block_hash}), as the node is syncing")

        self._on_block_callback = AsyncCallback(callback)

        block_filter = self.web3.eth.filter('latest')
        block_filter.watch(new_block_callback)
        register_filter_thread(block_filter)

        self.logger.info("Watching for new blocks")

    def every(self, frequency_in_seconds: int, callback):
        def setup_timer(delay):
            timer = threading.Timer(delay, func)
            timer.daemon = True
            timer.start()

        def func():
            try:
                callback()
            except:
                setup_timer(frequency_in_seconds)
                raise
            setup_timer(frequency_in_seconds)

        setup_timer(1)
        self._at_least_one_every = True

    def _get_gas_price(self) -> GasPrice:
        if self.arguments.gas_price > 0:
            return FixedGasPrice(self.arguments.gas_price)
        else:
            return DefaultGasPrice()

    def _wait_for_init(self):
        # wait for the client to have at least one peer
        if self.web3.net.peerCount == 0:
            self.logger.info(f"Waiting for the node to have at least one peer...")
            while self.web3.net.peerCount == 0:
                time.sleep(0.25)

        # wait for the client to sync completely,
        # as we do not want to apply keeper logic to stale blocks
        if self.web3.eth.syncing:
            self.logger.info(f"Waiting for the node to sync...")
            while self.web3.eth.syncing:
                time.sleep(0.25)

    def _check_account_unlocked(self):
        try:
            self.web3.eth.sign(self.web3.eth.defaultAccount, "test")
        except:
            self.logger.fatal(f"Account {self.web3.eth.defaultAccount} is not unlocked")
            self.logger.fatal(f"Unlocking the account is necessary for the keeper to operate")
            exit(-1)

    def _main_loop(self):
        # terminate gracefully on either SIGINT or SIGTERM
        signal.signal(signal.SIGINT, self.sigint_sigterm_handler)
        signal.signal(signal.SIGTERM, self.sigint_sigterm_handler)

        # in case at least one filter has been set up, we enter an infinite loop and let
        # the callbacks do the job. in case of no filters, we will not enter this loop
        # and the keeper will terminate soon after it started
        while any_filter_thread_present() or self._at_least_one_every:
            time.sleep(1)

            # if the keeper logic asked us to terminate, we do so
            if self.terminated_internally:
                self.logger.warning("Keeper logic asked for termination, the keeper will terminate")
                break

            # if SIGINT/SIGTERM asked us to terminate, we do so
            if self.terminated_externally:
                self.logger.warning("The keeper is terminating due do SIGINT/SIGTERM signal received")
                break

            # if any exception is raised in filter handling thread (could be an HTTP exception
            # while communicating with the node), web3.py does not retry and the filter becomes
            # dysfunctional i.e. no new callbacks will ever be fired. we detect it and terminate
            # the keeper so it can be restarted.
            if not all_filter_threads_alive():
                self.logger.fatal("One of filter threads is dead, the keeper will terminate")
                self.fatal_termination = True
                break

            # if we are watching for new blocks and no new block has been reported during
            # some time, we assume the watching filter died and terminate the keeper
            # so it can be restarted.
            #
            # this used to happen when the machine that has the node and the keeper running
            # was put to sleep and then woken up.
            #
            # TODO the same thing could possibly happen if we watch any event other than
            # TODO a new block. if that happens, we have no reliable way of detecting it now.
            if self._last_block_time and (datetime.datetime.now() - self._last_block_time).total_seconds() > 300:
                if not self.web3.eth.syncing:
                    self.logger.fatal("No new blocks received for 300 seconds, the keeper will terminate")
                    self.fatal_termination = True
                    break


class Config:
    def __init__(self, config: dict):
        self.config = config

    @classmethod
    def load_config(cls, chain: str):
        with open('keeper/config.json') as data_file:
            return Config(json.load(data_file)[chain])

    def get_config(self):
        return self.config

    def get_contract_address(self, name):
        return self.config["contracts"].get(name, None)
