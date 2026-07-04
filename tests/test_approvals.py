import asyncio

import pytest

from voicecode.server.approvals import Approvals


class Notify:
    def __init__(self) -> None:
        self.messages: list[object] = []

    async def __call__(self, message: object) -> None:
        self.messages.append(message)


async def pushed_request(notify: Notify):
    while not notify.messages:
        await asyncio.sleep(0.001)
    return notify.messages[0]


async def test_create_pushes_request_and_allow_resolves():
    notify = Notify()
    approvals = Approvals(notify, timeout=1.0)
    task = asyncio.create_task(approvals.create("s1", "Bash", "rm -rf /tmp/x"))
    request = await pushed_request(notify)
    assert request.type == "approval_request"
    assert (request.session, request.tool, request.summary) == ("s1", "Bash", "rm -rf /tmp/x")
    assert request.approval_id in approvals.pending

    approvals.resolve(request.approval_id, True)
    assert await task is True
    assert approvals.pending == {}


async def test_deny_resolves_false():
    notify = Notify()
    approvals = Approvals(notify, timeout=1.0)
    task = asyncio.create_task(approvals.create("s1", "Bash", "sudo rm x"))
    request = await pushed_request(notify)
    approvals.resolve(request.approval_id, False)
    assert await task is False


async def test_timeout_raises_and_cleans_up():
    approvals = Approvals(Notify(), timeout=0.02)
    with pytest.raises(TimeoutError):
        await approvals.create("s1", "Bash", "x")
    assert approvals.pending == {}


async def test_resolve_unknown_or_repeated_id_is_a_noop():
    notify = Notify()
    approvals = Approvals(notify, timeout=1.0)
    approvals.resolve("missing", True)  # no pending future — nothing happens
    task = asyncio.create_task(approvals.create("s1", "Bash", "x"))
    request = await pushed_request(notify)
    approvals.resolve(request.approval_id, True)
    approvals.resolve(request.approval_id, False)  # late duplicate is ignored
    assert await task is True
