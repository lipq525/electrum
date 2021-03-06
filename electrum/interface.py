#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 thomasv@gitorious
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import os
import re
import ssl
import sys
import traceback
import asyncio
from typing import Tuple, Union
from collections import defaultdict

import aiorpcx
from aiorpcx import ClientSession, Notification

from .util import PrintError, aiosafe, bfh, AIOSafeSilentException, SilentTaskGroup
from . import util
from . import x509
from . import pem
from .version import ELECTRUM_VERSION, PROTOCOL_VERSION
from . import blockchain
from .blockchain import Blockchain
from . import constants


class NotificationSession(ClientSession):

    def __init__(self, *args, **kwargs):
        super(NotificationSession, self).__init__(*args, **kwargs)
        self.subscriptions = defaultdict(list)
        self.cache = {}
        self.in_flight_requests_semaphore = asyncio.Semaphore(100)
        # disable bandwidth limiting (used by superclass):
        self.bw_limit = 0

    async def handle_request(self, request):
        # note: if server sends malformed request and we raise, the superclass
        # will catch the exception, count errors, and at some point disconnect
        if isinstance(request, Notification):
            params, result = request.args[:-1], request.args[-1]
            key = self.get_index(request.method, params)
            if key in self.subscriptions:
                self.cache[key] = result
                for queue in self.subscriptions[key]:
                    await queue.put(request.args)
            else:
                assert False, request.method

    async def send_request(self, *args, timeout=-1, **kwargs):
        # note: the timeout starts after the request touches the wire!
        if timeout == -1:
            timeout = 20 if not self.proxy else 30
        # note: the semaphore implementation guarantees no starvation
        async with self.in_flight_requests_semaphore:
            try:
                return await asyncio.wait_for(
                    super().send_request(*args, **kwargs),
                    timeout)
            except asyncio.TimeoutError as e:
                raise GracefulDisconnect('request timed out: {}'.format(args)) from e

    async def subscribe(self, method, params, queue):
        # note: until the cache is written for the first time,
        # each 'subscribe' call might make a request on the network.
        key = self.get_index(method, params)
        self.subscriptions[key].append(queue)
        if key in self.cache:
            result = self.cache[key]
        else:
            result = await self.send_request(method, params)
            self.cache[key] = result
        await queue.put(params + [result])

    def unsubscribe(self, queue):
        """Unsubscribe a callback to free object references to enable GC."""
        # note: we can't unsubscribe from the server, so we keep receiving
        # subsequent notifications
        for v in self.subscriptions.values():
            if queue in v:
                v.remove(queue)

    @classmethod
    def get_index(cls, method, params):
        """Hashable index for subscriptions and cache"""
        return str(method) + repr(params)


class GracefulDisconnect(Exception): pass


class ErrorParsingSSLCert(Exception): pass


class ErrorGettingSSLCertFromServer(Exception): pass



def deserialize_server(server_str: str) -> Tuple[str, str, str]:
    # host might be IPv6 address, hence do rsplit:
    host, port, protocol = str(server_str).rsplit(':', 2)
    if protocol not in ('s', 't'):
        raise ValueError('invalid network protocol: {}'.format(protocol))
    int(port)  # Throw if cannot be converted to int
    if not (0 < int(port) < 2**16):
        raise ValueError('port {} is out of valid range'.format(port))
    return host, port, protocol


def serialize_server(host: str, port: Union[str, int], protocol: str) -> str:
    return str(':'.join([host, str(port), protocol]))


