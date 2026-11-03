from concurrent.futures import ThreadPoolExecutor
from threading import Event
from pathlib import Path
import sys
from saiworks.tools.process_lifecycle import supports_process_groups

import pytest

from saiworks.mcp.client import TransportBackedMCPClient, MCPClientError
from saiworks.mcp.config import MCPServerConfig
from saiworks.mcp.manager import MCPManager
from saiworks.mcp.transport import MCPTransportClosed, StdioTransport, StreamableHttpTransport, SSETransport


def config(transport='stdio'):
    return MCPServerConfig(name='test', transport=transport, url='http://localhost:1/mcp')


class Client:
    def __init__(self, fail=False):
        self.fail = fail
        self.close_calls = 0
    def initialize(self):
        pass
    def close(self):
        self.close_calls += 1
        if self.fail:
            self.fail = False
            raise OSError('close failed')


def test_manager_retains_failed_cleanup_and_blocks_replacement_until_retry():
    client = Client(fail=True)
    created = []
    def factory(_):
        created.append(client)
        return client
    manager = MCPManager({'test': config()}, factory)
    manager.get_client('test')
    with pytest.raises(ExceptionGroup):
        manager.close()
    with pytest.raises(MCPTransportClosed):
        manager.get_client('test')
    assert len(created) == 1
    manager.close()
    assert client.close_calls == 2


def test_initialization_cannot_publish_after_manager_generation_is_closed():
    entered, release = Event(), Event()
    class SlowClient(Client):
        def initialize(self):
            entered.set()
            assert release.wait(3)
    client = SlowClient()
    manager = MCPManager({'test': config()}, lambda _: client)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(manager.get_client, 'test')
        assert entered.wait(3)
        try:
            manager.close()
        finally:
            release.set()
        with pytest.raises(MCPTransportClosed):
            future.result(timeout=3)
    assert manager._clients == {}
    assert client.close_calls >= 1


def test_retired_request_cannot_start_new_client_and_stale_failure_cannot_close_replacement():
    clients = [Client(), Client()]
    manager = MCPManager({'test': config()}, lambda _: clients.pop(0))
    generation = manager.capture_generation()
    old = manager.get_client('test')
    manager.close()
    with pytest.raises(MCPTransportClosed):
        manager.get_client('test', expected_generation=generation)
    new = manager.get_client('test')
    manager.invalidate('test', expected_client=old)
    assert manager.get_client('test') is new
    assert new.close_calls == 0
    manager.close()


@pytest.mark.parametrize('cls,transport', [(StdioTransport, 'stdio'), (StreamableHttpTransport, 'streamable_http'), (SSETransport, 'sse')])
def test_transport_close_is_terminal_before_any_connection_attempt(cls, transport):
    instance = cls(config(transport))
    instance.close()
    with pytest.raises(MCPTransportClosed):
        instance.connect()
    with pytest.raises(MCPTransportClosed):
        instance.request('tools/call', {})


def test_closed_client_cannot_reinitialize_even_if_underlying_transport_is_reusable():
    class Transport:
        connects = 0
        def connect(self):
            self.connects += 1
        def close(self):
            pass
    transport = Transport()
    client = TransportBackedMCPClient(config(), transport)
    client.close()
    with pytest.raises(MCPClientError) as failure:
        client.initialize()
    assert failure.value.error_code == 'mcp_transport_closed'
    assert transport.connects == 0


def test_stdio_close_wakes_inflight_request_without_restarting_process(tmp_path, monkeypatch):
    import sys
    script = tmp_path / 'slow.py'
    script.write_text('import sys,time\nsys.stdin.readline()\ntime.sleep(30)\n')
    transport = StdioTransport(MCPServerConfig(name='slow', transport='stdio', command=sys.executable,
                                              args=(str(script),), read_timeout=30))
    dispatched = Event()
    original = StdioTransport._write_message
    def write(self, payload):
        original(self, payload)
        dispatched.set()
    monkeypatch.setattr(StdioTransport, '_write_message', write)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(transport.request, 'tools/call', {})
        assert dispatched.wait(3)
        process = transport._process
        try:
            transport.close()
            with pytest.raises(MCPTransportClosed):
                future.result(timeout=3)
            assert process.poll() is not None
            with pytest.raises(MCPTransportClosed):
                transport.connect()
        finally:
            transport.close()


