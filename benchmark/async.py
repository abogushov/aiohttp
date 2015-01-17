import argparse
import asyncio
import collections
import gc
import random
import socket
import string
import sys
from multiprocessing import Process, set_start_method, Barrier

from scipy.stats import norm, tmean, tvar, tstd
from numpy import array, median
from numpy.ma import masked_equal

import aiohttp


PROFILE = False


def find_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('localhost', 0))
    host, port = s.getsockname()
    s.close()
    return host, port


def run_aiohttp(host, port, barrier):

    from aiohttp import web

    @asyncio.coroutine
    def test(request):
        txt = 'Hello, ' + request.match_info['name']
        return web.Response(text=txt)

    @asyncio.coroutine
    def prepare(request):
        gc.collect()
        return web.Response(text='OK')

    @asyncio.coroutine
    def stop(request):
        loop.call_later(0.1, loop.stop)
        return web.Response(text='OK')

    @asyncio.coroutine
    def init(loop):
        app = web.Application(loop=loop)
        app.router.add_route('GET', '/prepare', prepare)
        app.router.add_route('GET', '/stop', stop)
        app.router.add_route('GET', '/test/{name}', test)

        handler = app.make_handler(keep_alive=15, timeout=None)
        srv = yield from loop.create_server(handler, host, port)
        return srv, app, handler

    loop = asyncio.get_event_loop()
    srv, app, handler = loop.run_until_complete(init(loop))
    barrier.wait()

    if PROFILE:
        import cProfile

        prof = cProfile.Profile()
        prof.enable()

    loop.run_forever()
    srv.close()
    loop.run_until_complete(handler.finish_connections())
    loop.run_until_complete(srv.wait_closed())
    loop.close()

    if PROFILE:
        prof.disable()
        prof.dump_stats('out.prof')


def run_tornado(host, port, barrier):

    import tornado.ioloop
    import tornado.web

    class TestHandler(tornado.web.RequestHandler):

        def get(self, name):
            txt = 'Hello, ' + name
            self.set_header('Content-Type', 'text/plain; charset=utf-8')
            self.write(txt)

    class PrepareHandler(tornado.web.RequestHandler):

        def get(self):
            gc.collect()
            self.write('OK')

    class StopHandler(tornado.web.RequestHandler):

        def get(self):
            self.write('OK')

        def on_finish(self):
            tornado.ioloop.IOLoop.instance().stop()

    app = tornado.web.Application([
        (r'/prepare', PrepareHandler),
        (r'/stop', StopHandler),
        (r'/test/(.+)', TestHandler)])

    app.listen(port, host)
    barrier.wait()
    tornado.ioloop.IOLoop.instance().start()


def run_twisted(host, port, barrier):

    if 'bsd' in sys.platform or sys.platform.startswith('darwin'):
        from twisted.internet import kqreactor
        kqreactor.install()
    elif sys.platform in ['win32']:
        from twisted.internet.iocpreactor import reactor as iocpreactor
        iocpreactor.install()
    elif sys.platform.startswith('linux'):
        from twisted.internet import epollreactor
        epollreactor.install()
    else:
        from twisted.internet import default as defaultreactor
        defaultreactor.install()

    from twisted.web.server import Site
    from twisted.web.resource import Resource
    from twisted.internet import reactor

    class TestResource(Resource):

        def __init__(self, name):
            super().__init__()
            self.name = name
            self.isLeaf = name is not None

        def render_GET(self, request):
            txt = 'Hello, ' + self.name
            request.setHeader(b'Content-Type', b'text/plain; charset=utf-8')
            return txt.encode('utf8')

        def getChild(self, name, request):
            return TestResource(name=name.decode('utf-8'))

    class PrepareResource(Resource):

        isLeaf = True

        def render_GET(self, request):
            gc.collect()
            return b'OK'

    class StopResource(Resource):

        isLeaf = True

        def render_GET(self, request):
            reactor.callLater(0.1, reactor.stop)
            return b'OK'

    root = Resource()
    root.putChild(b'test', TestResource(None))
    root.putChild(b'prepare', PrepareResource())
    root.putChild(b'stop', StopResource())
    site = Site(root)
    reactor.listenTCP(port, site, interface=host)
    barrier.wait()

    reactor.run()


