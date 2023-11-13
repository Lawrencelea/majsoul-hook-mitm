import base64

from struct import unpack, pack
from enum import Enum
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.message import Message
from mitmproxy import http, ctx
from dataclasses import dataclass

from . import liqi_pb2 as pb


""" 
    # msg_block notify
    [   {'id': 1, 'type': 'string','data': b'.lq.ActionPrototype'},
        {'id': 2, 'type': 'string','data': b'protobuf_bytes'}       ]
    # msg_block request & response
    [   {'id': 1, 'type': 'string','data': b'.lq.FastTest.authGame'},
        {'id': 2, 'type': 'string','data': b'protobuf_bytes'}       ]
"""


class MsgType(Enum):
    """Game websocket message type"""

    Notify = 1
    Req = 2
    Res = 3


@dataclass
class Msg:
    """Game websocket message struct"""

    prototype: Message
    type: MsgType
    data: dict
    method: str

    id: int = -1

    def __post_init__(self):
        """
        ('.lq.FastTest.authGame', MsgType.Req) => _lq_FastTest_authGame_Req
        """
        self.func = "_".join([*self.method.split("."), self.type.name])

    @classmethod
    def notify(cls, data: dict, method: str, id: int = -1):
        prototype = Proto.getPrototype(method, MsgType.Notify)
        return cls(prototype, MsgType.Notify, data, method, id)

    @classmethod
    def req(cls, data: dict, method: str, id: int):
        prototype = Proto.getPrototype(method, MsgType.Req)
        return cls(prototype, MsgType.Req, data, method, id)

    @classmethod
    def res(cls, data: dict, method: str, id: int):
        prototype = Proto.getPrototype(method, MsgType.Res)
        return cls(prototype, MsgType.Res, data, method, id)


class Plugin:
    def handle(self, flow: http.HTTPFlow, msg: Msg) -> bool:
        """
        Try invoking `getattr(self, msg.func)(flow, msg)`, return True -> Skipped | return False -> Manipulated
        """
        if hasattr(self, msg.func):
            return bool(getattr(self, msg.func)(flow, msg))
        else:
            return True

    def reply(
        self,
        flow: http.HTTPFlow,
        req_msg: Msg,
        res_data: dict = dict(),
        notifys: list[dict] = list(),
    ) -> bool:
        assert req_msg.type is MsgType.Req

        # drop latest request
        flow.websocket.messages[-1].drop()

        # compose response
        res_msg = Msg.res(
            res_data,
            req_msg.method,
            req_msg.id,
        )

        # inject messages
        Proto.inject(flow, *notifys, res_msg)

        # mark as skipped
        return True