def test_sse_close_wakes_response_waiter_without_waiting_for_read_timeout():
    transport = SSETransport(config('sse'))
    entered = Event()
    def wait():
        entered.set()
        return transport._wait_for_response(1)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(wait)
        assert entered.wait(3)
        transport.close()
        with pytest.raises(MCPTransportClosed):
            future.result(timeout=3)


def test_old_adapter_request_cannot_reconnect_after_cleanup(tmp_path):
    from saiworks.mcp.adapter import MCPToolAdapter
    from saiworks.mcp.types import MCPToolDescriptor
    from saiworks.tools.base import ToolContext
    from saiworks.types import ToolAction
    made = []
    def factory(_):
        made.append(Client())
        return made[-1]
    manager = MCPManager({'test': config()}, factory)
    class Logger:
        def record(self, name, payload):
            if name == 'mcp.tool.call':
                manager.close()
    tool = MCPToolAdapter(config(), manager, Logger()).adapt(MCPToolDescriptor(server_name='test', tool_name='write'))
    result = tool.run(ToolAction(tool.name), ToolContext(cwd=str(tmp_path)))
    assert not result.success
    assert result.error_code == 'mcp_transport_closed'
    assert made == []


def test_sse_cleanup_failure_does_not_skip_other_connections_and_retains_failed_one():
    class Connection:
        def __init__(self, fail=False):
            self.fail, self.calls = fail, 0
        def close(self):
            self.calls += 1
            if self.fail:
                self.fail = False
                raise OSError('close failed')
    failed, post, stream = Connection(True), Connection(), Connection()
    transport = SSETransport(config('sse'))
    transport._post_connections.extend([failed, post])
    transport._stream_connection = stream
    with pytest.raises(ExceptionGroup):
        transport.close()
    assert [failed.calls, post.calls, stream.calls] == [1, 1, 1]
    assert transport._post_connections == [failed]
    assert transport._stream_connection is None
    transport.close()
    assert [failed.calls, post.calls, stream.calls] == [2, 1, 1]


@pytest.mark.parametrize('cls,transport,send_name', [
    (StreamableHttpTransport, 'streamable_http', '_send'),
    (StdioTransport, 'stdio', '_write_message'),
    (SSETransport, 'sse', '_post_message'),
])
def test_transport_serializes_request_and_notification(cls, transport, send_name, monkeypatch):
    entered, release, second_attempted, second_dispatched = Event(), Event(), Event(), Event()
    calls = []
    def send(self, payload, **kwargs):
        calls.append(payload['method'])
        if payload['method'] == 'first':
            entered.set()
            assert release.wait(3)
        else:
            second_dispatched.set()
        return {'jsonrpc': '2.0', 'id': payload.get('id'), 'result': {}}
    monkeypatch.setattr(cls, send_name, send)
    if cls is not StreamableHttpTransport:
        monkeypatch.setattr(cls, '_wait_for_response', lambda self, request_id: {'jsonrpc': '2.0', 'id': request_id, 'result': {}})
    instance = cls(config(transport))
    def notify():
        second_attempted.set()
        instance.notify('second')
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(instance.request, 'first')
        assert entered.wait(3)
        second = executor.submit(notify)
        assert second_attempted.wait(3)
        try:
            assert not second_dispatched.wait(0.05)
        finally:
            release.set()
        first.result(timeout=3)
        second.result(timeout=3)
    assert calls == ['first', 'second']
    instance.close()


