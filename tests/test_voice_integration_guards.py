"""Confirmation and shutdown guards for the real same-process DispatchPort.

No test places a call or accesses Vapi; only the simulated unit path is enabled.
"""

import asyncio

import pytest

from voice.config import Settings
from voice.ports import VoiceService
from voice.tools import confirm_plan
import voice.tools as tools


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeVapi:
    def __init__(self, say_ok=True):
        self.say_ok = say_ok
        self.spoken = []

    async def say(self, url, text):
        self.spoken.append(text)
        return self.say_ok

    async def add_message(self, url, content, **kwargs):
        return True

    async def create_call(self, *args, **kwargs):
        pytest.fail("Integration guard tests must never place real calls")


class AuthoritativeDispatch:
    def __init__(self):
        self.confirms = []
        self.reject = False
        self.gate = None

    async def on_pilot_confirm(self, incident_id, readback):
        if self.reject:
            raise ValueError("The response plan is not ready")
        if self.gate is not None:
            await self.gate.wait()
        self.confirms.append((incident_id, readback))

    async def on_event(self, event):
        pass


def make_service(tmp_path, *, say_ok=True):
    dispatch = AuthoritativeDispatch()
    service = VoiceService(
        Settings(log_dir=tmp_path, sim_units=True),
        FakeVapi(say_ok),
        dispatch=dispatch,
    )
    service.registry.bind_incident("pilot-1", "incident-1")
    pilot = service.registry.pilot("pilot-1")
    pilot.control_url = "https://fake.invalid/control"
    return service, dispatch, pilot


@pytest.mark.anyio
async def test_failed_briefing_cannot_be_confirmed(tmp_path):
    service, dispatch, pilot = make_service(tmp_path, say_ok=False)
    try:
        await service.brief_pilot("incident-1", "Fire and medical responders accepted.")
        assert pilot.plan is not None
        assert pilot.plan_delivered is False
        result = await confirm_plan(service, "pilot-1", {"readback": "Wilco"})
        assert "No plan has been issued" in result
        assert dispatch.confirms == []
        assert pilot.confirmed is False
    finally:
        await service.aclose()


@pytest.mark.anyio
async def test_backend_rejection_leaves_confirmation_retryable(tmp_path):
    service, dispatch, pilot = make_service(tmp_path)
    try:
        await service.brief_pilot("incident-1", "Fire and medical responders accepted.")
        assert pilot.plan_delivered is True
        dispatch.reject = True
        result = await confirm_plan(service, "pilot-1", {"readback": "Wilco"})
        assert "could not be confirmed" in result
        assert pilot.confirmed is False
        assert pilot.confirming is False
        dispatch.reject = False
        result = await confirm_plan(service, "pilot-1", {"readback": "Wilco"})
        assert result.startswith("Plan confirmed")
        assert pilot.confirmed is True
        assert dispatch.confirms == [("incident-1", "Wilco")]
    finally:
        await service.aclose()


@pytest.mark.anyio
async def test_ended_pilot_cannot_confirm_a_previously_delivered_plan(tmp_path):
    service, dispatch, pilot = make_service(tmp_path)
    try:
        await service.brief_pilot("incident-1", "Fire and medical responders accepted.")
        pilot.ended = True
        result = await confirm_plan(service, "pilot-1", {"readback": "Wilco"})
        assert "call has ended" in result
        assert pilot.confirmed is False
        assert dispatch.confirms == []
    finally:
        await service.aclose()


@pytest.mark.anyio
async def test_backend_timeout_does_not_mark_confirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "CONFIRM_TIMEOUT_S", 0.01)
    service, dispatch, pilot = make_service(tmp_path)
    dispatch.gate = asyncio.Event()
    try:
        await service.brief_pilot("incident-1", "Fire and medical responders accepted.")
        result = await confirm_plan(service, "pilot-1", {"readback": "Wilco"})
        assert "could not be confirmed" in result
        assert pilot.confirmed is False
        assert pilot.confirming is False
        assert dispatch.confirms == []
    finally:
        await service.aclose()


@pytest.mark.anyio
async def test_concurrent_confirmations_await_one_backend_decision(tmp_path):
    service, dispatch, pilot = make_service(tmp_path)
    dispatch.gate = asyncio.Event()
    try:
        await service.brief_pilot("incident-1", "Fire and medical responders accepted.")
        first = asyncio.create_task(confirm_plan(service, "pilot-1", {"readback": "Wilco"}))
        await asyncio.sleep(0)
        assert pilot.confirming is True
        assert pilot.confirmed is False
        second_result = await confirm_plan(service, "pilot-1", {"readback": "Wilco"})
        assert "being recorded" in second_result
        dispatch.gate.set()
        assert (await first).startswith("Plan confirmed")
        assert dispatch.confirms == [("incident-1", "Wilco")]
    finally:
        await service.aclose()


@pytest.mark.anyio
async def test_aclose_cancels_and_awaits_background_work(tmp_path):
    service, _, _ = make_service(tmp_path)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def wait_forever():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = service.spawn(wait_forever(), name="shutdown-guard")
    await started.wait()
    await service.aclose()
    assert task.cancelled()
    assert stopped.is_set()
    assert service._tasks == set()
    await service.aclose()  # shutdown is idempotent
