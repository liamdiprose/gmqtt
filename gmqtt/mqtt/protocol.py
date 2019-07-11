import asyncio
import logging
import struct

from . import package
from .constants import MQTTv50, MQTTCommands

logger = logging.getLogger(__name__)


class BaseMQTTProtocol:
    def __init__(self, buffer_size=2**16, loop=None):
        self._connection = None

        self._connected = asyncio.Event(loop=loop)

    def set_connection(self, conn):
        self._connection = conn

    def _parse_packet(self):
        raise NotImplementedError

    def connection_made(self, connection):
        logger.info('[CONNECTION MADE]')
        self._connection = connection
        self._connected.set()

    def write_data(self, data: bytes):
        if not self._connection.is_closing():
            self._connection.write(data)
        else:
            logger.warning('[TRYING WRITE TO CLOSED SOCKET]')

    def connection_lost(self, exc):
        self._connected.clear()

        if exc:
            logger.warning('[EXC: CONN LOST]', exc_info=exc)
        else:
            logger.info('[CONN CLOSE NORMALLY]')

    async def read(self, n=-1):
        bs = await self._connection.read(n=n)

        # so we don't receive anything but connection is not closed -
        # let's close it manually
        if not bs and not self._connection.is_closing():
            self._connection.close()
            # self.connection_lost(ConnectionResetError())
            raise ConnectionResetError("Reset connection manually.")
        return bs


class MQTTProtocol(BaseMQTTProtocol):
    proto_name = b'MQTT'
    proto_ver = MQTTv50

    def __init__(self, *args, **kwargs):
        super(MQTTProtocol, self).__init__(*args, **kwargs)
        self._queue = asyncio.Queue()

        self._disconnect = asyncio.Event()

        self._read_loop_future = None

    def connection_made(self, connection):
        super().connection_made(connection)
        self._read_loop_future = asyncio.ensure_future(self._read_loop())

    async def send_auth_package(self, client_id, username, password, clean_session, keepalive,
                                will_message=None, **kwargs):
        pkg = package.LoginPackageFactor.build_package(client_id, username, password, clean_session,
                                                       keepalive, self, will_message=will_message, **kwargs)
        self.write_data(pkg)

    def send_subscribe_packet(self, subscription, **kwargs):
        mid, pkg = package.SubscribePacket.build_package(subscription, self, **kwargs)
        self.write_data(pkg)
        return mid

    def send_unsubscribe_packet(self, topic, **kwargs):
        mid, pkg = package.UnsubscribePacket.build_package(topic, self, **kwargs)
        self.write_data(pkg)
        return mid

    def send_simple_command_packet(self, cmd):
        pkg = package.SimpleCommandPacket.build_package(cmd)
        self.write_data(pkg)

    def send_ping_request(self):
        self.send_simple_command_packet(MQTTCommands.PINGREQ)

    def send_publish(self, message):
        mid, pkg = package.PublishPacket.build_package(message, self)
        self.write_data(pkg)

        return mid, pkg

    def send_disconnect(self, reason_code=0, **properties):
        pkg = package.DisconnectPacket.build_package(self, reason_code=reason_code, **properties)

        self.write_data(pkg)

        return pkg

    def send_command_with_mid(self, cmd, mid, dup, reason_code=0):
        pkg = package.CommandWithMidPacket.build_package(cmd, mid, dup, reason_code=reason_code,
                                                         proto_ver=self.proto_ver)
        self.write_data(pkg)

    def _read_packet(self, data):
        parsed_size = 0
        raw_size = len(data)
        data_size = raw_size

        while True:
            # try to extract packet data, minimum expected packet size is 2
            if data_size < 2:
                break

            # extract payload size
            header_size = 1
            mult = 1
            payload_size = 0

            while True:
                if parsed_size + header_size >= raw_size:
                    # not full header
                    return parsed_size
                payload_byte = data[parsed_size + header_size]
                payload_size += (payload_byte & 0x7F) * mult
                if mult > 2097152:  # 128 * 128 * 128
                    return -1
                mult *= 128
                header_size += 1
                if header_size + payload_size > data_size:
                    # not enough data
                    break
                if payload_byte & 128 == 0:
                    break

            # check size once more
            if header_size + payload_size > data_size:
                # not enough data
                break

            # determine packet type
            command = data[parsed_size]
            start = parsed_size + header_size
            end = start + payload_size
            packet = data[start:end]

            data_size -= header_size + payload_size
            parsed_size += header_size + payload_size

            self._connection.put_package((command, packet))

        return parsed_size

    async def _read_loop(self):
        await self._connected.wait()

        buf = b''
        max_buff_size = 65536  # 64 * 1024
        while self._connected.is_set():
            try:
                buf += await self.read(max_buff_size)
                parsed_size = self._read_packet(buf)
                if parsed_size == -1 or self._connection.is_closing():
                    logger.debug("[RECV EMPTY] Connection will be reset automatically.")
                    break
                buf = buf[parsed_size:]
            except ConnectionResetError as exc:
                # This connection will be closed, because we received the empty data.
                # So we can safely break the while
                logger.debug("[RECV EMPTY] Connection will be reset automatically.")
                break

    def connection_lost(self, exc):
        super(MQTTProtocol, self).connection_lost(exc)
        self._connection.put_package((MQTTCommands.DISCONNECT, b''))
        if self._read_loop_future is not None:
            self._read_loop_future.cancel()
            self._read_loop_future = None

        self._queue = asyncio.Queue()
