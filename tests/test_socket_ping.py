"""The app's foreground liveness check gets an answer.

Run: uv run python -m unittest tests.test_socket_ping -v
"""

import unittest

from cptr.socket import main as socket_main


class SocketPingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping_is_acknowledged(self):
        # Returning a value is what Socket.IO sends back as the ack.
        self.assertIs(await socket_main.on_ping("sid"), True)
        self.assertIs(await socket_main.on_ping("sid", {"t": 1}), True)

    def test_ping_handler_is_registered(self):
        handlers = socket_main.sio.handlers.get("/", {})
        self.assertIs(handlers.get("cptr:ping"), socket_main.on_ping)


if __name__ == "__main__":
    unittest.main()
