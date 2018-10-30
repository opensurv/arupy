#!/usr/bin/env python3
# coding:utf-8
import os
import traceback
import time
import threading
import pika
from pika import exceptions as pika_exceptions
from pika.adapters.blocking_connection import BlockingChannel
from . import outils
from . import config as config_utils

logger = outils.get_logger('arupy')


class ArupyError(Exception):
    """"""
    pass


class Arupy(object):
    """"""

    def __init__(self, config="config.yml"):
        """Constructor"""
        self._mainthread: threading.Thread = None
        self.daemon = True

        if isinstance(config, str) and os.path.exists(config):
            self.pika_params = config_utils.parse_mq_parameters_from_file(config)
        elif isinstance(config, pika.ConnectionParameters):
            self.pika_params = config
        elif isinstance(config, dict):
            self.pika_params = config_utils.parse_mq_parameters_from_dict(config)
        else:
            raise ValueError("Config: {} cannot be parsed".format(config))
        self.is_working = threading.Event()
        self.consumers = {}
        self.connection = None

    def start(self):
        print("run arupy")
        self._mainthread = threading.Thread(name='arupy-main', target=self.run)
        self._mainthread.daemon = True
        self._mainthread.start()

    def join(self, timeout=None):
        return self._mainthread.join(timeout)

    def run(self):
        if not self.is_working.is_set():
            self.is_working.set()
        else:
            logger.warn("the Arupy is running already.")
            return

        if self.channel:
            self.channel.close()
        if self.connection:
            self.connection.close()

        self.connection = None
        self.channel = None

        logger.info('arupy is started.')
        while self.is_working.is_set():
            # setting for connection
            try:
                if not self.connection:
                    self.connection = pika.BlockingConnection(self.pika_params)
                    logger.info('arupy is connected to mq: {}.'.format(self.pika_params))
            except:
                logger.warn("arupy connection is failed. try 3s later")
                self.connection = None
                time.sleep(3)
                continue

            # setting for channel
            try:
                if not self.channel:
                    self.channel = self.connection.channel()
                    self.channel.basic_qos(prefetch_count=1)
                    logger.info('arupy created a channel and set the qos to 1.')
            except Exception as e:
                logger.warn("create channel failed. retry 3s later: {}".format(e))
                if self.connection:
                    try:
                        self.connection.sleep(3)
                    except:
                        self.connection = None
                        time.sleep(3)
                else:
                    time.sleep(3)
                continue

            # initial_consumer
            try:
                if self.consumers:
                    logger.info("initializing consumers")
                    self.initial_consumers()
                    logger.info('init consumers succeeded.')
            except Exception:
                logger.warn("errors in initial consumer: {}".format(
                    traceback.format_exc()
                ))
                self.connection = None
                time.sleep(3)
                # raise ArupyError()

            try:
                self.channel.start_consuming()
                time.sleep(3)
            except Exception:
                logger.warn("unexpect exit when consuming. {}".format(traceback.format_exc()))
                time.sleep(3)
                self.connection = None
                continue

        self.is_working.clear()
        logger.info("Arupy is shutdown normally.")

    def add_consumer(self, consumer, **kwargs):
        if isinstance(consumer, ArupyConsumer):
            consumer.bind_app(app=self)
        else:
            try:
                if issubclass(consumer, ArupyConsumer):
                    consumer = consumer(app=self, **kwargs)
            except Exception:
                raise TypeError(
                    "consumer should be subclass/subinstance of ArupyConsumer but {}".format(type(consumer)))

        if consumer.queue_name not in self.consumers:
            logger.info('consumer: {} is added.'.format(consumer))
            self.consumers[consumer.queue_name] = consumer

            if self.is_working.is_set():
                logger.info("reseting Arupy...")
                self.stop()

                logger.info("waiting for the mainthread stopped.")
                self.join()

                logger.info("restart Arupy")
                self.start()
            else:
                logger.info("the Arupy is not running. just pass.")
        else:
            raise ValueError("the queue_name: {} is existed in consumers")

    def new_publisher(self):
        return ArupyPublisher(self.pika_params)

    def new_safe_publisher(self, delivery_confirm=False):
        return ArupySafePublisher(self.pika_params, delivery_confirm)

    def initial_consumers(self):
        def _(qname, consumer):
            consumer.on_channel_created(self.channel)
            logger.info("set consumer for queue name: {} tag: {}".format(qname, qname))
            self.channel.basic_consume(consumer.handle, qname, consumer_tag=qname)

        [_(qname, consumer) for (qname, consumer) in self.consumers.items()]

    def serve_until_no_consumers(self):
        while self.consumers:
            self.is_working.wait(1)

    def remove_consumer(self, qname):
        self.channel.basic_cancel(consumer_tag=qname)
        logger.info("cancel consumer for queue: {}".format(qname))
        self.consumers.pop(qname, None)

    def stop(self):
        try:
            self.channel.stop_consuming()
            logger.info("stop consuming succeeded.")
        except:
            logger.info("stop consuming failed.")

        if self.is_working.isSet():
            self.is_working.clear()

        try:
            self.channel.close()
            logger.info("close channel succeeded")
        except:
            pass

        try:
            self.connection.close()
            logger.info("close pika connection succeeded")
        except:
            pass


class ArupyConsumer(object):
    """"""

    queue_name = "default"

    def __init__(self, app: Arupy = None):
        """Constructor"""
        self.app = app

    def bind_app(self, app: Arupy):
        self.app = app

    def on_channel_created(self, channel):
        pass

    def handle(self, channel, methods, props, body: bytes):
        pass


class ArupyPublisher(object):
    """"""

    def __init__(self, pika_params):
        """Constructor"""
        self.params = pika_params
        self.conn = pika.BlockingConnection(self.params)
        self.chan = self.conn.channel()

    def publish(self, exchange, routing_key, body, properties=None, mandatory=False, immediate=False):
        self.chan.basic_publish(exchange, routing_key, body, properties, mandatory, immediate)

    def close(self):
        self.chan.close()
        self.conn.close()


class ArupySafePublisher(object):
    """"""

    def __init__(self, pika_params, confirm=False):
        """Constructor"""
        self.params = pika_params
        self._confirm = confirm
        self.chan: BlockingChannel = None

        # set conn N chan for self
        self._initial()

    def _initial(self):
        while True:
            try:
                self.conn = pika.BlockingConnection(self.params)
                self.chan = self.conn.channel()
                if self._confirm:
                    self.chan.confirm_delivery()
                break
            except Exception as e:
                logger.warn("publisher cannot connect to mq with config: {} for {}".format(
                    self.params, e
                ))
                logger.info("2s later retry to connect mq")
                time.sleep(2)
                continue

    def publish(self, exchange, routing_key, body, properties=None, mandatory=False, immediate=False):
        while True:
            try:
                return self.chan.basic_publish(exchange, routing_key, body, properties, mandatory, immediate)
            except pika_exceptions.AMQPChannelError as e:
                self._reset_by_exception(e)
            except pika_exceptions.ChannelError as e:
                self._reset_by_exception(e)
            except pika_exceptions.RecursionError as e:
                self._reset_by_exception(e)
            except Exception as e:
                self._reset_by_exception(e)

            time.sleep(1)

    def close(self):
        self.chan.close()
        self.conn.close()

    def _reset_by_exception(self, e):
        logger.warn("reset publisher by exception: {}, retry!".format(e))
        try:
            self.close()
        except Exception:
            pass

        self._initial()
