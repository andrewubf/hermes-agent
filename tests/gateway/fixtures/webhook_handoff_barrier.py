"""Stop the owned daemon after source commit, before any Bot Chat transport call."""
import asyncio
import os
import runpy
import signal
from pathlib import Path

from gateway.platforms import webhook_bot_chat


async def stop_before_handoff(*args, **kwargs):
    await asyncio.to_thread(Path(os.environ['TEST_HANDOFF_BARRIER']).write_text, 'before handoff')
    os.kill(os.getpid(), signal.SIGSTOP)


webhook_bot_chat.deliver_result = stop_before_handoff
runpy.run_module('gateway.run', run_name='__main__')
