# -*- coding: utf-8 -*-
"""xdev MCP (streamable HTTP) の最小クライアント。tools/call だけできればよい。"""
import json
import itertools
import threading
import requests


class McpClient:
    def __init__(self, url, timeout=120):
        self.url = url
        self.timeout = timeout
        self.session_id = None
        self._ids = itertools.count(1)
        self._http = requests.Session()
        # requests.Session はスレッドセーフでないため、並列ワーカーからの呼び出しは直列化する
        self._lock = threading.Lock()
        self._initialize()

    def _headers(self):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _post(self, payload):
        with self._lock:
            return self._post_locked(payload)

    def _post_locked(self, payload):
        r = self._http.post(self.url, json=payload, headers=self._headers(),
                            timeout=self.timeout)
        r.raise_for_status()
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        body = r.content.decode("utf-8")  # charset無指定SSEをlatin-1誤判定させない
        ctype = r.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            msg, data = None, []
            for line in body.split("\n"):
                line = line.rstrip("\r")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
                elif line == "" and data:  # イベント境界
                    try:
                        msg = json.loads("\n".join(data))
                    except ValueError:
                        pass
                    data = []
            if data:
                try:
                    msg = json.loads("\n".join(data))
                except ValueError:
                    pass
            return msg
        if body.strip():
            return json.loads(body)
        return None

    def _initialize(self):
        res = self._post({
            "jsonrpc": "2.0", "id": next(self._ids), "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "nara-spots-crj", "version": "1.0"}}})
        if res is None or "error" in res:
            raise RuntimeError(f"MCP initialize failed: {res}")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool, arguments):
        res = self._post({
            "jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}})
        if res is None:
            raise RuntimeError(f"MCP {tool}: empty response")
        if "error" in res:
            raise RuntimeError(f"MCP {tool}: {res['error']}")
        result = res.get("result", {})
        if result.get("isError"):
            raise RuntimeError(f"MCP {tool} tool error: {result}")
        if "structuredContent" in result:
            return result["structuredContent"]
        texts = [c.get("text", "") for c in result.get("content", [])
                 if c.get("type") == "text"]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except ValueError:
            return joined
