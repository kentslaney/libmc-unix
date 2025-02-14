#!/usr/bin/env python
# encoding: utf-8
"""Run benchmarks with build/lib.* in sys.path"""

import sys
import math
import logging
import threading
from functools import wraps
from collections import namedtuple
from contextlib import contextmanager
from queue import Queue

import pylibmc
import libmc

if sys.version_info.major == 2:
    from time import clock as process_time
else:
    from time import process_time

if True:
    spawn = lambda f, *a: threading.Thread(target=f, args=a)
else:
    # ThreadedGreenletCompat.test_many_eventlets
    import gevent
    import gevent.monkey
    gevent.monkey.patch_all()

    import greenify
    greenify.greenify()
    for so_path in libmc.DYNAMIC_LIBRARIES:
        assert greenify.patch_lib(so_path)

    spawn = gevent.spawn

logger = logging.getLogger('libmc.bench')

Benchmark = namedtuple('Benchmark', 'name f args kwargs')
Participant = namedtuple('Participant', 'name factory threads', defaults=(1,))
BENCH_TIME = 1.0
N_SERVERS = 20
NTHREADS = 4
POOL_SIZE = 4

# setting (eg) NTHREADS to 40 and POOL_SIZE to 4 illustrates a failure case of a
# simpler python solution to thread pools for clients
#NTHREADS = 40
#POOL_SIZE = 4

class Prefix(object):
    '''add prefix for key in mc command'''

    def __init__(self, mc_client, prefix):
        self.mc = mc_client
        self.prefix = prefix

    def __repr__(self):
        return "Prefix " + str(self.mc)

    def __prepare_prefix_keys(self, keys):
        if isinstance(keys, dict):
            prepared_keys = {}
            for key, value in keys.items():
                prepared_keys[self.prefix + key] = value
            return prepared_keys
        elif isinstance(keys, (list, tuple)):
            return [self.prefix + v for v in keys]
        else:
            return self.prefix + keys

    def get_multi(self, keys):
        r = {}
        for key, value in self.mc.get_multi(self.__prepare_prefix_keys(keys)).items():
            r[key.replace(self.prefix, '', 1)] = value
        return r

    def __getattr__(self, name):
        if name in ('get', 'add', 'replace', 'set', 'cas', 'delete', 'incr', 'decr', 'prepend', 'append', 'touch',
                    'expire', 'gets'):

            def func(key, *args, **kwargs):
                key = self.__prepare_prefix_keys(key)
                return getattr(self.mc, name)(key, *args, **kwargs)

            return func
        elif name in ('get_multi', 'append_multi', 'prepend_multi', 'delete_multi', 'set_multi', 'get_list'):

            def func(keys, *args, **kwargs):
                keys = self.__prepare_prefix_keys(keys)
                return getattr(self.mc, name)(keys, *args, **kwargs)

            return func
        elif not name.startswith('__'):

            def func(*args, **kwargs):
                return getattr(self.mc, name)(*args, **kwargs)

            return func
        raise AttributeError(name)


def ratio(a, b):
    if a > b:
        return (a / b, 1)
    elif a < b:
        return (1, b / a)
    else:
        return (1, 1)


class Stopwatch(object):
    "A stopwatch that never stops"

    def __init__(self):
        self.t0 = process_time()
        self.laps = []

    def __unicode__(self):
        m = self.mean()
        d = self.stddev()
        fmt = u"%.3gs, \u03C3=%.3g, n=%d, snr=%.3g:%.3g".__mod__
        return fmt((m, d, len(self.laps)) + ratio(m, d))

    __str__ = __unicode__

    def mean(self):
        return sum(self.laps) / len(self.laps)

    def stddev(self):
        mean = self.mean()
        return math.sqrt(sum((lap - mean)**2 for lap in self.laps) / len(self.laps))

    def total(self):
        return process_time() - self.t0

    @contextmanager
    def timing(self):
        t0 = process_time()
        try:
            yield
        finally:
            te = process_time()
            self.laps.append(te - t0)


class DelayedStopwatch(Stopwatch):
    def __init__(self, laps=None, bound=0):
        super().__init__()
        self.laps = laps or []
        self._bound = bound

    @property
    def bound(self):
        return self._bound or sum(self.laps)

    def timing(self):
        self.t0 = process_time()
        self.timing = super().timing
        return super().timing()

    def __add__(self, other):
        bound = ((self.bound or other.bound) + (other.bound or self.bound)) / 2
        return DelayedStopwatch(self.laps + other.laps, bound)

    def mean(self):
        return self.bound / len(self.laps)

    def stddev(self):
        boundless = DelayedStopwatch(self.laps) if self._bound else super()
        return boundless.stddev()


def benchmark_method(f):
    "decorator to turn f into a factory of benchmarks"

    @wraps(f)
    def inner(name, *args, **kwargs):
        return Benchmark(name, f, args, kwargs)

    return inner


@benchmark_method
def bench_get(mc, key, data):
    if mc.get(key) != data:
        # logger.warn('%r.get(%r) fail', mc, key)
        raise Exception()


