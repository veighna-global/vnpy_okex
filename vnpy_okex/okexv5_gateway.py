import base64
import hashlib
import hmac
import json
import sys
import time
import zlib
from copy import copy
from datetime import datetime
from threading import Lock
from urllib.parse import urlencode
from typing import Dict
from vnpy.trader.utility import round_to

from requests import ConnectionError
from pytz import utc as UTC_TZ

from vnpy.api.rest import Request, RestClient
from vnpy.api.websocket import WebsocketClient
from vnpy.trader.constant import (Direction, Exchange, Interval, Offset, OrderType, Product, Status, OptionType)
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (AccountData, BarData, CancelRequest, ContractData, HistoryRequest,
                                OrderData, OrderRequest, PositionData, SubscribeRequest, TickData,
                                TradeData)

from tzlocal import get_localzone
LOCAL_TZ = get_localzone()

_ = lambda x: x  # noqa
REST_HOST = "https://www.okex.com"

PUBLIC_WEBSOCKET_HOST = "wss://ws.okex.com:8443/ws/v5/public"
PRIVATE_WEBSOCKET_HOST = "wss://ws.okex.com:8443/ws/v5/private"

SIMULATED_PUBLIC_WEBSOCKET_HOST = "wss://wspap.okex.com:8443/ws/v5/public?brokerId=9999"
SIMULATED_PRIVATE_WEBSOCKET_HOST = "wss://wspap.okex.com:8443/ws/v5/private?brokerId=9999"

STATUS_OKEXV52VT = {
    "live": Status.NOTTRADED,
    "partially_filled": Status.PARTTRADED
}

ORDERTYPE_OKEXV52VT = {
    "market": OrderType.MARKET,
    "limit": OrderType.LIMIT
}

ORDERTYPE_VT2OKEXV5 = {v: k for k, v in ORDERTYPE_OKEXV52VT.items()}

SIDE_OKEXV52VT = {
    "buy": Direction.LONG,
    "sell": Direction.SHORT
}

DIRECTION_OKEXV52VT = {
    "long": Direction.LONG,
    "short": Direction.SHORT,
    "net": Direction.NET
}

INTERVAL_VT2OKEXV5 = {
    Interval.MINUTE: "1m",
    Interval.HOUR: "1H",
    Interval.DAILY: "1D",
}

PRODUCT_OKEXV52VT = {
    "SWAP": Product.FUTURES,
    "SPOT": Product.SPOT,
    "FUTURES": Product.FUTURES,
    "OPTION": Product.OPTION
}

PRODUCT_VT2OKEXV5 = {v: k for k, v in PRODUCT_OKEXV52VT.items()}

OPTIONTYPE_OKEXO2VT = {
    "C": OptionType.CALL,
    "P": OptionType.PUT
}

symbol_contract_map: Dict[str, ContractData] = {}