@asyncio.coroutine
def attack(count, concurrency, connector, loop, url):
    sem = asyncio.Semaphore(concurrency)
    request = aiohttp.request

    @asyncio.coroutine
    def do_bomb(rnd):
        real_url = url + '/test/' + rnd
        with (yield from sem):
            t1 = loop.time()
            resp = yield from request('GET', real_url,
                                      connector=connector, loop=loop)
            assert resp.status == 200, resp.status
            if 'text/plain; charset=utf-8' != resp.headers['Content-Type']:
                raise AssertionError('Invalid Content-Type: %r' % resp.headers)
            body = yield from resp.text()
            assert body == ('Hello, ' + rnd), rnd
            t2 = loop.time()
            return t2 - t1

    bombs = []

    for i in range(count):
        rnd = ''.join(random.sample(string.ascii_letters, 16))
        bombs.append(asyncio.async(do_bomb(rnd)))

    data = (yield from asyncio.gather(*bombs))
    return data


@asyncio.coroutine
def run(test, count, concurrency, *, loop, verbose):
    if verbose:
        print("Prepare")
    else:
        print('.', end='', flush=True)
    host, port = find_port()
    barrier = Barrier(2)
    server = Process(target=test, args=(host, port, barrier))
    server.start()
    barrier.wait()

    url = 'http://{}:{}'.format(host, port)

    connector = aiohttp.TCPConnector(loop=loop)

    for i in range(10):
        # make server hot
        resp = yield from aiohttp.request('GET', url+'/prepare',
                                          connector=connector, loop=loop)
        assert resp.status == 200, resp.status
        resp.release()

    if verbose:
        test_name = test.__name__
        print("Attack", test_name)
    data = yield from attack(count, concurrency, connector, loop, url)
    if verbose:
        print("Done")

    resp = yield from aiohttp.request('GET', url+'/stop',
                                      connector=connector, loop=loop)
    assert resp.status == 200, resp.status
    resp.release()
    server.join()
    return data


def main(argv):
    args = ARGS.parse_args()

    count = args.count
    concurrency = args.concurrency
    verbose = args.verbose
    tries = args.tries

    loop = asyncio.get_event_loop()
    suite = [run_aiohttp, run_tornado, run_twisted]

    suite *= tries
    random.shuffle(suite)

    results = collections.defaultdict(list)
    for test in suite:
        test_name = test.__name__

        times = loop.run_until_complete(run(test, count, concurrency,
                                            loop=loop, verbose=verbose))
        results[test_name].append(times)

    print()

    for test_name in sorted(results):

        data = array(results[test_name])
        trimmed = masked_equal(data, 0)

        rps = trimmed.size / trimmed
        rps_mean = tmean(rps)
        times = trimmed * 1000000 / trimmed.size
        times_mean = tmean(times)
        times_stdev = tstd(times)
        times_median = median(times)
        print('Results for', test_name)
        print('RPS: {:d},\tmean: {:.3f} μs,'
              '\tstandard deviation {:.3f} μs\tmedian {:.3f} μs'
              .format(int(rps_mean),
                      times_mean,
                      times_stdev,
                      times_median))
    return 0

ARGS = argparse.ArgumentParser(description="Run benchmark.")
ARGS.add_argument(
    '-t', '--tries', action="store",
    nargs='?', type=int, default=5,
    help='count of tries (default: `%(default)s`)')
ARGS.add_argument(
    '-n', '--count', action="store",
    nargs='?', type=int, default=10000,
    help='requests count (default: `%(default)s`)')
ARGS.add_argument(
    '-c', '--concurrency', action="store",
    nargs='?', type=int, default=500,
    help='count of parallel requests (default: `%(default)s`)')
ARGS.add_argument(
    '-p', '--plot-file-name', action="store",
    type=str, default=None,
    dest='plot_file_name',
    help='file name for plot (default: `%(default)s`)')
ARGS.add_argument(
    '-v', '--verbose', action="count", default=0,
    help='verbosity level (default: `%(default)s`)')


if __name__ == '__main__':
    set_start_method('spawn')
    sys.exit(main(sys.argv))