@benchmark_method
def bench_set(mc, key, data):
    if any(isinstance(mc.mc, client) for client in libmc_clients):
        if not mc.set(key, data):
            # logger.warn('%r.set(%r, ...) fail', mc, key)
            raise Exception()
    else:
        if not mc.set(key, data, min_compress_len=4001):
            # logger.warn('%r.set(%r, ...) fail', mc, key)
            raise Exception()


@benchmark_method
def bench_get_multi(mc, keys, pairs):
    if len(mc.get_multi(keys)) != len(pairs):
        # logger.warn('%r.get_multi() incomplete', mc)
        raise Exception()


@benchmark_method
def bench_set_multi(mc, keys, pairs):
    ret = mc.set_multi(pairs)
    if any(isinstance(mc.mc, client) for client in libmc_clients):
        if not ret:
            # logger.warn('%r.set_multi fail', mc)
            raise Exception()
    else:
        if ret:
            # logger.warn('%r.set_multi(%r) fail', mc, ret)
            raise Exception()


def multi_pairs(n, val_len):
    d = {('multi_key_%d' % i): ('i' * val_len) for i in range(n)}
    return (list(d.keys()), d)


complex_data_type = ([], {}, __import__('fractions').Fraction(3, 4))

# NOTE: set and get should be in order
benchmarks = [
    bench_set_multi('Multi set 10 keys with value size 100', *multi_pairs(10, 100)),
    bench_get_multi('Multi get 10 keys with value size 100', *multi_pairs(10, 100)),
    bench_set_multi('Multi set 100 keys with value size 100', *multi_pairs(100, 100)),
    bench_get_multi('Multi get 100 keys with value size 100', *multi_pairs(100, 100)),
    bench_set_multi('Multi set 10 keys with value size 1000', *multi_pairs(10, 1000)),
    bench_get_multi('Multi get 10 keys with value size 1000', *multi_pairs(10, 1000)),
    bench_set('Small set', 'abc', 'all work no play jack is a dull boy'),
    bench_get('Small get', 'abc', 'all work no play jack is a dull boy'),
    bench_set('4k uncompressed set', 'abc' * 8, 'defb' * 1000),
    bench_get('4k uncompressed get', 'abc' * 8, 'defb' * 1000),
    bench_set('4k compressed set', 'abc' * 8, 'a' + 'defb' * 1000),
    bench_get('4k compressed get', 'abc' * 8, 'a' + 'defb' * 1000),
    bench_set('1M compressed set', 'abc', '1' * 1000000),
    bench_get('1M compressed get', 'abc', '1' * 1000000),
    bench_set('Complex data set', 'abc', complex_data_type),
    bench_get('Complex data get', 'abc', complex_data_type),
]


def make_pylibmc_client(servers, **kw):
    tcp_type = __import__('_pylibmc').server_type_tcp
    servers_ = []
    for addr in servers:
        host, port = addr.split(':')
        port = int(port)
        servers_.append((tcp_type, host, port))
    prefix = kw.pop('prefix')
    return Prefix(__import__('pylibmc').Client(servers_, **kw), prefix)


class Pool:
    ''' adapted from pylibmc '''

    client = libmc.ClientUnsafe

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs

    def clone(self):
        return self.client(*self.args, **self.kwargs)

    def __getattr__(self, key):
        if not hasattr(libmc.Client, key):
            raise AttributeError
        result = getattr(libmc.Client, key)
        if callable(result):
            @wraps(result)
            def wrapper(*args, **kwargs):
                with self.reserve() as mc:
                    return getattr(mc, key)(*args, **kwargs)
            return wrapper
        return result


class ThreadMappedPool(Pool):
    client = libmc.Client

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clients = {}

    @property
    def current_key(self):
        return threading.current_thread().native_id

    @contextmanager
    def reserve(self):
        key = self.current_key
        mc = self.clients.pop(key, None)
        if mc is None:
            mc = self.clone()
        try:
            yield mc
        finally:
            self.clients[key] = mc


class ThreadPool(Pool):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clients = Queue()
        for _ in range(POOL_SIZE):
            self.clients.put(self.clone())

    @contextmanager
    def reserve(self):
        mc = self.clients.get()
        try:
            yield mc
        finally:
            self.clients.put(mc)


