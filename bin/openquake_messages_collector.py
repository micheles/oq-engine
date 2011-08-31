#! /usr/bin/python

"""
A collector for the log messages generated by (possibly distributed) OpenQuake
jobs.

The collected messages will be logged by the "collector" logger, as configured
in logging.cfg
"""

import os

import logging
import logging.config

import openquake
from openquake import signalling

import oqpath
oqpath.set_oq_path()


class Collector(signalling.LogMessageConsumer):
    def __init__(self, *args, **kwargs):
        super(Collector, self).__init__(*args, **kwargs)

        self.logger = logging.getLogger('collector')

    def message_callback(self, msg):
        try:
            job_id, type_ = \
                signalling.parse_routing_key(msg.delivery_info['routing_key'])
        except ValueError:
            pass
        else:
            if type_ in ('debug', 'info', 'warn', 'error', 'fatal'):
                self.logger.log(getattr(logging, type_.upper()), msg.body)


def main():
    logging.config.fileConfig(os.path.join(openquake.OPENQUAKE_ROOT,
                                           'logging.cfg'))

    # any job
    collector = Collector('*')

    collector.run()


if __name__ == '__main__':
    main()