class Proto:
    def __init__(self) -> None:
        self.tot = 0
        self.res_type = dict()

    def parse(self, flow: http.HTTPFlow) -> Msg:
        flow_msg = flow.websocket.messages[-1]
        buf = flow_msg.content
        msg_type = MsgType(buf[0])

        if msg_type == MsgType.Notify:
            msg_block = fromProtobuf(buf[1:])
            method_name = msg_block[0]["data"].decode()
            prototype = self.getPrototype(method_name, msg_type)
            proto_obj = prototype.FromString(msg_block[1]["data"])
            dict_obj = MessageToDict(
                proto_obj,
                preserving_proto_field_name=True,
                including_default_value_fields=True,
            )

            if "data" in dict_obj:
                B = base64.b64decode(dict_obj["data"])
                action_proto_obj = getattr(pb, dict_obj["name"]).FromString(decode(B))
                action_dict_obj = MessageToDict(
                    action_proto_obj,
                    preserving_proto_field_name=True,
                    including_default_value_fields=True,
                )
                dict_obj["data"] = action_dict_obj

            msg_id = self.tot
        else:
            msg_id = unpack("<H", buf[1:3])[0]
            msg_block = fromProtobuf(buf[3:])
            if msg_type == MsgType.Req:
                assert msg_id < 1 << 16
                assert len(msg_block) == 2
                assert msg_id not in self.res_type
                method_name = msg_block[0]["data"].decode()
                prototype = self.getPrototype(method_name, msg_type)
                proto_obj = prototype.FromString(msg_block[1]["data"])
                dict_obj = MessageToDict(
                    proto_obj,
                    preserving_proto_field_name=True,
                    including_default_value_fields=True,
                )

                self.res_type[msg_id] = (
                    method_name,
                    self.getPrototype(method_name, MsgType.Res),
                )
            elif msg_type == MsgType.Res:
                assert len(msg_block[0]["data"]) == 0
                assert msg_id in self.res_type
                method_name, prototype = self.res_type.pop(msg_id)
                proto_obj = prototype.FromString(msg_block[1]["data"])
                dict_obj = MessageToDict(
                    proto_obj,
                    preserving_proto_field_name=True,
                    including_default_value_fields=True,
                )

                if "game_restore" in dict_obj:
                    for action in dict_obj["game_restore"]["actions"]:
                        b64 = base64.b64decode(action["data"])
                        action_proto_obj = getattr(pb, action["name"]).FromString(b64)
                        action_dict_obj = MessageToDict(
                            action_proto_obj,
                            preserving_proto_field_name=True,
                            including_default_value_fields=True,
                        )
                        action["data"] = action_dict_obj
        self.tot += 1
        return Msg(prototype, msg_type, dict_obj, method_name, msg_id)

    @classmethod
    def compose(cls, msg: Msg) -> bytes:
        head = msg.type.value.to_bytes(length=1, byteorder="little")
        proto_obj = ParseDict(js_dict=msg.data, message=msg.prototype())
        msg_block = [{"id": 1, "type": "string"}, {"id": 2, "type": "string"}]

        if msg.type == MsgType.Notify:
            if "data" in msg.data:
                """Not yet supported"""
                raise NotImplementedError

            msg_block[0]["data"] = msg.method.encode()
            msg_block[1]["data"] = proto_obj.SerializeToString()
            return head + toProtobuf(msg_block)
        elif msg.type == MsgType.Req:
            msg_block[0]["data"] = msg.method.encode()
            msg_block[1]["data"] = proto_obj.SerializeToString()
            return head + pack("<H", msg.id) + toProtobuf(msg_block)
        elif msg.type == MsgType.Res:
            msg_block[0]["data"] = b""
            msg_block[1]["data"] = proto_obj.SerializeToString()
            return head + pack("<H", msg.id) + toProtobuf(msg_block)

    @staticmethod
    def getPrototype(method_name: str, msg_type: MsgType):
        if msg_type == MsgType.Notify:
            _, lq, message_name = method_name.split(".")
            return getattr(pb, message_name)

        else:
            _, lq, service, rpc = method_name.split(".")
            method_desc = pb.DESCRIPTOR.services_by_name[service].methods_by_name[rpc]

            if msg_type == MsgType.Req:
                return getattr(pb, method_desc.input_type.name)
            elif msg_type == MsgType.Res:
                return getattr(pb, method_desc.output_type.name)

    @classmethod
    def manipulate(cls, flow: http.HTTPFlow, msg: Msg) -> None:
        last_message = flow.websocket.messages[-1]
        last_message.content = cls.compose(msg=msg)

    @classmethod
    def inject(cls, flow: http.HTTPFlow, *msgs: dict) -> None:
        for msg in msgs:
            # inject.websocket flow to_client message is_text
            ctx.master.commands.call(
                "inject.websocket", flow, True, cls.compose(msg=msg), False
            )


def fromProtobuf(buf) -> list[dict]:
    p = 0
    result = []
    while p < len(buf):
        block_begin = p
        block_type = buf[p] & 7
        block_id = buf[p] >> 3
        p += 1
        if block_type == 0:
            block_type = "varint"
            data, p = parseVarint(buf, p)
        elif block_type == 2:
            block_type = "string"
            s_len, p = parseVarint(buf, p)
            data = buf[p : p + s_len]
            p += s_len
        else:
            raise Exception("unknow type:", block_type, "at", p)
        result.append(
            {"id": block_id, "type": block_type, "data": data, "begin": block_begin}
        )
    return result


def toProtobuf(data: list[dict]) -> bytes:
    result = b""
    for d in data:
        if d["type"] == "varint":
            result += ((d["id"] << 3) + 0).to_bytes(length=1, byteorder="little")
            result += toVarint(d["data"])
        elif d["type"] == "string":
            result += ((d["id"] << 3) + 2).to_bytes(length=1, byteorder="little")
            result += toVarint(len(d["data"]))
            result += d["data"]
        else:
            raise NotImplementedError
    return result


def parseVarint(buf, p):
    data = 0
    base = 0
    while p < len(buf):
        data += (buf[p] & 127) << base
        base += 7
        p += 1
        if buf[p - 1] >> 7 == 0:
            break
    return (data, p)


def toVarint(x: int) -> bytes:
    data = 0
    base = 0
    length = 0
    if x == 0:
        return b"\x00"
    while x > 0:
        length += 1
        data += (x & 127) << base
        x >>= 7
        if x > 0:
            data += 1 << (base + 7)
        base += 8
    return data.to_bytes(length, "little")


def decode(data: bytes):
    keys = [0x84, 0x5E, 0x4E, 0x42, 0x39, 0xA2, 0x1F, 0x60, 0x1C]
    data = bytearray(data)
    for i in range(len(data)):
        u = (23 ^ len(data)) + 5 * i + keys[i % len(keys)] & 255
        data[i] ^= u
    return bytes(data)