class Interface(PrintError):

    def __init__(self, network, server, config_path, proxy):
        self.exception = None
        self.ready = asyncio.Future()
        self.server = server
        self.host, self.port, self.protocol = deserialize_server(self.server)
        self.port = int(self.port)
        self.config_path = config_path
        self.cert_path = os.path.join(self.config_path, 'certs', self.host)
        self.blockchain = None
        self._requested_chunks = set()
        self.network = network
        self._set_proxy(proxy)

        self.tip_header = None
        self.tip = 0

        # TODO combine?
        self.fut = asyncio.get_event_loop().create_task(self.run())
        self.group = SilentTaskGroup()

    def diagnostic_name(self):
        return self.host

    def _set_proxy(self, proxy: dict):
        if proxy:
            username, pw = proxy.get('user'), proxy.get('password')
            if not username or not pw:
                auth = None
            else:
                auth = aiorpcx.socks.SOCKSUserAuth(username, pw)
            if proxy['mode'] == "socks4":
                self.proxy = aiorpcx.socks.SOCKSProxy((proxy['host'], int(proxy['port'])), aiorpcx.socks.SOCKS4a, auth)
            elif proxy['mode'] == "socks5":
                self.proxy = aiorpcx.socks.SOCKSProxy((proxy['host'], int(proxy['port'])), aiorpcx.socks.SOCKS5, auth)
            else:
                raise NotImplementedError  # http proxy not available with aiorpcx
        else:
            self.proxy = None

    async def is_server_ca_signed(self, sslc):
        try:
            await self.open_session(sslc, exit_early=True)
        except ssl.SSLError as e:
            assert e.reason == 'CERTIFICATE_VERIFY_FAILED'
            return False
        return True

    async def _try_saving_ssl_cert_for_first_time(self, ca_ssl_context):
        try:
            ca_signed = await self.is_server_ca_signed(ca_ssl_context)
        except (OSError, aiorpcx.socks.SOCKSFailure) as e:
            raise ErrorGettingSSLCertFromServer(e) from e
        if ca_signed:
            with open(self.cert_path, 'w') as f:
                # empty file means this is CA signed, not self-signed
                f.write('')
        else:
            await self.save_certificate()

    def _is_saved_ssl_cert_available(self):
        if not os.path.exists(self.cert_path):
            return False
        with open(self.cert_path, 'r') as f:
            contents = f.read()
        if contents == '':  # CA signed
            return True
        # pinned self-signed cert
        try:
            b = pem.dePem(contents, 'CERTIFICATE')
        except SyntaxError as e:
            self.print_error("error parsing already saved cert:", e)
            raise ErrorParsingSSLCert(e) from e
        try:
            x = x509.X509(b)
        except Exception as e:
            self.print_error("error parsing already saved cert:", e)
            raise ErrorParsingSSLCert(e) from e
        try:
            x.check_date()
            return True
        except x509.CertificateError as e:
            self.print_error("certificate has expired:", e)
            os.unlink(self.cert_path)  # delete pinned cert only in this case
            return False

    async def _get_ssl_context(self):
        if self.protocol != 's':
            # using plaintext TCP
            return None

        # see if we already have cert for this server; or get it for the first time
        ca_sslc = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        if not self._is_saved_ssl_cert_available():
            await self._try_saving_ssl_cert_for_first_time(ca_sslc)
        # now we have a file saved in our certificate store
        siz = os.stat(self.cert_path).st_size
        if siz == 0:
            # CA signed cert
            sslc = ca_sslc
        else:
            # pinned self-signed cert
            sslc = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self.cert_path)
            sslc.check_hostname = 0
        return sslc

    def handle_graceful_disconnect(func):
        async def wrapper_func(self, *args, **kwargs):
            try:
                return await func(self, *args, **kwargs)
            except GracefulDisconnect as e:
                self.print_error("disconnecting gracefully. {}".format(e))
                self.exception = e
        return wrapper_func

    @aiosafe
    @handle_graceful_disconnect
    async def run(self):
        try:
            ssl_context = await self._get_ssl_context()
        except (ErrorParsingSSLCert, ErrorGettingSSLCertFromServer) as e:
            self.exception = e
            return
        try:
            await self.open_session(ssl_context, exit_early=False)
        except (asyncio.CancelledError, OSError, aiorpcx.socks.SOCKSFailure) as e:
            self.print_error('disconnecting due to: {} {}'.format(e, type(e)))
            self.exception = e
            return
        # should never get here (can only exit via exception)
        assert False

    def mark_ready(self):
        if self.ready.cancelled():
            raise GracefulDisconnect('conn establishment was too slow; *ready* future was cancelled')
        if self.ready.done():
            return

        assert self.tip_header
        chain = blockchain.check_header(self.tip_header)
        if not chain:
            self.blockchain = blockchain.blockchains[0]
        else:
            self.blockchain = chain
        assert self.blockchain is not None

        self.print_error("set blockchain with height", self.blockchain.height())

        self.ready.set_result(1)

    async def save_certificate(self):
        if not os.path.exists(self.cert_path):
            # we may need to retry this a few times, in case the handshake hasn't completed
            for _ in range(10):
                dercert = await self.get_certificate()
                if dercert:
                    self.print_error("succeeded in getting cert")
                    with open(self.cert_path, 'w') as f:
                        cert = ssl.DER_cert_to_PEM_cert(dercert)
                        # workaround android bug
                        cert = re.sub("([^\n])-----END CERTIFICATE-----","\\1\n-----END CERTIFICATE-----",cert)
                        f.write(cert)
                        # even though close flushes we can't fsync when closed.
                        # and we must flush before fsyncing, cause flush flushes to OS buffer
                        # fsync writes to OS buffer to disk
                        f.flush()
                        os.fsync(f.fileno())
                    break
                await asyncio.sleep(1)
            else:
                raise Exception("could not get certificate")

    async def get_certificate(self):
        sslc = ssl.SSLContext()
        try:
            async with aiorpcx.ClientSession(self.host, self.port, ssl=sslc, proxy=self.proxy) as session:
                return session.transport._ssl_protocol._sslpipe._sslobj.getpeercert(True)
        except ValueError:
            return None

    async def get_block_header(self, height, assert_mode):
        # use lower timeout as we usually have network.bhi_lock here
        self.print_error('requesting block header {} in mode {}'.format(height, assert_mode))
        timeout = 5 if not self.proxy else 10
        res = await self.session.send_request('blockchain.block.header', [height], timeout=timeout)
        return blockchain.deserialize_header(bytes.fromhex(res), height)

    async def request_chunk(self, height, tip=None, *, can_return_early=False):
        index = height // 2016
        if can_return_early and index in self._requested_chunks:
            return
        self.print_error("requesting chunk from height {}".format(height))
        size = 2016
        if tip is not None:
            size = min(size, tip - index * 2016 + 1)
            size = max(size, 0)
        try:
            self._requested_chunks.add(index)
            res = await self.session.send_request('blockchain.block.headers', [index * 2016, size])
        finally:
            try: self._requested_chunks.remove(index)
            except KeyError: pass
        conn = self.blockchain.connect_chunk(index, res['hex'])
        if not conn:
            return conn, 0
        return conn, res['count']

    async def open_session(self, sslc, exit_early):
        header_queue = asyncio.Queue()
        self.session = NotificationSession(self.host, self.port, ssl=sslc, proxy=self.proxy)
        async with self.session as session:
            try:
                ver = await session.send_request('server.version', [ELECTRUM_VERSION, PROTOCOL_VERSION])
            except aiorpcx.jsonrpc.RPCError as e:
                raise GracefulDisconnect(e)  # probably 'unsupported protocol version'
            if exit_early:
                return
            self.print_error("connection established. version: {}".format(ver))
            await session.subscribe('blockchain.headers.subscribe', [], header_queue)

            async with self.group as group:
                await group.spawn(self.ping())
                await group.spawn(self.run_fetch_blocks(header_queue))
                await group.spawn(self.monitor_connection())
                # NOTE: group.__aexit__ will be called here; this is needed to notice exceptions in the group!

    async def monitor_connection(self):
        while True:
            await asyncio.sleep(1)
            if not self.session or self.session.is_closing():
                raise GracefulDisconnect('server closed session')

    async def ping(self):
        while True:
            await asyncio.sleep(300)
            await self.session.send_request('server.ping')

    def close(self):
        self.fut.cancel()
        asyncio.get_event_loop().create_task(self.group.cancel_remaining())

    async def run_fetch_blocks(self, header_queue):
        while True:
            self.network.notify('updated')
            item = await header_queue.get()
            raw_header = item[0]
            height = raw_header['height']
            header = blockchain.deserialize_header(bfh(raw_header['hex']), height)
            self.tip_header = header
            self.tip = height
            if self.tip < constants.net.max_checkpoint():
                raise GracefulDisconnect('server tip below max checkpoint')
            self.mark_ready()
            async with self.network.bhi_lock:
                if self.blockchain.height() >= height and self.blockchain.check_header(header):
                    # another interface amended the blockchain
                    self.print_error("skipping header", height)
                    continue
                _, height = await self.step(height, header)
                # in the simple case, height == self.tip+1
                if height <= self.tip:
                    await self.sync_until(height)
            self.network.switch_lagging_interface()

    async def sync_until(self, height, next_height=None):
        if next_height is None:
            next_height = self.tip
        last = None
        while last is None or height <= next_height:
            prev_last, prev_height = last, height
            if next_height > height + 10:
                could_connect, num_headers = await self.request_chunk(height, next_height)
                if not could_connect:
                    if height <= constants.net.max_checkpoint():
                        raise Exception('server chain conflicts with checkpoints or genesis')
                    last, height = await self.step(height)
                    continue
                self.network.notify('updated')
                height = (height // 2016 * 2016) + num_headers
                assert height <= next_height+1, (height, self.tip)
                last = 'catchup'
            else:
                last, height = await self.step(height)
            assert (prev_last, prev_height) != (last, height), 'had to prevent infinite loop in interface.sync_until'
        return last, height

    async def step(self, height, header=None):
        assert height != 0
        assert height <= self.tip, (height, self.tip)
        if header is None:
            header = await self.get_block_header(height, 'catchup')

        chain = blockchain.check_header(header) if 'mock' not in header else header['mock']['check'](header)
        if chain:
            self.blockchain = chain if isinstance(chain, Blockchain) else self.blockchain
            return 'catchup', height+1

        can_connect = blockchain.can_connect(header) if 'mock' not in header else header['mock']['connect'](height)
        if not can_connect:
            self.print_error("can't connect", height)
            height, header, bad, bad_header = await self._search_headers_backwards(height, header)
            chain = blockchain.check_header(header) if 'mock' not in header else header['mock']['check'](header)
            can_connect = blockchain.can_connect(header) if 'mock' not in header else header['mock']['connect'](height)
            assert chain or can_connect
        if can_connect:
            self.print_error("could connect", height)
            height += 1
            if isinstance(can_connect, Blockchain):  # not when mocking
                self.blockchain = can_connect
                self.blockchain.save_header(header)
            return 'catchup', height

        good, height, bad, bad_header = await self._search_headers_binary(height, bad, bad_header, chain)
        return await self._resolve_potential_chain_fork_given_forkpoint(good, height, bad, bad_header)

    async def _search_headers_binary(self, height, bad, bad_header, chain):
        self.blockchain = chain if isinstance(chain, Blockchain) else self.blockchain
        good = height
        while True:
            assert good < bad, (good, bad)
            height = (good + bad) // 2
            self.print_error("binary step. good {}, bad {}, height {}".format(good, bad, height))
            header = await self.get_block_header(height, 'binary')
            chain = blockchain.check_header(header) if 'mock' not in header else header['mock']['check'](header)
            if chain:
                self.blockchain = chain if isinstance(chain, Blockchain) else self.blockchain
                good = height
            else:
                bad = height
                bad_header = header
            if good + 1 == bad:
                break

        mock = bad_header and 'mock' in bad_header and bad_header['mock']['connect'](height)
        real = not mock and self.blockchain.can_connect(bad_header, check_height=False)
        if not real and not mock:
            raise Exception('unexpected bad header during binary: {}'.format(bad_header))
        self.print_error("binary search exited. good {}, bad {}".format(good, bad))
        return good, height, bad, bad_header

    async def _resolve_potential_chain_fork_given_forkpoint(self, good, height, bad, bad_header):
        branch = blockchain.blockchains.get(bad)
        if branch is not None:
            self.print_error("existing fork found at bad height {}".format(bad))
            ismocking = type(branch) is dict
            # FIXME: it does not seem sufficient to check that the branch
            # contains the bad_header. what if self.blockchain doesn't?
            # the chains shouldn't be joined then. observe the incorrect
            # joining on regtest with a server that has a fork of height
            # one. the problem is observed only if forking is not during
            # electrum runtime
            if not ismocking and branch.check_header(bad_header) \
                    or ismocking and branch['check'](bad_header):
                self.print_error('joining chain', bad)
                height += 1
                return 'join', height
            else:
                height = bad + 1
                if ismocking:
                    self.print_error("TODO replace blockchain")
                    return 'conflict', height
                self.print_error('forkpoint conflicts with existing fork', branch.path())
                branch.write(b'', 0)
                branch.save_header(bad_header)
                self.blockchain = branch
                return 'conflict', height
        else:
            bh = self.blockchain.height()
            self.print_error("no existing fork yet at bad height {}. local chain height: {}".format(bad, bh))
            if bh > good:
                forkfun = self.blockchain.fork
                if 'mock' in bad_header:
                    chain = bad_header['mock']['check'](bad_header)
                    forkfun = bad_header['mock']['fork'] if 'fork' in bad_header['mock'] else forkfun
                else:
                    chain = self.blockchain.check_header(bad_header)
                if not chain:
                    b = forkfun(bad_header)
                    with blockchain.blockchains_lock:
                        assert bad not in blockchain.blockchains, (bad, list(blockchain.blockchains))
                        blockchain.blockchains[bad] = b
                    self.blockchain = b
                    height = b.forkpoint + 1
                    assert b.forkpoint == bad
                return 'fork', height
            else:
                assert bh == good
                if bh < self.tip:
                    self.print_error("catching up from %d" % (bh + 1))
                    height = bh + 1
                return 'no_fork', height

    async def _search_headers_backwards(self, height, header):
        async def iterate():
            nonlocal height, header
            checkp = False
            if height <= constants.net.max_checkpoint():
                height = constants.net.max_checkpoint()
                checkp = True
            header = await self.get_block_header(height, 'backward')
            chain = blockchain.check_header(header) if 'mock' not in header else header['mock']['check'](header)
            can_connect = blockchain.can_connect(header) if 'mock' not in header else header['mock']['connect'](height)
            if chain or can_connect:
                return False
            if checkp:
                raise Exception("server chain conflicts with checkpoints")
            return True

        bad, bad_header = height, header
        with blockchain.blockchains_lock: chains = list(blockchain.blockchains.values())
        local_max = max([0] + [x.height() for x in chains]) if 'mock' not in header else float('inf')
        height = min(local_max + 1, height - 1)
        while await iterate():
            bad, bad_header = height, header
            delta = self.tip - height
            height = self.tip - 2 * delta
        self.print_error("exiting backward mode at", height)
        return height, header, bad, bad_header


def check_cert(host, cert):
    try:
        b = pem.dePem(cert, 'CERTIFICATE')
        x = x509.X509(b)
    except:
        traceback.print_exc(file=sys.stdout)
        return

    try:
        x.check_date()
        expired = False
    except:
        expired = True

    m = "host: %s\n"%host
    m += "has_expired: %s\n"% expired
    util.print_msg(m)


# Used by tests
def _match_hostname(name, val):
    if val == name:
        return True

    return val.startswith('*.') and name.endswith(val[1:])


def test_certificates():
    from .simple_config import SimpleConfig
    config = SimpleConfig()
    mydir = os.path.join(config.path, "certs")
    certs = os.listdir(mydir)
    for c in certs:
        p = os.path.join(mydir,c)
        with open(p, encoding='utf-8') as f:
            cert = f.read()
        check_cert(c, cert)

if __name__ == "__main__":
    test_certificates()