class OkexV5Gateway(BaseGateway):
    """
    VN Trader Gateway for OKEX connection.
    """

    default_setting = {
        "API Key": "",
        "Secret Key": "",
        "Passphrase": "",
        "会话数": 3,
        "代理地址": "",
        "代理端口": "",
        "服务器": ["SIMULATED", "REAL"],
        "合约模式": ["反向", "正向"],
        "产品类型": ""
    }

    exchanges = [Exchange.OKEX]

    def __init__(self, event_engine):
        """Constructor"""
        super().__init__(event_engine, "OKEXV5")

        self.rest_api = OkexV5RestApi(self)
        # self.ws_api = OkexV5WebsocketApi(self)

        self.ws_pub_api = OkexV5WebsocketPublicApi(self)
        self.ws_pri_api = OkexV5WebsocketPrivateApi(self)

        self.orders = {}

    def connect(self, setting: dict):
        """"""
        key = setting["API Key"]
        secret = setting["Secret Key"]
        passphrase = setting["Passphrase"]
        session_number = setting["会话数"]
        proxy_host = setting["代理地址"]
        proxy_port = setting["代理端口"]
        server = setting["服务器"]

        if server == "REAL":
            self.rest_api.simulated = False
        else:
            self.rest_api.simulated = True

        if setting["合约模式"] == "正向":
            usdt_base = True
        else:
            usdt_base = False

        if proxy_port.isdigit():
            proxy_port = int(proxy_port)
        else:
            proxy_port = 0

        self.rest_api.connect(usdt_base, key, secret, passphrase,
                              session_number, proxy_host, proxy_port)
        # self.ws_api.connect(usdt_base, key, secret, passphrase, proxy_host, proxy_port, server)

        self.ws_pub_api.connect(usdt_base, proxy_host, proxy_port, server)
        self.ws_pri_api.connect(usdt_base, key, secret, passphrase, proxy_host, proxy_port, server)

    def subscribe(self, req: SubscribeRequest):
        """"""
        # self.ws_api.subscribe(req)

        self.ws_pub_api.subscribe(req)

    def send_order(self, req: OrderRequest):
        """"""
        return self.rest_api.send_order(req)

    def cancel_order(self, req: CancelRequest):
        """"""
        self.rest_api.cancel_order(req)

    def query_account(self):
        """"""
        pass

    def query_position(self):
        """"""
        pass

    def query_history(self, req: HistoryRequest):
        """"""
        return self.rest_api.query_history(req)

    def close(self):
        """"""
        self.rest_api.stop()
        # self.ws_api.stop()

        self.ws_pub_api.stop()
        self.ws_pri_api.stop()

    def on_order(self, order: OrderData):
        """"""
        self.orders[order.orderid] = order
        super().on_order(order)

    def get_order(self, orderid: str):
        """"""
        return self.orders.get(orderid, None)