class BenchmarkThreadedClient(libmc.ThreadedClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config(libmc.MC_INITIAL_CLIENTS, POOL_SIZE)
        self.config(libmc.MC_MAX_CLIENTS, POOL_SIZE)


class FIFOThreadPool(ThreadPool):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.waiting = Queue()
        self.semaphore = Queue(1) # sorry
        self.semaphore.put(1)

    @contextmanager
    def reserve(self):
        try:
            self.semaphore.get()
            mc = self.clients.get(False)
            self.semaphore.put(1)
        except:
            channel = Queue(1)
            self.waiting.put(channel)
            self.semaphore.put(1)
            channel.get()
            mc = self.clients.get(False)
            self.semaphore.put(1)
        try:
            yield mc
        finally:
            self.semaphore.get()
            self.clients.put(mc)
            try:
                self.waiting.get(False).put(1)
            except:
                self.semaphore.put(1)


host = '127.0.0.1'
servers = ['%s:%d' % (host, 21211 + i) for i in range(N_SERVERS)]

libmc_clients = (libmc.Client, BenchmarkThreadedClient, ThreadMappedPool, ThreadPool)
libmc_kwargs = {"servers": servers, "comp_threshold": 4000}

participants = [
    Participant(
        name='pylibmc (md5 / ketama)',
        factory=lambda:
        make_pylibmc_client(servers, behaviors={
            'verify_keys': True,
            'hash': 'md5',
            'ketama': True
        }, prefix='pylibmc1')
    ),
    Participant(
        name='pylibmc (md5 / ketama / nodelay / nonblocking)',
        factory=lambda: make_pylibmc_client(
            servers,
            behaviors={
                'tcp_nodelay': True,
                'verify_keys': True,
                'hash': 'md5',
                'ketama': True,
                'no_block': True
            },
            prefix='pylibmc2'
        )
    ),
    Participant(name='python-memcached', factory=lambda: Prefix(__import__('memcache').Client(servers), 'memcache1')),
    Participant(
        name='libmc(md5 / ketama / nodelay / nonblocking, from douban)',
        factory=lambda: Prefix(__import__('libmc').Client(**libmc_kwargs), 'libmc1')
    ),
    Participant(
        name='libmc(md5 / ketama / nodelay / nonblocking / C++ thread pool, from douban)',
        factory=lambda: Prefix(BenchmarkThreadedClient(**libmc_kwargs), 'libmc2'),
        threads=NTHREADS
    ),
    # Participant(
    #     name='libmc(md5 / ketama / nodelay / nonblocking / py thread mapped, from douban)',
    #     factory=lambda: Prefix(ThreadMappedPool(**libmc_kwargs), 'libmc3'),
    #     threads=NTHREADS
    # ),
    # Participant(
    #     name='libmc(md5 / ketama / nodelay / nonblocking / py thread pool, from douban)',
    #     factory=lambda: Prefix(ThreadPool(**libmc_kwargs), 'libmc4'),
    #     threads=NTHREADS
    # ),
    # Participant(
    #     name='libmc(md5 / ketama / nodelay / nonblocking / py ordered thread pool, from douban)',
    #     factory=lambda: Prefix(FIFOThreadPool(**libmc_kwargs), 'libmc5'),
    #     threads=NTHREADS
    # ),
]

def bench(participants=participants, benchmarks=benchmarks, bench_time=BENCH_TIME):
    """Do you even lift?"""

    mcs = [p.factory() for p in participants]
    means = [[] for p in participants]
    stddevs = [[] for p in participants]
    exceptions = [[] for p in participants]

    # Have each lifter do one benchmark each
    last_fn = None
    for benchmark_name, fn, args, kwargs in benchmarks:
        logger.info('')
        logger.info('%s', benchmark_name)

        for i, (participant, mc) in enumerate(zip(participants, mcs)):
            failed = False
            def loop(sw):
                nonlocal failed
                try:
                    while sw.total() < bench_time:
                        with sw.timing():
                            fn(mc, *args, **kwargs)
                except Exception as e:
                    failed = failed or e

            try:
                # FIXME: set before bench for get
                if 'get' in fn.__name__:
                    last_fn(mc, *args, **kwargs)

                if participant.threads == 1:
                    sw = [DelayedStopwatch()]
                    loop(sw[0])
                else:
                    sw = [DelayedStopwatch() for i in range(participant.threads)]
                    ts = [spawn(loop, i) for i in sw]
                    for t in ts:
                        t.start()

                    for t in ts:
                        t.join()

                if failed:
                    raise failed

                total = sum(sw, DelayedStopwatch())
                means[i].append(total.mean())
                stddevs[i].append(total.stddev())

                logger.info(u'%76s: %s', participant.name, total)
                exceptions[i].append(None)
            except Exception as e:
                logger.info(u'%76s: %s', participant.name, "failed")
                exceptions[i].append(e)
        last_fn = fn

    return means, stddevs, exceptions


def main(args=sys.argv[1:]):
    logger.info('pylibmc: %s', pylibmc.__file__)
    logger.info('libmc: %s', libmc.__file__)
    logger.info('Running %s servers, %s threads, and a %s client pool',
                N_SERVERS, NTHREADS, POOL_SIZE)

    ps = [p for p in participants if any(p.name.startswith(arg) for arg in args)]
    ps = ps if ps else participants

    bs = benchmarks[:]

    logger.info('%d participants in %d benchmarks', len(ps), len(bs))

    means, stddevs, exceptions = bench(participants=ps, benchmarks=bs)

    print('labels =', [p.name for p in ps])
    print('benchmarks =', [b.name for b in bs])
    print('means =', means)
    print('stddevs =', stddevs)
    print('exceptions =', exceptions)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    main()
