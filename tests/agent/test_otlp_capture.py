"""A6-05：OTLP 本地接收器 + 最小 protobuf 解码探针的负对照。

为什么要有这个测试文件：
验收脚本出问题时最常见的两种假绿：
  1) 接收器永远返回 200 —— 那「POST 成功」什么都不证明；
  2) 解码器把空报文当成合法的 0 条 span —— 那「查不到」看起来像「没数据」。
这里两条都钉死。
"""

from __future__ import annotations

import unittest
import urllib.error
import urllib.request

from .otlp_capture import CaptureServer, parse_message


class CaptureServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = CaptureServer().start()

    def tearDown(self) -> None:
        self.server.stop()

    def _post(self, path: str, data: bytes) -> int:
        request = urllib.request.Request(self.server.endpoint + path, data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_unknown_path_is_not_200(self) -> None:
        """负对照：随便一个路径必须 404，否则「发成功了」这句话毫无意义。"""
        self.assertEqual(self._post("/v1/traces-wrong", b""), 404)

    def test_malformed_payload_is_rejected(self) -> None:
        self.assertEqual(self._post("/v1/traces", b"\xff\xff\xff"), 400)

    def test_valid_empty_batch_is_accepted_and_decodes_to_nothing(self) -> None:
        self.assertEqual(self._post("/v1/traces", b""), 200)
        self.assertEqual(self.server.recorder.spans(), [])

    def test_three_signals_are_kept_apart(self) -> None:
        self._post("/v1/traces", b"")
        self._post("/v1/metrics", b"")
        self._post("/v1/logs", b"")
        self.assertEqual([b["signal"] for b in self.server.recorder.batches], ["traces", "metrics", "logs"])

    def test_wire_format_parser_reads_strings_and_varints(self) -> None:
        # 字段 1（wire 2）="hi"，字段 2（wire 0）=3
        payload = bytes([0x0A, 0x02]) + b"hi" + bytes([0x10, 0x03])
        fields = parse_message(payload)
        self.assertEqual(fields[1][0][1], b"hi")
        self.assertEqual(fields[2][0][1], 3)


if __name__ == "__main__":
    unittest.main()