class OkexV5RestApi(RestClient):
    """
    OKEX V5 REST API
    """

    def __init__(self, gateway: "OkexV5Gateway"):
        """"""
        super(OkexV5RestApi, self).__init__()

        self.gateway = gateway
        self.gateway_name = gateway.gateway_name

        self.key = ""
        self.secret = ""
        self.passphrase = ""

        self.order_count = 10000
        self.order_count_lock = Lock()

        self.connect_time = 0

        self.simulated: bool = False
        self.usdt_base: bool = False

    def sign(self, request):
        """
        Generate OKEX V5 signature.
        """
        # Sign
        timestamp = generate_timestamp()
        request.data = json.dumps(request.data)

        if request.params:
            path = request.path + "?" + urlencode(request.params)
        else:
            path = request.path

        msg = timestamp + request.method + path + request.data
        signature = generate_signature(msg, self.secret)

        # Add headers
        request.headers = {
            "OK-ACCESS-KEY": self.key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json"
        }

        if self.simulated:
            request.headers["x-simulated-trading"] = 1

        return request

    def connect(
        self,
        usdt_base: bool,
        key: str,
        secret: str,
        passphrase: str,
        session_number: int,
        proxy_host: str,
        proxy_port: int,
    ):
        """
        Initialize connection to REST server.
        """
        self.usdt_base = usdt_base
        self.key = key
        self.secret = secret.encode()
        self.passphrase = passphrase

        self.connect_time = int(datetime.now().strftime("%y%m%d%H%M%S"))

        self.init(REST_HOST, proxy_host, proxy_port)
        self.start(session_number)
        self.gateway.write_log("REST API启动成功")

        self.query_time()
        self.query_contract()
        self.query_accounts()
        self.query_position()

    def _new_order_id(self):
        with self.order_count_lock:
            self.order_count += 1
            return self.order_count

    def send_order(self, req: OrderRequest):
        """"""
        orderid = f"a{self.connect_time}{self._new_order_id()}"

        data = {
            "instId": req.symbol,
            "tdMode": "cross",
            "clOrdId": orderid,
            "side": SIDE_OKEXV52VT[req.direction],
            "ordType": ORDERTYPE_VT2OKEXV5[req.type],
            "px": str(req.price),
            "sz": str(req.volume)
        }
        if req.offset == Offset.OPEN:
            if req.direction == Direction.LONG:
                data["posSide"] = "long"
            else:
                data["posSide"] = "short"
        elif req.offset == Offset.CLOSE:
            if req.direction == Direction.LONG:
                data["posSide"] = "short"
            else:
                data["posSide"] = "long"

        order = req.create_order_data(orderid, self.gateway_name)

        self.add_request(
            "POST",
            "/api/v5/trade/order",
            callback=self.on_send_order,
            data=data,
            extra=order,
            on_failed=self.on_send_order_failed,
            on_error=self.on_send_order_error,
        )

        self.gateway.on_order(order)
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest):
        """"""
        data = {
            "clOrdId": req.orderid
        }
        self.add_request(
            "POST",
            "/api/v5/trade/cancel-order/",
            data=data,
            callback=self.on_cancel_order,
            on_error=self.on_cancel_order_error,
            on_failed=self.on_cancel_order_failed,
            extra=req
        )

    def query_contract(self):
        """"""
        contracts = ["SPOT", "SWAP", "FUTURES", "OPTION"]
        ulys = ["EOS-USD", "ETH-USD", "BTC-USD"]
        for contract in contracts:
            if contract == "OPTION":
                for uly in ulys:
                    data = {
                        "instType": "OPTION",
                        "uly": uly
                    }
                    self._query_contract(data)
            else:
                data = {
                    "instType": contract
                }
                self._query_contract(data)

    def _query_contract(self, data):
        """"""
        self.add_request(
            "GET",
            "/api/v5/public/instruments",
            data=data,
            callback=self.on_query_contracts
        )

    def query_accounts(self):
        """"""
        self.add_request(
            "GET",
            "/api/v5/account/balance",
            callback=self.on_query_accounts
        )

    def query_orders(self):
        """"""
        self.add_request(
            "GET",
            "/api/v5/trade/orders-pending",
            callback=self.on_query_order,
        )

    def query_position(self):
        """"""
        self.add_request(
            "GET",
            "/api/v5/account/positions",
            callback=self.on_query_position
        )

    def query_time(self):
        """"""
        self.add_request(
            "GET",
            "/api/v5/public/time",
            callback=self.on_query_time
        )

    def on_query_contracts(self, data, request):
        """"""
        for d in data["data"]:
            symbol = d["instId"]

            product = PRODUCT_OKEXV52VT[d["instType"]]
            contract = ContractData(
                symbol=symbol,
                exchange=Exchange.OKEX,
                name=symbol,
                product=product,
                pricetick=float(d["tickSz"]),
                min_volume=float(d["minSz"]),
                history_data=True,
                gateway_name=self.gateway_name,
            )

            if product == Product.OPTION:
                contract.size = float(d["ctMult"])
                contract.option_strike = float(d["stk"])
                contract.option_type = OPTIONTYPE_OKEXO2VT[d["optType"]]
                contract.option_expiry = _parse_timestamp(d["expTime"])
                contract.option_portfolio = d["uly"]
                contract.option_index = d["stk"]
                contract.net_position = True
                contract.option_underlying = "_".join([
                    contract.option_portfolio,
                    contract.option_expiry.strftime("%Y%m%d")
                ])

            elif product == Product.SPOT:
                contract.net_position = True
                contract.size = 1

            else:
                contract.size = float(d["ctMult"])

            self.gateway.on_contract(contract)

            symbol_contract_map[contract.symbol] = contract

        self.gateway.write_log("合约信息查询成功")

        # Start websocket api after instruments data collected
        self.gateway.ws_api.start()

        # and query pending orders
        self.query_orders()

    def on_query_accounts(self, data, request):
        """"""
        for details in data['data']:
            account = _parse_account_details(details, gateway_name=self.gateway_name)
            self.gateway.on_account(account)

        self.gateway.write_log("账户资金查询成功")

    def on_query_position(self, datas, request):
        """"""
        for data in datas:
            d = data["data"]

            for data in d:
                symbol = data["instId"].upper()
                pos = _parse_position_data(data, symbol=symbol, gateway_name=self.gateway_name)
                self.gateway.on_position(pos)

    def on_query_order(self, data, request):
        """"""
        for order_info in data["data"]:
            order = _parse_order_data(order_info, gateway_name=self.gateway_name)
            self.gateway.on_order(order)

    def on_query_time(self, data, request):
        """"""
        timestamp = eval(data["data"][0]["ts"])
        server_time = datetime.fromtimestamp(timestamp)
        local_time = datetime.utcnow().isoformat()
        msg = f"服务器时间：{server_time}，本机时间：{local_time}"
        self.gateway.write_log(msg)

    def on_send_order_failed(self, status_code: str, request: Request):
        """
        Callback when sending order failed on server.
        """

        order = request.extra
        order.status = Status.REJECTED
        order.time = datetime.now().strftime("%H:%M:%S.%f")
        self.gateway.on_order(order)
        msg = f"委托失败，状态码：{status_code}，信息：{request.response.text}"
        self.gateway.write_log(msg)

    def on_send_order_error(
        self, exception_type: type, exception_value: Exception, tb, request: Request
    ):
        """
        Callback when sending order caused exception.
        """

        order = request.extra
        order.status = Status.REJECTED
        self.gateway.on_order(order)

        # Record exception if not ConnectionError
        if not issubclass(exception_type, ConnectionError):
            self.on_error(exception_type, exception_value, tb, request)

    def on_send_order(self, data, request):
        """
        Websocket will push a new order status
        """
        order = request.extra
        error_msg = data["error_message"]
        if error_msg:
            order.status = Status.REJECTED
            self.gateway.on_order(order)

            self.gateway.write_log(f"委托失败：{error_msg}")

    def on_cancel_order_error(
        self, exception_type: type, exception_value: Exception, tb, request: Request
    ):
        """
        Callback when cancelling order failed on server.
        """
        # Record exception if not ConnectionError
        if not issubclass(exception_type, ConnectionError):
            self.on_error(exception_type, exception_value, tb, request)

    def on_cancel_order(self, data, request):
        """
        Websocket will push a new order status
        """
        pass

    def on_cancel_order_failed(self, status_code: int, request: Request):
        """
        If cancel failed, mark order status to be rejected.
        """
        req = request.extra
        order = self.gateway.get_order(req.orderid)
        if order:
            order.status = Status.REJECTED
            self.gateway.on_order(order)

    def on_failed(self, status_code: int, request: Request):
        """
        Callback to handle request failed.
        """
        msg = f"请求失败，状态码：{status_code}，信息：{request.response.text}"
        self.gateway.write_log(msg)

    def on_error(
        self, exception_type: type, exception_value: Exception, tb, request: Request
    ):
        """
        Callback to handler request exception.
        """
        msg = f"触发异常，状态码：{exception_type}，信息：{exception_value}"
        self.gateway.write_log(msg)

        sys.stderr.write(
            self.exception_detail(exception_type, exception_value, tb, request)
        )

    def query_history(self, req: HistoryRequest):
        """"""
        buf = {}
        end_time = None

        for i in range(10):
            path = "/api/v5/market/history-candles"

            # Create query params
            params = {
                "instId": req.symbol,
                "bar": INTERVAL_VT2OKEXV5[req.interval]
            }

            if end_time:
                params["after"] = end_time

            # Get response from server
            resp = self.request(
                "GET",
                path,
                params=params
            )

            # Break if request failed with other status code
            if resp.status_code // 100 != 2:
                msg = f"获取历史数据失败，状态码：{resp.status_code}，信息：{resp.text}"
                self.gateway.write_log(msg)
                break
            else:
                data = resp.json()
                if not data["data"]:
                    m = data["msg"]
                    msg = f"获取历史数据为空, {m}"
                    break

                for l in data["data"]:
                    ts, o, h, l, c, vol, _ = l
                    dt = _parse_timestamp(ts)
                    bar = BarData(
                        symbol=req.symbol,
                        exchange=req.exchange,
                        datetime=dt,
                        interval=req.interval,
                        volume=float(vol),
                        open_price=float(o),
                        high_price=float(h),
                        low_price=float(l),
                        close_price=float(c),
                        gateway_name=self.gateway_name
                    )
                    buf[bar.datetime] = bar

                begin = data[-1][0]
                end = data[0][0]
                msg = f"获取历史数据成功，{req.symbol} - {req.interval.value}，{begin} - {end}"
                self.gateway.write_log(msg)

                # Update start time
                end_time = begin

        index = list(buf.keys())
        index.sort()

        history = [buf[i] for i in index]
        return history


