"""A6-05 探针：OTLP 三信号本地接收器（零第三方依赖的 HTTP/protobuf 解码）。

为什么自己解码 protobuf：
本项目的测试环境只有一个 venv，装 grpcio 会把「核心零依赖」这条线弄脏。
但「POST 成功」不等于「Collector 收到了完整的 trace」，所以这里用最朴素的
protobuf wire format 把 OTLP 报文解回字段名，逐条断言 span / metric / log 的内容。

用法：python otlp_capture.py [--port 4318] [--out capture.json]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def parse_message(data: bytes) -> dict[int, list[tuple[int, object]]]:
    """解一层 protobuf：返回 {字段号: [(wire_type, 值)]}。不认识的 wire type 直接失败。"""
    fields: dict[int, list[tuple[int, object]]] = {}
    pos = 0
    length = len(data)
    while pos < length:
        key, pos = read_varint(data, pos)
        field_no = key >> 3
        wire = key & 0x7
        if wire == 0:
            value, pos = read_varint(data, pos)
        elif wire == 1:
            value = data[pos:pos + 8]
            pos += 8
        elif wire == 2:
            size, pos = read_varint(data, pos)
            value = data[pos:pos + size]
            pos += size
        elif wire == 5:
            value = data[pos:pos + 4]
            pos += 4
        else:
            raise ValueError("unsupported wire type " + str(wire))
        fields.setdefault(field_no, []).append((wire, value))
    return fields




def rawhex(value: object) -> str:
    """把 wire 里的字节值转成十六进制串；不是字节就返回空串。

    注意不要把 bytes 先 text() 再 .hex()：text() 会做 utf-8 解码，
    解码后的 str 没有 .hex() 方法（实测 AttributeError: str has no attribute hex）。
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return ""

def text(value: object) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def sub(fields: dict, number: int) -> list[dict]:
    out = []
    for wire, value in fields.get(number, []):
        if wire == 2:
            out.append(parse_message(value))
    return out


def strings(fields: dict, number: int) -> list[str]:
    return [text(value) for wire, value in fields.get(number, []) if wire == 2]


def decode_any_value(fields: dict) -> str:
    for number in (1, 3):
        for wire, value in fields.get(number, []):
            if wire == 2:
                try:
                    return text(value)
                except Exception:
                    pass
    return ""


def attributes(fields: dict, number: int = 9) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in sub(fields, number):
        key = strings(item, 1)
        values = sub(item, 2)
        if key:
            out[key[0]] = decode_any_value(values[0]) if values else ""
    return out


def decode_spans(payload: dict) -> list[dict]:
    spans = []
    for resource in sub(payload, 1):
        resource_attrs = attributes((sub(resource, 1) or [{}])[0])
        for scope in sub(resource, 2):
            for span in sub(scope, 2):
                spans.append({
                    "trace_id": rawhex(span.get(1, [(0, b"")])[0][1]),
                    "span_id": rawhex(span.get(2, [(0, b"")])[0][1]),
                    "name": (strings(span, 5) or [""])[0],
                    "kind": span.get(6, [(0, 0)])[0][1],
                    "start": span.get(7, [(0, 0)])[0][1],
                    "end": span.get(8, [(0, 0)])[0][1],
                    "attributes": attributes(span),
                    "status": span.get(15, [(0, 0)])[0][1] if span.get(15) else 0,
                    "resource": resource_attrs,
                })
    return spans


def decode_metrics(payload: dict) -> list[dict]:
    out = []
    for resource in sub(payload, 1):
        for scope in sub(resource, 2):
            for metric in sub(scope, 2):
                name = (strings(metric, 1) or [""])[0]
                kinds = {2: "gauge", 3: "sum", 4: "histogram", 5: "exponential_histogram", 7: "summary"}
                kind = next((label for number, label in kinds.items() if number in metric), "unknown")
                numbers = []
                for number in kinds:
                    for group in metric.get(number, []):
                        if not isinstance(group, (bytes, bytearray)):
                            continue
                        body = parse_message(group)
                        for point in sub(body, 1):
                            for field in (3, 4, 5):
                                for wire, value in point.get(field, []):
                                    if wire == 1:
                                        import struct

                                        numbers.append(struct.unpack("<d", value)[0])
                                    elif wire == 0:
                                        numbers.append(value)
                            for attr in point.get(7, []):
                                pass
                out.append({"name": name, "kind": kind, "values": numbers})
    return out


def decode_logs(payload: dict) -> list[dict]:
    out = []
    for resource in sub(payload, 1):
        for scope in sub(resource, 2):
            for record in sub(scope, 2):
                body = ""
                for item in sub(record, 5):
                    body = decode_any_value(item)
                out.append({
                    "trace_id": rawhex(record.get(9, [(0, b"")])[0][1]),
                    "span_id": rawhex(record.get(10, [(0, b"")])[0][1]),
                    "severity": record.get(2, [(0, 0)])[0][1] if record.get(2) else 0,
                    "body": body,
                    "attributes": attributes(record, 6),
                })
    return out


class Recorder:
    def __init__(self) -> None:
        self.batches: list[dict] = []
        self.lock = threading.Lock()

    def add(self, signal: str, payload: dict) -> None:
        with self.lock:
            self.batches.append({"signal": signal, "payload": payload})

    def spans(self) -> list[dict]:
        out: list[dict] = []
        for batch in self.batches:
            if batch["signal"] == "traces":
                out.extend(decode_spans(batch["payload"]))
        return out

    def metrics(self) -> list[dict]:
        out: list[dict] = []
        for batch in self.batches:
            if batch["signal"] == "metrics":
                out.extend(decode_metrics(batch["payload"]))
        return out

    def logs(self) -> list[dict]:
        out: list[dict] = []
        for batch in self.batches:
            if batch["signal"] == "logs":
                out.extend(decode_logs(batch["payload"]))
        return out


def make_handler(recorder: Recorder):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            size = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(size) if size else b""
            signal = {
                "/v1/traces": "traces",
                "/v1/metrics": "metrics",
                "/v1/logs": "logs",
            }.get(self.path, "")
            if not signal:
                self.send_response(404)
                self.end_headers()
                return
            try:
                recorder.add(signal, parse_message(raw))
            except Exception as exc:  # noqa: BLE001
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(exc).encode("utf-8"))
                return
            self.send_response(200)
            self.send_header("content-type", "application/x-protobuf")
            self.end_headers()
            self.wfile.write(b"")

        def log_message(self, *args) -> None:
            return

    return Handler


class CaptureServer:
    """本地 OTLP/HTTP 接收器，端口由系统分配（避免和真实 Collector 抢 4318）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.recorder = Recorder()
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self.recorder))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return "http://127.0.0.1:" + str(self.port)

    def start(self) -> "CaptureServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="OTLP 三信号本地接收器")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    server = CaptureServer(port=args.port).start()
    print(json.dumps({"endpoint": server.endpoint, "port": server.port}), flush=True)
    import time

    time.sleep(args.seconds)
    server.stop()
    summary = {"spans": server.recorder.spans(), "metrics": server.recorder.metrics(), "logs": server.recorder.logs()}
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"spans": len(summary["spans"]), "metrics": len(summary["metrics"]), "logs": len(summary["logs"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())