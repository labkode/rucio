# Copyright European Organization for Nuclear Research (CERN) since 2012
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Judge-Cleaner is a daemon to clean expired replication rules.
"""
import functools
import logging
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta
from random import randint
from re import match
from typing import TYPE_CHECKING, Optional

from sqlalchemy.exc import DatabaseError

import rucio.db.sqla.util
from rucio.common import exception
from rucio.common.exception import DatabaseException, RuleNotFound, UnsupportedOperation
from rucio.common.logging import setup_logging
from rucio.core.monitor import MetricManager
from rucio.core.rule import delete_rule, get_expired_rules
from rucio.daemons.common import HeartbeatHandler, run_daemon
from rucio.db.sqla.constants import MYSQL_LOCK_NOWAIT_REGEX, ORACLE_CONNECTION_LOST_CONTACT_REGEX, ORACLE_RESOURCE_BUSY_REGEX, PSQL_PSYCOPG_LOCK_NOT_AVAILABLE_REGEX
from rucio.db.sqla.util import get_db_time

if TYPE_CHECKING:
    from types import FrameType

METRICS = MetricManager(module=__name__)
graceful_stop = threading.Event()
DAEMON_NAME = 'judge-cleaner'


def rule_cleaner(
        once: bool = False,
        sleep_time: int = 60
) -> None:
    """
    Main loop to check for expired replication rules
    """
    paused_rules = {}  # {rule_id: datetime}
    run_daemon(
        once=once,
        graceful_stop=graceful_stop,
        executable=DAEMON_NAME,
        partition_wait_time=1,
        sleep_time=sleep_time,
        run_once_fnc=functools.partial(
            run_once,
            paused_rules=paused_rules,
        )
    )


def run_once(
        paused_rules: dict[str, datetime],
        heartbeat_handler: HeartbeatHandler,
        **_kwargs
) -> None:
    worker_number, total_workers, logger = heartbeat_handler.live()

    start = time.time()

    # Refresh paused rules
    iter_paused_rules = deepcopy(paused_rules)
    for key in iter_paused_rules:
        if datetime.utcnow() > paused_rules[key]:
            del paused_rules[key]

    rules = get_expired_rules(total_workers=total_workers,
                              worker_number=worker_number,
                              limit=200,
                              blocked_rules=[key for key in paused_rules])
    logger(logging.DEBUG, 'index query time %f fetch size is %d' % (time.time() - start, len(rules)))

    if not rules:
        logger(logging.DEBUG, 'did not get any work (paused_rules=%s)' % str(len(paused_rules)))
        return

    for rule in rules:
        _, _, logger = heartbeat_handler.live()
        rule_id = rule[0]
        rule_expression = rule[1]
        logger(logging.INFO, 'Deleting rule %s with expression %s' % (rule_id, rule_expression))
        if graceful_stop.is_set():
            break
        try:
            start = time.time()
            delete_rule(rule_id=rule_id, nowait=True)
            logger(logging.DEBUG, 'deletion of %s took %f' % (rule_id, time.time() - start))
        except (DatabaseException, DatabaseError, UnsupportedOperation) as e:
            if match(ORACLE_RESOURCE_BUSY_REGEX, str(e.args[0])) or match(PSQL_PSYCOPG_LOCK_NOT_AVAILABLE_REGEX, str(e.args[0])) or match(MYSQL_LOCK_NOWAIT_REGEX, str(e.args[0])):
                paused_rules[rule_id] = datetime.utcnow() + timedelta(seconds=randint(600, 2400))  # noqa: S311
                METRICS.counter('exceptions.{exception}').labels(exception='LocksDetected').inc()
                logger(logging.WARNING, 'Locks detected for %s' % rule_id)
            elif match('.*QueuePool.*', str(e.args[0])):
                logger(logging.WARNING, 'DatabaseException', exc_info=True)
                METRICS.counter('exceptions.{exception}').labels(exception=e.__class__.__name__).inc()
            elif match(ORACLE_CONNECTION_LOST_CONTACT_REGEX, str(e.args[0])):
                logger(logging.WARNING, 'DatabaseException', exc_info=True)
                METRICS.counter('exceptions.{exception}').labels(exception=e.__class__.__name__).inc()
            else:
                logger(logging.ERROR, 'DatabaseException', exc_info=True)
                METRICS.counter('exceptions.{exception}').labels(exception=e.__class__.__name__).inc()
        except RuleNotFound:
            pass


def stop(signum: Optional[int] = None, frame: Optional["FrameType"] = None) -> None:
    """
    Graceful exit.
    """
    graceful_stop.set()


def run(
        once: bool = False,
        threads: int = 1,
        sleep_time: int = 60
) -> None:
    """
    Starts up the Judge-Clean threads.
    """
    setup_logging(process_name=DAEMON_NAME)

    if rucio.db.sqla.util.is_old_db():
        raise exception.DatabaseException('Database was not updated, daemon won\'t start')

    client_time, db_time = datetime.utcnow(), get_db_time()
    max_offset = timedelta(hours=1, seconds=10)
    if type(db_time) is datetime:
        if db_time - client_time > max_offset or client_time - db_time > max_offset:
            logging.critical('Offset between client and db time too big. Stopping Cleaner')
            return

    if once:
        rule_cleaner(once)
    else:
        logging.info('Cleaner starting %s threads' % str(threads))
        thread_list = [threading.Thread(target=rule_cleaner, kwargs={'once': once,
                                                                     'sleep_time': sleep_time}) for i in range(0, threads)]
        [t.start() for t in thread_list]
        # Interruptible joins require a timeout.
        while thread_list[0].is_alive():
            [t.join(timeout=3.14) for t in thread_list]
