"""Test doubles for the copy tool: two local stores served by the REAL MCP tool
functions of memora.server, selected the way /mcp/<db> selects them."""

import asyncio
import json

from memora import copy_memories as cm
from memora import server, storage
from memora.storage import CURRENT_DB

WRITE_TOOLS = {"memory_import", "memory_link", "memory_update", "memory_create", "memory_delete"}


class InProcessEndpoint(cm.Endpoint):
    """Calls memora.server's tool functions with CURRENT_DB bound to `store`.

    `calls` records (tool, args). `hook(tool, args, phase)` can raise to
    simulate a transport failure: phase 'before' (the request never reached
    the server) or 'after' (the server committed, the reply was lost)."""

    def __init__(self, store, *, health=None):
        self.store = store
        self.calls = []
        self.hook = None
        self._health = health or {"http": 200, "status": "ok", "freeze": {"state": "open", "in_flight": 0}}

    def call(self, tool, args):
        self.calls.append((tool, json.loads(json.dumps(args))))
        if self.hook:
            self.hook(tool, args, "before")
        token = CURRENT_DB.set(self.store)
        try:
            out = asyncio.run(getattr(server, tool)(**args))
        finally:
            CURRENT_DB.reset(token)
        if self.hook:
            self.hook(tool, args, "after")
        return json.loads(json.dumps(out, default=str))

    def health(self):
        return dict(self._health)

    def writes(self):
        return [c for c in self.calls if c[0] in WRITE_TOOLS]


def in_store(name):
    class _Ctx:
        def __enter__(self):
            self.token = CURRENT_DB.set(name)
            self.conn = storage.connect()
            return self.conn

        def __exit__(self, *exc):
            self.conn.close()
            CURRENT_DB.reset(self.token)
    return _Ctx()