class OkexV5WebsocketPublicApi(WebsocketClient):
    """"""

    def __init__(self, gateway):
        """"""
        super(OkexV5WebsocketPublicApi, self).__init__()
        self.ping_interval = 20  # OKEX use 30 seconds for ping

        self.gateway = gateway
        self.gateway_name = gateway.gateway_name

        self.usdt_base: bool = False

        self.subscribed: Dict[str, SubscribeRequest] = {}
        self.callbacks = {}
        self.ticks = {}

    def connect(
        self,
        usdt_base: bool,
        proxy_host: str,
        proxy_port: int,
        server: str
    ) -> None:
        """"""
        self.usdt_base = usdt_base

        if server == "REAL":
            self.init(PUBLIC_WEBSOCKET_HOST, proxy_host, proxy_port)
        else:
            self.init(SIMULATED_PUBLIC_WEBSOCKET_HOST, proxy_host, proxy_port)

    def subscribe(self, req: SubscribeRequest):
        """
        Subscribe to tick data upate.
        """
        self.callbacks["tickers"] = self.on_ticker
        self.callbacks["books5"] = self.on_depth

        if req.symbol not in symbol_contract_map:
            self.gateway.write_log(f"找不到该合约代码{req.symbol}")
            return

        self.subscribed[req.vt_symbol] = req

        tick = TickData(
            symbol=req.symbol,
            exchange=req.exchange,
            name=req.symbol,
            datetime=datetime.now(UTC_TZ),
            gateway_name=self.gateway_name,
        )
        self.ticks[req.symbol] = tick

        instId = req.symbol
        channel_ticker = {
            "channel": "tickers",
            "instId": instId
        }
        channel_depth = {
            "channel": "books5",
            "instId": instId
        }

        req = {
            "op": "subscribe",
            "args": [channel_ticker, channel_depth]
        }
        self.send_packet(req)

    def on_connected(self) -> None:
        """"""
        self.gateway.write_log("Websocket Public API连接成功")
        self.subscribe_public_topic()

        for req in list(self.subscribed.values()):
            self.subscribe(req)

    def on_disconnected(self):
        """"""
        self.gateway.write_log("Websocket Public API连接断开")

    def on_packet(self, packet: dict):
        """"""
        if "event" in packet:
            event = packet["event"]
            if event == "subscribe":
                return
            elif event == "error":
                code = packet["code"]
                msg = packet["msg"]
                self.gateway.write_log(f"Websocket Public API请求异常, 状态码：{code}, 信息{msg}")

        else:
            channel = packet["arg"]
            data = packet["data"]
            callback = self.callbacks.get(channel, None)

            if callback:
                for d in data:
                    callback(d)

    def on_error(self, exception_type: type, exception_value: Exception, tb):
        """"""
        msg = f"触发异常，状态码：{exception_type}，信息：{exception_value}"
        self.gateway.write_log(msg)

        sys.stderr.write(self.exception_detail(exception_type, exception_value, tb))

    def subscribe_public_topic(self):
        """
        Subscribe to all public topics.
        """
        self.callbacks["ticker"] = self.on_ticker
        self.callbacks["books5"] = self.on_depth

    def on_ticker(self, d):
        """"""
        symbol = d["insrId"]
        tick = self.ticks.get(symbol, None)
        if not tick:
            return

        # Filter last price with 0 value
        last_price = float(d["last"])
        if not last_price:
            return

        tick.last_price = last_price
        tick.high_price = float(d["high24h"])
        tick.low_price = float(d["low24h"])
        tick.volume = float(d["vol24h"])
        tick.datetime = _parse_timestamp(d["ts"])

        self.gateway.on_tick(copy(tick))

    def on_depth(self, d):
        """"""
        symbol = d["instId"]
        tick = self.ticks.get(symbol, None)
        if not tick:
            return

        bids = d["bids"]
        asks = d["asks"]
        for n in range(min(5, len(bids))):
            price, volume, _, _ = bids[n]
            tick.__setattr__("bid_price_%s" % (n + 1), float(price))
            tick.__setattr__("bid_volume_%s" % (n + 1), int(volume))

        for n in range(min(5, len(asks))):
            price, volume, _, _ = asks[n]
            tick.__setattr__("ask_price_%s" % (n + 1), float(price))
            tick.__setattr__("ask_volume_%s" % (n + 1), int(volume))

        tick.datetime = _parse_timestamp(d["timestamp"])
        self.gateway.on_tick(copy(tick))


