#!/usr/bin/env python

# Unless explicitly stated otherwise all files in this repository are licensed under
# the BSD-3-Clause License. This product includes software developed at Datadog
# (https://www.datadoghq.com/).
# Copyright 2015-Present Datadog, Inc
"""
DogStatsd is a Python client for DogStatsd, a Statsd fork for Datadog.
"""
# Standard libraries
from random import random
import logging
import os
import socket
import errno
import threading
import time
from threading import Lock, RLock

from typing import Optional, List, Text, Union

# Datadog libraries
from datadog.dogstatsd.context import (
    TimedContextManagerDecorator,
    DistributedContextManagerDecorator,
)
from datadog.dogstatsd.route import get_default_route
from datadog.dogstatsd.container import ContainerID
from datadog.util.compat import is_p3k, text
from datadog.util.format import normalize_tags
from datadog.version import __version__

# Logging
log = logging.getLogger("datadog.dogstatsd")

# Default config
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8125

# Buffering-related values (in seconds)
DEFAULT_FLUSH_INTERVAL = 0.3
MIN_FLUSH_INTERVAL = 0.0001

# Tag name of entity_id
ENTITY_ID_TAG_NAME = "dd.internal.entity_id"

# Env var name of entity_id
ENTITY_ID_ENV_VAR = "DD_ENTITY_ID"

# Env var to enable/disable sending the container ID field
ORIGIN_DETECTION_ENABLED = "DD_ORIGIN_DETECTION_ENABLED"

# Default buffer settings based on socket type
UDP_OPTIMAL_PAYLOAD_LENGTH = 1432
UDS_OPTIMAL_PAYLOAD_LENGTH = 8192

# Socket options
MIN_SEND_BUFFER_SIZE = 32 * 1024

# Mapping of each "DD_" prefixed environment variable to a specific tag name
DD_ENV_TAGS_MAPPING = {
    ENTITY_ID_ENV_VAR: ENTITY_ID_TAG_NAME,
    "DD_ENV": "env",
    "DD_SERVICE": "service",
    "DD_VERSION": "version",
}

# Telemetry minimum flush interval in seconds
DEFAULT_TELEMETRY_MIN_FLUSH_INTERVAL = 10

# Telemetry pre-computed formatting string. Pre-computation
# increases throughput of composing the result by 2-15% from basic
# '%'-based formatting with a `join`.
TELEMETRY_FORMATTING_STR = "\n".join(
    [
        "datadog.dogstatsd.client.metrics:%s|c|#%s",
        "datadog.dogstatsd.client.events:%s|c|#%s",
        "datadog.dogstatsd.client.service_checks:%s|c|#%s",
        "datadog.dogstatsd.client.bytes_sent:%s|c|#%s",
        "datadog.dogstatsd.client.bytes_dropped:%s|c|#%s",
        "datadog.dogstatsd.client.packets_sent:%s|c|#%s",
        "datadog.dogstatsd.client.packets_dropped:%s|c|#%s",
    ]
) + "\n"


def addressfamily(hostname, port):
    # type: (str, int) -> socket.AddressFamily
    if not isinstance(hostname, str):
        return socket.AF_INET

    # sort to prefer IPv4 address family for backwards compatibility
    return sorted(i[0] for i in socket.getaddrinfo(hostname, port))[0]