@pytest.mark.parametrize('cls,transport', [(StreamableHttpTransport, 'streamable_http'), (SSETransport, 'sse')])
def test_connect_failure_retains_connection_when_cleanup_itself_fails(cls, transport, monkeypatch):
    import socket
    from saiworks.mcp.transport import MCPTransportTimeout
    class Connection:
        closes = 0
        def connect(self):
            raise socket.timeout('connect timed out')
        def close(self):
            self.closes += 1
            if self.closes == 1:
                raise OSError('close failed')
    connection = Connection()
    if cls is StreamableHttpTransport:
        import saiworks.mcp.transport as module
        monkeypatch.setattr(module.http.client, 'HTTPConnection', lambda *args, **kwargs: connection)
    else:
        monkeypatch.setattr(cls, '_build_connection', lambda *args, **kwargs: connection)
    instance = cls(config(transport))
    with pytest.raises(MCPTransportTimeout):
        instance.connect()
    owned = instance._connection if cls is StreamableHttpTransport else instance._stream_connection
    assert owned is connection
    instance.close()
    assert connection.closes == 2
    owned = instance._connection if cls is StreamableHttpTransport else instance._stream_connection
    assert owned is None


def test_queued_exchange_rechecks_closed_state_before_dispatch(monkeypatch):
    entered, release, attempted = Event(), Event(), Event()
    calls = []
    def send(self, payload, **kwargs):
        calls.append(payload['method'])
        entered.set()
        assert release.wait(3)
        return {'jsonrpc': '2.0', 'id': payload.get('id'), 'result': {}}
    monkeypatch.setattr(StreamableHttpTransport, '_send', send)
    transport = StreamableHttpTransport(config('streamable_http'))
    def second():
        attempted.set()
        transport.notify('queued')
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(transport.request, 'active')
        assert entered.wait(3)
        queued = executor.submit(second)
        assert attempted.wait(3)
        try:
            transport.close()
        finally:
            release.set()
        first.result(timeout=3)
        with pytest.raises(MCPTransportClosed):
            queued.result(timeout=3)
    assert calls == ['active']


def test_concurrent_client_initialization_performs_one_handshake():
    from saiworks.mcp.client import DEFAULT_PROTOCOL_VERSION
    entered, release = Event(), Event()
    class Transport:
        requests = 0
        def connect(self):
            pass
        def request(self, method, params):
            self.requests += 1
            entered.set()
            assert release.wait(3)
            return {'jsonrpc': '2.0', 'id': self.requests, 'result': {
                'protocolVersion': DEFAULT_PROTOCOL_VERSION, 'capabilities': {}, 'serverInfo': {}}}
        def notify(self, method, params):
            pass
        def close(self):
            pass
    transport = Transport()
    client = TransportBackedMCPClient(config(), transport)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(client.initialize)
        assert entered.wait(3)
        second = executor.submit(client.initialize)
        release.set()
        first.result(timeout=3)
        second.result(timeout=3)
    assert transport.requests == 1
    client.close()


@pytest.mark.skipif(not supports_process_groups(), reason='Requires local Linux group ownership verification')
def test_mcp_stdio_cleanup_terminates_child_ignoring_term(tmp_path):
    from saiworks.mcp.config import MCPServerConfig
    from saiworks.mcp.transport import StdioTransport
    script = tmp_path / 'server.py'
    child = tmp_path / 'child.py'
    child.write_text("import signal,time,pathlib\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\npathlib.Path('ready').write_text('ready')\ntime.sleep(30)\n")
    script.write_text(
        'import json,sys,subprocess,pathlib,time,os\n'
        f'os.chdir({str(tmp_path)!r})\n'
        'request=json.loads(sys.stdin.readline())\n'
        f'child=subprocess.Popen([sys.executable,{str(child)!r}])\n'
        'while not pathlib.Path("ready").exists(): time.sleep(0.01)\n'
        'print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{"pid":child.pid}}),flush=True)\n'
        'time.sleep(30)\n'
    )
    transport = StdioTransport(MCPServerConfig(name='owned', transport='stdio', command=sys.executable,
                                               args=(str(script),), read_timeout=3))
    result = transport.request('spawn')
    pid = result['result']['pid']
    try:
        transport.close()
        try:
            data = Path(f'/proc/{pid}/stat').read_text()
        except FileNotFoundError:
            pass
        else:
            assert data[data.rfind(')') + 2:].split()[0] in {'Z', 'X'}
    finally:
        transport.close()