class OkexV5WebsocketPrivateApi(WebsocketClient):
    """"""

    def __init__(self, gateway):
        """"""
        super(OkexV5WebsocketPrivateApi, self).__init__()
        self.ping_interval = 20  # OKEX use 30 seconds for ping

        self.gateway: OkexV5Gateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.usdt_base: bool = False

        self.key = ""
        self.secret = ""
        self.passphrase = ""

        self.callbacks = {}

    def connect(
        self,
        usdt_base: bool,
        key: str,
        secret: str,
        passphrase: str,
        proxy_host: str,
        proxy_port: int,
        server: str
    ) -> None:
        """"""
        self.usdt_base = usdt_base
        self.key = key
        self.secret = secret.encode()
        self.passphrase = passphrase

        if server == "REAL":
            self.init(PRIVATE_WEBSOCKET_HOST, proxy_host, proxy_port)
        else:
            self.init(SIMULATED_PRIVATE_WEBSOCKET_HOST, proxy_host, proxy_port)

    def on_connected(self):
        """"""
        self.gateway.write_log("Websocket Private API连接成功")
        self.login()

    def on_disconnected(self):
        """"""
        self.gateway.write_log("Websocket Private API连接断开")

    def on_packet(self, packet: dict):
        """"""
        if "event" in packet:
            event = packet["event"]
            if event == "subscribe":
                return
            elif event == "error":
                code = packet["code"]
                msg = packet["msg"]
                self.gateway.write_log(f"Websocket Private API请求异常, 状态码：{code}, 信息{msg}")
            elif event == "login":
                self.on_login(packet)

        else:
            channel = packet["arg"]
            data = packet["data"]
            callback = self.callbacks.get(channel, None)

            if callback:
                for d in data:
                    callback(d)

    def on_error(self, exception_type: type, exception_value: Exception, tb):
        """"""
        msg = f"触发异常，状态码：{exception_type}，信息：{exception_value}"
        self.gateway.write_log(msg)

        sys.stderr.write(self.exception_detail(exception_type, exception_value, tb))

    def login(self):
        """
        Need to login befores subscribe to websocket topic.
        """
        timestamp = str(time.time())

        msg = timestamp + "GET" + "/users/self/verify"
        signature = generate_signature(msg, self.secret)

        req = {
            "op": "login",
            "args":
            [
                {
                    "apiKey": self.key,
                    "passphrase": self.passphrase,
                    "timestamp": timestamp,
                    "sign": signature.decode("utf-8")
                }
            ]
        }
        self.send_packet(req)
        self.callbacks["login"] = self.on_login

    def subscribe_private_topic(self):
        """
        Subscribe to all private topics.
        """
        self.callbacks["orders"] = self.on_order
        self.callbacks["account"] = self.on_account
        self.callbacks["positions"] = self.on_position

        # Subscribe to order update
        req = {
            "op": "subscribe",
            "args": [{
                "channel": "orders",
                "instType": "ANY"
            }]
        }
        self.send_packet(req)

        # Subscribe to account update

        req = {
            "op": "subscribe",
            "args": [{
                "channel": "account"
            }]
        }
        self.send_packet(req)

        # Subscribe to position update
        req = {
            "op": "subscribe",
            "args": [{
                "channel": "positions",
                "instType": "ANY"
            }]
        }
        self.send_packet(req)

    def on_login(self, data: dict):
        """"""
        code = data["code"]

        if code == 0:
            self.gateway.write_log("Websocket Private API登录成功")
            self.subscribe_private_topic()

        else:
            self.gateway.write_log("Websocket Private API登录失败")

    def on_order(self, data):
        """"""
        order = _parse_order_data(data, gateway_name=self.gateway_name)
        self.gateway.on_order(copy(order))

        traded_volume = float(data.get("fillSz", 0))

        contract = symbol_contract_map.get(order.symbol, None)
        if contract:
            traded_volume = round_to(traded_volume, contract.min_volume)

        if traded_volume != 0:

            trade = TradeData(
                symbol=order.symbol,
                exchange=order.exchange,
                orderid=order.orderid,
                tradeid=data["tradeId"],
                direction=order.direction,
                offset=order.offset,
                price=float(data["fillPx"]),
                volume=traded_volume,
                datetime=order.datetime,
                gateway_name=self.gateway_name,
            )
            self.gateway.on_trade(trade)

    def on_account(self, data):
        """"""
        account = _parse_account_details(data, gateway_name=self.gateway_name)
        self.gateway.on_account(account)

    def on_position(self, data):
        """"""
        data = data['data']
        symbol = data['instId']

        long_position = PositionData(
            symbol=symbol,
            exchange=Exchange.OKEX,
            direction=Direction.LONG,
            gateway_name=self.gateway_name
        )

        short_position = PositionData(
            symbol=symbol,
            exchange=Exchange.OKEX,
            direction=Direction.SHORT,
            gateway_name=self.gateway_name
        )

        net_position = PositionData(
            symbol=symbol,
            exchange=Exchange.OKEX,
            direction=Direction.NET,
            gateway_name=self.gateway_name
        )

        for d in data:
            if d["posSide"] == "long":
                long_position = _parse_position_data(
                    data=d,
                    symbol=symbol,
                    gateway_name=self.gateway_name
                )
            elif d["posSide"] == "short":
                short_position = _parse_position_data(
                    data=d,
                    symbol=symbol,
                    gateway_name=self.gateway_name
                )
            elif d["posSide"] == "net":
                net_position = _parse_position_data(
                    data=d,
                    symbol=symbol,
                    gateway_name=self.gateway_name
                )

        self.gateway.on_position(long_position)
        self.gateway.on_position(short_position)
        self.gateway.on_position(net_position)