# pylint: disable=useless-object-inheritance,too-many-instance-attributes
# pylint: disable=too-many-arguments,too-many-locals
class DogStatsd(object):
    OK, WARNING, CRITICAL, UNKNOWN = (0, 1, 2, 3)

    def __init__(
        self,
        host=DEFAULT_HOST,                      # type: Text
        port=DEFAULT_PORT,                      # type: int
        max_buffer_size=None,                   # type: None
        flush_interval=DEFAULT_FLUSH_INTERVAL,  # type: float
        disable_buffering=True,                 # type: bool
        namespace=None,                         # type: Optional[Text]
        constant_tags=None,                     # type: Optional[List[str]]
        use_ms=False,                           # type: bool
        use_default_route=False,                # type: bool
        socket_path=None,                       # type: Optional[Text]
        default_sample_rate=1,                  # type: float
        disable_telemetry=False,                # type: bool
        telemetry_min_flush_interval=(DEFAULT_TELEMETRY_MIN_FLUSH_INTERVAL),  # type: int
        telemetry_host=None,                    # type: Text
        telemetry_port=None,                    # type: Union[str, int]
        telemetry_socket_path=None,             # type: Text
        max_buffer_len=0,                       # type: int
        container_id=None,                      # type: Optional[Text]
        origin_detection_enabled=True,          # type: bool
    ):  # type: (...) -> None
        """
        Initialize a DogStatsd object.

        >>> statsd = DogStatsd()

        :envvar DD_AGENT_HOST: the host of the DogStatsd server.
        If set, it overrides default value.
        :type DD_AGENT_HOST: string

        :envvar DD_DOGSTATSD_PORT: the port of the DogStatsd server.
        If set, it overrides default value.
        :type DD_DOGSTATSD_PORT: integer

        :envvar DATADOG_TAGS: Tags to attach to every metric reported by dogstatsd client.
        :type DATADOG_TAGS: comma-delimited string

        :envvar DD_ENTITY_ID: Tag to identify the client entity.
        :type DD_ENTITY_ID: string

        :envvar DD_ENV: the env of the service running the dogstatsd client.
        If set, it is appended to the constant (global) tags of the statsd client.
        :type DD_ENV: string

        :envvar DD_SERVICE: the name of the service running the dogstatsd client.
        If set, it is appended to the constant (global) tags of the statsd client.
        :type DD_SERVICE: string

        :envvar DD_VERSION: the version of the service running the dogstatsd client.
        If set, it is appended to the constant (global) tags of the statsd client.
        :type DD_VERSION: string

        :envvar DD_DOGSTATSD_DISABLE: Disable any statsd metric collection (default False)
        :type DD_DOGSTATSD_DISABLE: boolean

        :envvar DD_TELEMETRY_HOST: the host for the dogstatsd server we wish to submit
        telemetry stats to. If set, it overrides default value.
        :type DD_TELEMETRY_HOST: string

        :envvar DD_TELEMETRY_PORT: the port for the dogstatsd server we wish to submit
        telemetry stats to. If set, it overrides default value.
        :type DD_TELEMETRY_PORT: integer

        :envvar DD_ORIGIN_DETECTION_ENABLED: Enable/disable sending the container ID field
        for origin detection.
        :type DD_ORIGIN_DETECTION_ENABLED: boolean

        :param host: the host of the DogStatsd server.
        :type host: string

        :param port: the port of the DogStatsd server.
        :type port: integer

        :max_buffer_size: Deprecated option, do not use it anymore.
        :type max_buffer_type: None

        :flush_interval: Amount of time in seconds that the flush thread will
        wait before trying to flush the buffered metrics to the server. If set,
        it overrides the default value.
        :type flush_interval: float

        :disable_buffering: If set, metrics are no longered buffered by the client and
        all data is sent synchronously to the server
        :type disable_buffering: bool

        :param namespace: Namespace to prefix all metric names
        :type namespace: string

        :param constant_tags: Tags to attach to all metrics
        :type constant_tags: list of strings

        :param use_ms: Report timed values in milliseconds instead of seconds (default False)
        :type use_ms: boolean

        :param use_default_route: Dynamically set the DogStatsd host to the default route
        (Useful when running the client in a container) (Linux only)
        :type use_default_route: boolean

        :param socket_path: Communicate with dogstatsd through a UNIX socket instead of
        UDP. If set, disables UDP transmission (Linux only)
        :type socket_path: string

        :param default_sample_rate: Sample rate to use by default for all metrics
        :type default_sample_rate: float

        :param max_buffer_len: Maximum number of bytes to buffer before sending to the server
        if sending metrics in batch. If not specified it will be adjusted to a optimal value
        depending on the connection type.
        :type max_buffer_len: integer

        :param disable_telemetry: Should client telemetry be disabled
        :type disable_telemetry: boolean

        :param telemetry_min_flush_interval: Minimum flush interval for telemetry in seconds
        :type telemetry_min_flush_interval: integer

        :param telemetry_host: the host for the dogstatsd server we wish to submit
        telemetry stats to. Optional. If telemetry is enabled and this is not specified
        the default host will be used.
        :type host: string

        :param telemetry_port: the port for the dogstatsd server we wish to submit
        telemetry stats to. Optional. If telemetry is enabled and this is not specified
        the default host will be used.
        :type port: integer

        :param telemetry_socket_path: Submit client telemetry to dogstatsd through a UNIX
        socket instead of UDP. If set, disables UDP transmission (Linux only)
        :type telemetry_socket_path: string

        :param container_id: Allows passing the container ID, this will be used by the Agent to enrich
        metrics with container tags.
        This feature requires Datadog Agent version >=6.35.0 && <7.0.0 or Agent versions >=7.35.0.
        When configured, the provided container ID is prioritized over the container ID discovered
        via Origin Detection. When DD_ENTITY_ID is set, this value is ignored.
        Default: None.
        :type container_id: string

        :param origin_detection_enabled: Enable/disable the client origin detection.
        This feature requires Datadog Agent version >=6.35.0 && <7.0.0 or Agent versions >=7.35.0.
        When enabled, the client tries to discover its container ID and sends it to the Agent
        to enrich the metrics with container tags.
        Origin detection can be disabled by configuring the environment variabe DD_ORIGIN_DETECTION_ENABLED=false
        The client tries to read the container ID by parsing the file /proc/self/cgroup.
        This is not supported on Windows.
        The client prioritizes the value passed via DD_ENTITY_ID (if set) over the container ID.
        Default: True.
        More on this: https://docs.datadoghq.com/developers/dogstatsd/?tab=kubernetes#origin-detection-over-udp
        :type origin_detection_enabled: boolean
        """

        self._socket_lock = Lock()

        # Check for deprecated option
        if max_buffer_size is not None:
            log.warning("The parameter max_buffer_size is now deprecated and is not used anymore")

        # Check host and port env vars
        agent_host = os.environ.get("DD_AGENT_HOST")
        if agent_host and host == DEFAULT_HOST:
            host = agent_host

        dogstatsd_port = os.environ.get("DD_DOGSTATSD_PORT")
        if dogstatsd_port and port == DEFAULT_PORT:
            try:
                port = int(dogstatsd_port)
            except ValueError:
                log.warning(
                    "Port number provided in DD_DOGSTATSD_PORT env var is not an integer: \
                %s, using %s as port number",
                    dogstatsd_port,
                    port,
                )

        # Assuming environment variables always override
        telemetry_host = os.environ.get("DD_TELEMETRY_HOST", telemetry_host)
        telemetry_port = os.environ.get("DD_TELEMETRY_PORT", telemetry_port) or port

        # Check enabled
        if os.environ.get("DD_DOGSTATSD_DISABLE") not in {"True", "true", "yes", "1"}:
            self._enabled = True
        else:
            self._enabled = False

        # Connection
        self._max_payload_size = max_buffer_len
        if socket_path is not None:
            self.socket_path = socket_path  # type: Optional[text]
            self.host = None
            self.port = None
            transport = "uds"
            if not self._max_payload_size:
                self._max_payload_size = UDS_OPTIMAL_PAYLOAD_LENGTH
        else:
            self.socket_path = None
            self.host = self.resolve_host(host, use_default_route)
            self.port = int(port)
            transport = "udp"
            if not self._max_payload_size:
                self._max_payload_size = UDP_OPTIMAL_PAYLOAD_LENGTH

        self.telemetry_socket_path = telemetry_socket_path
        self.telemetry_host = None
        self.telemetry_port = None
        if not telemetry_socket_path and telemetry_host:
            self.telemetry_socket_path = None
            self.telemetry_host = self.resolve_host(telemetry_host, use_default_route)
            self.telemetry_port = int(telemetry_port)

        # Socket
        self.socket = None
        self.telemetry_socket = None
        self.encoding = "utf-8"

        # Options
        env_tags = [tag for tag in os.environ.get("DATADOG_TAGS", "").split(",") if tag]
        # Inject values of DD_* environment variables as global tags.
        has_entity_id = False
        for var, tag_name in DD_ENV_TAGS_MAPPING.items():
            value = os.environ.get(var, "")
            if value:
                env_tags.append("{name}:{value}".format(name=tag_name, value=value))
                if var == ENTITY_ID_ENV_VAR:
                    has_entity_id = True
        if constant_tags is None:
            constant_tags = []
        self.constant_tags = constant_tags + env_tags
        if namespace is not None:
            namespace = text(namespace)
        self.namespace = namespace
        self.use_ms = use_ms
        self.default_sample_rate = default_sample_rate

        # Origin detection
        self._container_id = None
        if not has_entity_id:
            origin_detection_enabled = self._is_origin_detection_enabled(
                container_id, origin_detection_enabled, has_entity_id
            )
            self._set_container_id(container_id, origin_detection_enabled)

        # init telemetry version
        self._client_tags = [
            "client:py",
            "client_version:{}".format(__version__),
            "client_transport:{}".format(transport),
        ]
        self._reset_telemetry()
        self._telemetry_flush_interval = telemetry_min_flush_interval
        self._telemetry = not disable_telemetry
        self._last_flush_time = time.time()

        self._current_buffer_total_size = 0
        self._buffer = []  # type: List[Text]
        self._buffer_lock = RLock()

        self._reset_buffer()

        # This lock is used for all cases where buffering functionality is
        # being toggled (by `open_buffer()`, `close_buffer()`, or
        # `self._disable_buffering` calls).
        self._buffering_toggle_lock = RLock()

        # If buffering is disabled, we bypass the buffer function.
        self._send = self._send_to_buffer
        self._disable_buffering = disable_buffering
        if self._disable_buffering:
            self._send = self._send_to_server
            log.debug("Statsd buffering is disabled")

        # Start the flush thread if buffering is enabled and the interval is above
        # a reasonable range. This both prevents thrashing and allow us to use "0.0"
        # as a value for disabling the automatic flush timer as well.
        self._flush_interval = flush_interval
        self._flush_thread_stop = threading.Event()
        self._start_flush_thread(self._flush_interval)

    def disable_telemetry(self):
        self._telemetry = False

    def enable_telemetry(self):
        self._telemetry = True

    # Note: Invocations of this method should be thread-safe
    def _start_flush_thread(self, flush_interval):
        if self._disable_buffering or self._flush_interval <= MIN_FLUSH_INTERVAL:
            log.debug("Statsd periodic buffer flush is disabled")
            return

        def _flush_thread_loop(self, flush_interval):
            while not self._flush_thread_stop.is_set():
                time.sleep(flush_interval)
                self.flush()

        self._flush_thread = threading.Thread(
            name="{}_flush_thread".format(self.__class__.__name__),
            target=_flush_thread_loop,
            args=(self, flush_interval,),
        )
        self._flush_thread.daemon = True
        self._flush_thread.start()

        log.debug(
            "Statsd flush thread registered with period of %s",
            self._flush_interval,
        )

    # Note: Invocations of this method should be thread-safe
    def _stop_flush_thread(self):
        if not self._flush_thread:
            log.warning("No statsd flush thread to stop")
            return

        try:
            self.flush()
        finally:
            pass

        self._flush_thread_stop.set()

        self._flush_thread.join()
        self._flush_thread = None

        self._flush_thread_stop.clear()

    def _dedicated_telemetry_destination(self):
        return bool(self.telemetry_socket_path or self.telemetry_host)

    # Context manager helper
    def __enter__(self):
        self.open_buffer()
        return self

    # Context manager helper
    def __exit__(self, exc_type, value, traceback):
        self.close_buffer()

    @property
    def disable_buffering(self):
        with self._buffering_toggle_lock:
            return self._disable_buffering

    @disable_buffering.setter
    def disable_buffering(self, is_disabled):
        with self._buffering_toggle_lock:
            # If the toggle didn't change anything, this method is a noop
            if self._disable_buffering == is_disabled:
                return

            self._disable_buffering = is_disabled

            # If buffering has been disabled, flush and kill the background thread
            # otherwise start up the flushing thread and enable the buffering.
            if is_disabled:
                self._send = self._send_to_server
                self._stop_flush_thread()
                log.debug("Statsd buffering is disabled")
            else:
                self._send = self._send_to_buffer
                self._start_flush_thread(self._flush_interval)

    @staticmethod
    def resolve_host(host, use_default_route):
        """
        Resolve the DogStatsd host.

        :param host: host
        :type host: string
        :param use_default_route: Use the system default route as host (overrides `host` parameter)
        :type use_default_route: bool
        """
        if not use_default_route:
            return host

        return get_default_route()

    def get_socket(self, telemetry=False):
        """
        Return a connected socket.

        Note: connect the socket before assigning it to the class instance to
        avoid bad thread race conditions.
        """
        with self._socket_lock:
            if telemetry and self._dedicated_telemetry_destination():
                if not self.telemetry_socket:
                    if self.telemetry_socket_path is not None:
                        self.telemetry_socket = self._get_uds_socket(
                            self.telemetry_socket_path,
                        )
                    else:
                        self.telemetry_socket = self._get_udp_socket(
                            self.telemetry_host,
                            self.telemetry_port,
                        )

                return self.telemetry_socket

            if not self.socket:
                if self.socket_path is not None:
                    self.socket = self._get_uds_socket(self.socket_path)
                else:
                    self.socket = self._get_udp_socket(
                        self.host,
                        self.port,
                    )

            return self.socket

    @classmethod
    def _ensure_min_send_buffer_size(cls, sock, min_size=MIN_SEND_BUFFER_SIZE):
        # Increase the receiving buffer size where needed (e.g. MacOS has 4k RX
        # buffers which is half of the max packet size that the client will send.
        if os.name == 'posix':
            try:
                recv_buff_size = sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
                if recv_buff_size <= min_size:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, min_size)
                    log.debug("Socket send buffer increased to %dkb", min_size / 1024)
            finally:
                pass

    @classmethod
    def _get_uds_socket(cls, socket_path):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.setblocking(0)
        cls._ensure_min_send_buffer_size(sock)
        sock.connect(socket_path)
        return sock

    @classmethod
    def _get_udp_socket(cls, host, port):
        sock = socket.socket(addressfamily(host, port), socket.SOCK_DGRAM)
        sock.setblocking(0)
        cls._ensure_min_send_buffer_size(sock)
        sock.connect((host, port))

        return sock

    def open_buffer(self, max_buffer_size=None):
        """
        Open a buffer to send a batch of metrics.

        To take advantage of automatic flushing, you should use the context manager instead

        >>> with DogStatsd() as batch:
        >>>     batch.gauge("users.online", 123)
        >>>     batch.gauge("active.connections", 1001)

        Note: This method must be called before close_buffer() matching invocation.
        """

        self._buffering_toggle_lock.acquire()

        # XXX Remove if `disable_buffering` default is changed to False
        self._send = self._send_to_buffer

        if max_buffer_size is not None:
            log.warning("The parameter max_buffer_size is now deprecated and is not used anymore")

        self._reset_buffer()

    def close_buffer(self):
        """
        Flush the buffer and switch back to single metric packets.

        Note: This method must be called after a matching open_buffer()
        invocation.
        """
        try:
            self.flush()
        finally:
            # XXX Remove if `disable_buffering` default is changed to False
            if self._disable_buffering:
                self._send = self._send_to_server

            self._buffering_toggle_lock.release()

    def _reset_buffer(self):
        with self._buffer_lock:
            self._current_buffer_total_size = 0
            self._buffer = []

    def flush(self):
        """
        Flush the metrics buffer by sending the data to the server.
        """
        with self._buffer_lock:
            # Only send packets if there are packets to send
            if self._buffer:
                self._send_to_server("\n".join(self._buffer))
                self._reset_buffer()

    def gauge(
        self,
        metric,  # type: Text
        value,  # type: float
        tags=None,  # type: Optional[List[str]]
        sample_rate=None,  # type: Optional[float]
    ):  # type(...) -> None
        """
        Record the value of a gauge, optionally setting a list of tags and a
        sample rate.

        >>> statsd.gauge("users.online", 123)
        >>> statsd.gauge("active.connections", 1001, tags=["protocol:http"])
        """
        return self._report(metric, "g", value, tags, sample_rate)

    def increment(
        self,
        metric,  # type: Text
        value=1,  # type: float
        tags=None,  # type: Optional[List[str]]
        sample_rate=None,  # type: Optional[float]
    ):  # type: (...) -> None
        """
        Increment a counter, optionally setting a value, tags and a sample
        rate.

        >>> statsd.increment("page.views")
        >>> statsd.increment("files.transferred", 124)
        """
        self._report(metric, "c", value, tags, sample_rate)

    def decrement(
        self,
        metric,  # type: Text
        value=1,  # type: float
        tags=None,  # type: Optional[List[str]]
        sample_rate=None,  # type: Optional[float]
    ):  # type(...) -> None
        """
        Decrement a counter, optionally setting a value, tags and a sample
        rate.

        >>> statsd.decrement("files.remaining")
        >>> statsd.decrement("active.connections", 2)
        """
        metric_value = -value if value else value
        self._report(metric, "c", metric_value, tags, sample_rate)

    def histogram(
        self,
        metric,  # type: Text
        value,  # type: float
        tags=None,  # type: Optional[List[str]]
        sample_rate=None,  # type: Optional[float]
    ):  # type(...) -> None
        """
        Sample a histogram value, optionally setting tags and a sample rate.

        >>> statsd.histogram("uploaded.file.size", 1445)
        >>> statsd.histogram("album.photo.count", 26, tags=["gender:female"])
        """
        self._report(metric, "h", value, tags, sample_rate)

    def distribution(
        self,
        metric,  # type: Text
        value,  # type: float
        tags=None,  # type: Optional[List[str]]
        sample_rate=None,  # type: Optional[float]
    ):  # type(...) -> None
        """
        Send a global distribution value, optionally setting tags and a sample rate.

        >>> statsd.distribution("uploaded.file.size", 1445)
        >>> statsd.distribution("album.photo.count", 26, tags=["gender:female"])
        """
        self._report(metric, "d", value, tags, sample_rate)

    def timing(
        self,
        metric,  # type: Text
        value,  # type: float
        tags=None,  # type: Optional[List[str]]
        sample_rate=None,  # type: Optional[float]
    ):  # type(...) -> None
        """
        Record a timing, optionally setting tags and a sample rate.

        >>> statsd.timing("query.response.time", 1234)
        """
        self._report(metric, "ms", value, tags, sample_rate)

    def timed(self, metric=None, tags=None, sample_rate=None, use_ms=None):
        """
        A decorator or context manager that will measure the distribution of a
        function's/context's run time. Optionally specify a list of tags or a
        sample rate. If the metric is not defined as a decorator, the module
        name and function name will be used. The metric is required as a context
        manager.
        ::

            @statsd.timed("user.query.time", sample_rate=0.5)
            def get_user(user_id):
                # Do what you need to ...
                pass

            # Is equivalent to ...
            with statsd.timed("user.query.time", sample_rate=0.5):
                # Do what you need to ...
                pass

            # Is equivalent to ...
            start = time.time()
            try:
                get_user(user_id)
            finally:
                statsd.timing("user.query.time", time.time() - start)
        """
        return TimedContextManagerDecorator(self, metric, tags, sample_rate, use_ms)

    def distributed(self, metric=None, tags=None, sample_rate=None, use_ms=None):
        """
        A decorator or context manager that will measure the distribution of a
        function's/context's run time using custom metric distribution.
        Optionally specify a list of tags or a sample rate. If the metric is not
        defined as a decorator, the module name and function name will be used.
        The metric is required as a context manager.
        ::

            @statsd.distributed("user.query.time", sample_rate=0.5)
            def get_user(user_id):
                # Do what you need to ...
                pass

            # Is equivalent to ...
            with statsd.distributed("user.query.time", sample_rate=0.5):
                # Do what you need to ...
                pass

            # Is equivalent to ...
            start = time.time()
            try:
                get_user(user_id)
            finally:
                statsd.distribution("user.query.time", time.time() - start)
        """
        return DistributedContextManagerDecorator(self, metric, tags, sample_rate, use_ms)

    def set(self, metric, value, tags=None, sample_rate=None):
        """
        Sample a set value.

        >>> statsd.set("visitors.uniques", 999)
        """
        self._report(metric, "s", value, tags, sample_rate)

    def close_socket(self):
        """
        Closes connected socket if connected.
        """
        with self._socket_lock:
            if self.socket:
                try:
                    self.socket.close()
                except OSError as e:
                    log.error("Unexpected error: %s", str(e))
                self.socket = None

            if self.telemetry_socket:
                try:
                    self.telemetry_socket.close()
                except OSError as e:
                    log.error("Unexpected error: %s", str(e))
                self.telemetry_socket = None

    def _serialize_metric(self, metric, metric_type, value, tags, sample_rate=1):
        # Create/format the metric packet
        return "%s%s:%s|%s%s%s%s" % (
            (self.namespace + ".") if self.namespace else "",
            metric,
            value,
            metric_type,
            ("|@" + text(sample_rate)) if sample_rate != 1 else "",
            ("|#" + ",".join(normalize_tags(tags))) if tags else "",
            ("|c:" + self._container_id if self._container_id else "")
        )

    def _report(self, metric, metric_type, value, tags, sample_rate):
        """
        Create a metric packet and send it.

        More information about the packets' format: http://docs.datadoghq.com/guides/dogstatsd/
        """
        if value is None:
            return

        if self._enabled is not True:
            return

        if self._telemetry:
            self.metrics_count += 1

        if sample_rate is None:
            sample_rate = self.default_sample_rate

        if sample_rate != 1 and random() > sample_rate:
            return

        # Resolve the full tag list
        tags = self._add_constant_tags(tags)
        payload = self._serialize_metric(metric, metric_type, value, tags, sample_rate)

        # Send it
        self._send(payload)

    def _reset_telemetry(self):
        self.metrics_count = 0
        self.events_count = 0
        self.service_checks_count = 0
        self.bytes_sent = 0
        self.bytes_dropped = 0
        self.packets_sent = 0
        self.packets_dropped = 0
        self._last_flush_time = time.time()

    def _flush_telemetry(self):
        telemetry_tags = ",".join(self._add_constant_tags(self._client_tags))

        return TELEMETRY_FORMATTING_STR % (
            self.metrics_count,
            telemetry_tags,
            self.events_count,
            telemetry_tags,
            self.service_checks_count,
            telemetry_tags,
            self.bytes_sent,
            telemetry_tags,
            self.bytes_dropped,
            telemetry_tags,
            self.packets_sent,
            telemetry_tags,
            self.packets_dropped,
            telemetry_tags,
        )

    def _is_telemetry_flush_time(self):
        return self._telemetry and \
            self._last_flush_time + self._telemetry_flush_interval < time.time()

    def _send_to_server(self, packet):
        self._xmit_packet(packet + '\n', False)
        if self._is_telemetry_flush_time():
            telemetry = self._flush_telemetry()
            if self._xmit_packet(telemetry, True):
                self._reset_telemetry()
                self.packets_sent += 1
                self.bytes_sent += len(telemetry)
            else:
                # Telemetry packet has been dropped, keep telemetry data for the next flush
                self._last_flush_time = time.time()
                self.bytes_dropped += len(telemetry)
                self.packets_dropped += 1

    def _xmit_packet(self, packet, is_telemetry):
        try:
            if is_telemetry and self._dedicated_telemetry_destination():
                mysocket = self.telemetry_socket or self.get_socket(telemetry=True)
            else:
                # If set, use socket directly
                mysocket = self.socket or self.get_socket()

            mysocket.send(packet.encode(self.encoding))

            if not is_telemetry and self._telemetry:
                self.packets_sent += 1
                self.bytes_sent += len(packet)

            return True
        except socket.timeout:
            # dogstatsd is overflowing, drop the packets (mimics the UDP behaviour)
            pass
        except (socket.herror, socket.gaierror) as socket_err:
            log.warning(
                "Error submitting packet: %s, dropping the packet and closing the socket",
                socket_err,
            )
            self.close_socket()
        except socket.error as socket_err:
            if socket_err.errno == errno.EAGAIN:
                log.debug("Socket send would block: %s, dropping the packet", socket_err)
            elif socket_err.errno == errno.ENOBUFS:
                log.debug("Socket buffer full: %s, dropping the packet", socket_err)
            elif socket_err.errno == errno.EMSGSIZE:
                log.debug(
                    "Packet size too big (size: %d): %s, dropping the packet",
                    len(packet.encode(self.encoding)),
                    socket_err)
            else:
                log.warning(
                    "Error submitting packet: %s, dropping the packet and closing the socket",
                    socket_err,
                )
                self.close_socket()
        except Exception as exc:
            print("Unexpected error: %s", exc)
            log.error("Unexpected error: %s", str(exc))

        if not is_telemetry and self._telemetry:
            self.bytes_dropped += len(packet)
            self.packets_dropped += 1

        return False

    def _send_to_buffer(self, packet):
        with self._buffer_lock:
            if self._should_flush(len(packet)):
                self.flush()

            self._buffer.append(packet)
            # Update the current buffer length, including line break to anticipate
            # the final packet size
            self._current_buffer_total_size += len(packet) + 1

    def _should_flush(self, length_to_be_added):
        if self._current_buffer_total_size + length_to_be_added + 1 > self._max_payload_size:
            return True
        return False

    @staticmethod
    def _escape_event_content(string):
        return string.replace("\n", "\\n")

    @staticmethod
    def _escape_service_check_message(string):
        return string.replace("\n", "\\n").replace("m:", "m\\:")

    def event(
        self,
        title,
        message,
        alert_type=None,
        aggregation_key=None,
        source_type_name=None,
        date_happened=None,
        priority=None,
        tags=None,
        hostname=None,
    ):
        """
        Send an event. Attributes are the same as the Event API.
            http://docs.datadoghq.com/api/

        >>> statsd.event("Man down!", "This server needs assistance.")
        >>> statsd.event("Web server restart", "The web server is up", alert_type="success")  # NOQA
        """
        title = DogStatsd._escape_event_content(title)
        message = DogStatsd._escape_event_content(message)

        # pylint: disable=undefined-variable
        if not is_p3k():
            if not isinstance(title, unicode):                                       # noqa: F821
                title = unicode(DogStatsd._escape_event_content(title), 'utf8')      # noqa: F821
            if not isinstance(message, unicode):                                     # noqa: F821
                message = unicode(DogStatsd._escape_event_content(message), 'utf8')  # noqa: F821

        # Append all client level tags to every event
        tags = self._add_constant_tags(tags)

        string = u"_e{{{},{}}}:{}|{}".format(
            len(title.encode('utf8', 'replace')),
            len(message.encode('utf8', 'replace')),
            title,
            message,
        )

        if date_happened:
            string = "%s|d:%d" % (string, date_happened)
        if hostname:
            string = "%s|h:%s" % (string, hostname)
        if aggregation_key:
            string = "%s|k:%s" % (string, aggregation_key)
        if priority:
            string = "%s|p:%s" % (string, priority)
        if source_type_name:
            string = "%s|s:%s" % (string, source_type_name)
        if alert_type:
            string = "%s|t:%s" % (string, alert_type)
        if tags:
            string = "%s|#%s" % (string, ",".join(tags))
        if self._container_id:
            string = "%s|c:%s" % (string, self._container_id)

        if len(string) > 8 * 1024:
            raise ValueError(
                u'Event "{0}" payload is too big (>=8KB). Event discarded'.format(
                    title
                )
            )

        if self._telemetry:
            self.events_count += 1

        self._send(string)

    def service_check(
        self,
        check_name,
        status,
        tags=None,
        timestamp=None,
        hostname=None,
        message=None,
    ):
        """
        Send a service check run.

        >>> statsd.service_check("my_service.check_name", DogStatsd.WARNING)
        """
        message = DogStatsd._escape_service_check_message(message) if message is not None else ""

        string = u"_sc|{0}|{1}".format(check_name, status)

        # Append all client level tags to every status check
        tags = self._add_constant_tags(tags)

        if timestamp:
            string = u"{0}|d:{1}".format(string, timestamp)
        if hostname:
            string = u"{0}|h:{1}".format(string, hostname)
        if tags:
            string = u"{0}|#{1}".format(string, ",".join(tags))
        if message:
            string = u"{0}|m:{1}".format(string, message)
        if self._container_id:
            string = u"{0}|c:{1}".format(string, self._container_id)

        if self._telemetry:
            self.service_checks_count += 1

        self._send(string)

    def _add_constant_tags(self, tags):
        if self.constant_tags:
            if tags:
                return tags + self.constant_tags

            return self.constant_tags
        return tags

    def _is_origin_detection_enabled(self, container_id, origin_detection_enabled, has_entity_id):
        """
        Returns whether the client should fill the container field.
        If DD_ENTITY_ID is set, we don't send the container ID
        If a user-defined container ID is provided, we don't ignore origin detection
        as dd.internal.entity_id is prioritized over the container field for backward compatibility.
        If DD_ENTITY_ID is not set, we try to fill the container field automatically unless
        DD_ORIGIN_DETECTION_ENABLED is explicitly set to false.
        """
        if not origin_detection_enabled or has_entity_id or container_id is not None:
            # origin detection is explicitly disabled
            # or DD_ENTITY_ID was found
            # or a user-defined container ID was provided
            return False
        value = os.environ.get(ORIGIN_DETECTION_ENABLED, "")
        return value.lower() not in {"no", "false", "0", "n", "off"}

    def _set_container_id(self, container_id, origin_detection_enabled):
        """
        Initializes the container ID.
        It can either be provided by the user or read from cgroups.
        """
        if container_id:
            self._container_id = container_id
            return
        if origin_detection_enabled:
            try:
                reader = ContainerID()
                self._container_id = reader.container_id
            except Exception as e:
                log.debug("Couldn't get container ID: %s", str(e))
                self._container_id = None


statsd = DogStatsd()
