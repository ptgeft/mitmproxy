import re
import typing

from mitmproxy import ctx, exceptions
from mitmproxy.net.tls import is_tls_record_magic
from mitmproxy.proxy.protocol import base
from mitmproxy.proxy.protocol.http import HTTPMode
from mitmproxy.proxy2 import context, layer, layers
from mitmproxy.proxy2.layers import modes
from mitmproxy.proxy2.layers.tls import HTTP_ALPNS, parse_client_hello

LayerCls = typing.Type[layer.Layer]


def stack_match(
        context: context.Context,
        layers: typing.List[typing.Union[LayerCls, typing.Tuple[LayerCls, ...]]]
) -> bool:
    if len(context.layers) != len(layers):
        return False
    return all(
        expected is typing.Any or isinstance(actual, expected)
        for actual, expected in zip(context.layers, layers)
    )


class HostMatcher:
    def __init__(self, patterns: typing.Iterable[str] = tuple()):
        self.patterns = patterns
        self.regexes = [re.compile(p, re.IGNORECASE) for p in self.patterns]

    def __call__(self, address):
        if not address:
            return False
        host = f"{address[0]}:{address[1]}"
        return any(rex.search(host) for rex in self.regexes)

    def __bool__(self):
        return bool(self.patterns)


class NextLayer:
    ignore_hosts: typing.Iterable[re.Pattern] = ()
    allow_hosts: typing.Iterable[re.Pattern] = ()
    tcp_hosts: typing.Iterable[re.Pattern] = ()

    def configure(self, updated):
        if "tcp_hosts" in updated:
            self.tcp_hosts = [
                re.compile(x, re.IGNORECASE) for x in ctx.options.tcp_hosts
            ]
        if "allow_hosts" in updated or "ignore_hosts" in updated:
            if ctx.options.allow_hosts and ctx.options.ignore_hosts:
                raise exceptions.OptionsError("The allow_hosts and ignore_hosts options are mutually exclusive.")
            self.ignore_hosts = [
                re.compile(x, re.IGNORECASE) for x in ctx.options.ignore_hosts
            ]
            self.allow_hosts = [
                re.compile(x, re.IGNORECASE) for x in ctx.options.allow_hosts
            ]

    def ignore_connection(self, context: context.Context, data_client: bytes) -> typing.Optional[bool]:
        """
        Returns:
            True, if the connection should be ignored.
            False, if it should not be ignored.
            None, if we need to wait for more input data.
        """
        if not ctx.options.ignore_hosts and not ctx.options.allow_hosts:
            return False

        hostnames: typing.List[str] = []
        if context.server.address:
            hostnames.append(context.server.address[0])
        if is_tls_record_magic(data_client):
            try:
                sni = parse_client_hello(data_client).sni
            except ValueError:
                return None  # defer decision, wait for more input data
            else:
                hostnames.append(sni.decode("idna"))

        if not hostnames:
            return False

        if ctx.options.ignore_hosts:
            return any(
                re.search(rex, host, re.IGNORECASE)
                for host in hostnames
                for rex in ctx.options.ignore_hosts
            )
        elif ctx.options.allow_hosts:
            return not any(
                re.search(rex, host, re.IGNORECASE)
                for host in hostnames
                for rex in ctx.options.allow_hosts
            )

    def next_layer(self, nextlayer: layer.NextLayer):
        if isinstance(nextlayer, base.Layer):
            return  # skip the old proxy core's next_layer event.
        nextlayer.layer = self._next_layer(nextlayer.context, nextlayer.data_client())

    def _next_layer(self, context: context.Context, data_client: bytes) -> typing.Optional[layer.Layer]:
        if len(context.layers) == 0:
            return self.make_top_layer(context)

        if len(data_client) < 3:
            return

        client_tls = is_tls_record_magic(data_client)
        s = lambda *layers: stack_match(context, layers)
        top_layer = context.layers[-1]

        # 1. check for --ignore/--allow
        ignore = self.ignore_connection(context, data_client)
        if ignore is True:
            return layers.TCPLayer(context, ignore=True)
        if ignore is None:
            return

        # 2. Check for TLS
        if client_tls:
            # client tls requires a server tls layer as parent layer
            # reverse proxy mode manages this itself.
            # a secure web proxy doesn't have a server part.
            if isinstance(top_layer, layers.ServerTLSLayer) or s(modes.ReverseProxy) or s(modes.HttpProxy):
                return layers.ClientTLSLayer(context)
            else:
                return layers.ServerTLSLayer(context)

        # 3. Setup the HTTP layer for a regular HTTP proxy or an upstream proxy.
        if any([
            s(modes.HttpProxy),
            # or a "Secure Web Proxy", see https://www.chromium.org/developers/design-documents/secure-web-proxy
            s(modes.HttpProxy, layers.ClientTLSLayer),
        ]):
            if ctx.options.mode == "regular":
                return layers.HttpLayer(context, HTTPMode.regular)
            else:
                return layers.HttpLayer(context, HTTPMode.upstream)

        # 4. Check for --tcp
        if any(
                (context.server.address and rex.search(context.server.address[0])) or
                (context.client.sni and rex.search(context.client.sni))
                for rex in self.tcp_hosts
        ):
            return layers.TCPLayer(context)

        # 5. Check for raw tcp mode.
        alpn_indicates_non_http = (
                context.client.alpn and context.client.alpn not in HTTP_ALPNS
        )
        # Very simple heuristic here - the first three bytes should be
        # the HTTP verb, so A-Za-z is expected.
        probably_no_http = (
            not data_client[:3].isalpha()
        )
        if ctx.options.rawtcp and (alpn_indicates_non_http or probably_no_http):
            return layers.TCPLayer(context)

        # 6. Assume HTTP by default.
        return layers.HttpLayer(context, HTTPMode.transparent)

    def make_top_layer(self, context: context.Context) -> layer.Layer:
        if ctx.options.mode == "regular" or ctx.options.mode.startswith("upstream:"):
            return layers.modes.HttpProxy(context)

        elif ctx.options.mode == "transparent":
            return layers.modes.TransparentProxy(context)

        elif ctx.options.mode.startswith("reverse:"):
            return layers.modes.ReverseProxy(context)

        elif ctx.options.mode == "socks5":
            raise NotImplementedError("Mode not implemented.")

        else:
            raise NotImplementedError("Unknown mode.")