def generate_signature(msg: str, secret_key: str):
    """OKEX V5 signature"""
    return base64.b64encode(hmac.new(secret_key, msg.encode(), hashlib.sha256).digest())


def generate_timestamp():
    """"""
    now = datetime.utcnow()
    timestamp = now.isoformat("T", "milliseconds")
    return timestamp + "Z"


def _parse_timestamp(timestamp):
    """parse timestamp into local time."""
    timestamp = eval(timestamp)
    dt = datetime.fromtimestamp(timestamp/1000)
    dt = UTC_TZ.localize(dt)
    return dt


def _parse_position_data(data, symbol, gateway_name):
    """parse single 'data' record in replied position data to PositionData. """
    position = int(data["pos"])
    pos = PositionData(
        symbol=symbol,
        exchange=Exchange.OKEX,
        direction=DIRECTION_OKEXV52VT[data['posSide']],
        volume=position,
        frozen=float(position - float(data["availPos"])),
        price=float(data['avgPx']),
        pnl=float(data['upl']),
        gateway_name=gateway_name,
    )
    return pos


def _parse_account_details(details, gateway_name):
    """
    parse single 'details' record inside account reply to AccountData.
    """
    account = AccountData(
        accountid=details['ccy'].upper(),
        balance=float(details["eq"]),
        frozen=float(details["ordFrozen"]),
        gateway_name=gateway_name,
    )
    return account


def _parse_order_data(data, gateway_name: str):
    posside = DIRECTION_OKEXV52VT[data["posSide"]]
    side = SIDE_OKEXV52VT[data["side"]]

    order_id = data["clOrdId"]
    if not order_id:
        order_id = data["ordId"]
    order = OrderData(
        symbol=data["instId"],
        exchange=Exchange.OKEX,
        type=ORDERTYPE_OKEXV52VT[data["ordType"]],
        orderid=order_id,
        direction=side,
        traded=float(data["accFillSz"]),
        price=float(data["px"]),
        volume=float(data["sz"]),
        datetime=_parse_timestamp(data["uTime"]),
        status=STATUS_OKEXV52VT[data["state"]],
        gateway_name=gateway_name,
    )
    if posside == Direction.NET:
        order.offset == Offset.NONE
    elif posside == Direction.LONG:
        if side == Direction.LONG:
            order.offset == Offset.OPEN
        else:
            order.offset == Offset.CLOSE
    else:
        if side == Direction.LONG:
            order.offset == Offset.CLOSE
        else:
            order.offset == Offset.OPEN
    return order
