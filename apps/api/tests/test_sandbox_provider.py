"""Provider-layer tests. No network, no key, no real microVM.

The E2B tests drive the real SDK model classes (`Execution`, `Result`, `Logs`,
`CommandExitException`) through a stand-in `Sandbox` class. That is the point:
asserting against hand-rolled dicts would pass forever while the SDK renamed a
field underneath us, whereas constructing a real `Result` fails the day its
shape changes — which is the only kind of failure worth having here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from e2b import CommandExitException, NotFoundException, SandboxException
from e2b_code_interpreter.models import Execution, ExecutionError, Logs, Result
from pydantic import SecretStr

from app.config import Settings
from app.services.sandbox import e2b_provider
from app.services.sandbox.e2b_provider import E2BProvider
from app.services.sandbox.fake import FakeProvider
from app.services.sandbox.policy import ALL_TRAFFIC, ALL_TRAFFIC_V6, ALWAYS_DENIED_CIDRS
from app.services.sandbox.provider import get_provider, reset_provider_cache
from app.services.sandbox.types import ExecResult, SandboxError, SandboxHandle, SandboxSpec

API_KEY = "e2b_secret_key_do_not_leak"
HANDLE = SandboxHandle(provider="e2b", external_id="sbx-1")


def _settings(**overrides: Any) -> Settings:
    base: Dict[str, Any] = {"app_env": "development", "sandbox_enabled": True}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    # The driver cache outlives a test by design (a FakeProvider has to keep its
    # sandboxes between calls), so each test starts from an empty one.
    reset_provider_cache()
    yield
    reset_provider_cache()


class StubSandbox:
    """Stands in for `e2b_code_interpreter.Sandbox`.

    Records the kwargs `create` was called with, since the egress policy only
    exists if it actually reaches the SDK.
    """

    created: List[Dict[str, Any]] = []
    connected: List[Dict[str, Any]] = []
    killed: List[str] = []
    execution: Optional[Execution] = None
    raise_on_create: Optional[BaseException] = None
    raise_on_kill: Optional[BaseException] = None
    command_result: Any = None

    def __init__(self, sandbox_id: str = "sbx-1") -> None:
        self.sandbox_id = sandbox_id
        self.commands = _StubCommands(self)

    @classmethod
    def reset(cls) -> None:
        cls.created = []
        cls.connected = []
        cls.killed = []
        cls.execution = None
        cls.raise_on_create = None
        cls.raise_on_kill = None
        cls.command_result = None

    @classmethod
    def create(cls, **kwargs: Any) -> StubSandbox:
        if cls.raise_on_create is not None:
            raise cls.raise_on_create
        cls.created.append(kwargs)
        return cls()

    @classmethod
    def connect(cls, sandbox_id: str, **kwargs: Any) -> StubSandbox:
        cls.connected.append({"sandbox_id": sandbox_id, **kwargs})
        return cls(sandbox_id)

    @classmethod
    def kill(cls, sandbox_id: str, **kwargs: Any) -> bool:
        if cls.raise_on_kill is not None:
            raise cls.raise_on_kill
        cls.killed.append(sandbox_id)
        return True

    def run_code(self, code: str, **kwargs: Any) -> Execution:
        on_stdout = kwargs.get("on_stdout")
        execution = type(self).execution or Execution()
        if on_stdout is not None:
            for line in execution.logs.stdout:
                on_stdout(_Message(line))
        return execution


class _Message:
    def __init__(self, line: str) -> None:
        self.line = line


class _StubCommands:
    def __init__(self, sandbox: StubSandbox) -> None:
        self._sandbox = sandbox

    def run(self, cmd: str, **kwargs: Any) -> Any:
        result = type(self._sandbox).command_result
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture()
def stub_sdk(monkeypatch: pytest.MonkeyPatch) -> Any:
    StubSandbox.reset()
    monkeypatch.setattr(e2b_provider, "Sandbox", StubSandbox)
    return StubSandbox


def _provider() -> E2BProvider:
    return E2BProvider(api_key=API_KEY, template="")


# --- the factory ---------------------------------------------------------


def test_get_provider_returns_the_configured_driver_and_caches_it() -> None:
    settings = _settings(sandbox_provider="fake")
    first = get_provider(settings)
    assert isinstance(first, FakeProvider)
    # Same settings must hand back the same instance, or a FakeProvider would
    # lose every sandbox it created between two calls.
    assert get_provider(_settings(sandbox_provider="fake")) is first


def test_get_provider_builds_the_e2b_driver_when_configured() -> None:
    provider = get_provider(
        _settings(sandbox_provider="e2b", sandbox_api_key=SecretStr(API_KEY))
    )
    assert isinstance(provider, E2BProvider)
    assert provider.name == "e2b"


def test_get_provider_refuses_when_disabled() -> None:
    with pytest.raises(SandboxError) as exc:
        get_provider(_settings(sandbox_enabled=False))
    assert "turned off" in str(exc.value)


def test_get_provider_refuses_when_the_key_is_missing() -> None:
    # model_construct skips the validator that normally makes this configuration
    # unbootable, so the runtime guard is exercised on its own terms.
    unready = Settings.model_construct(
        sandbox_enabled=True, sandbox_provider="e2b", sandbox_api_key=None
    )
    with pytest.raises(SandboxError) as exc:
        get_provider(unready)
    assert "not configured" in str(exc.value)


# --- egress policy reaches the SDK ---------------------------------------


def test_open_policy_still_denies_metadata_and_private_ranges(stub_sdk: Any) -> None:
    _provider().create(SandboxSpec(workspace_id="w1", network="open"))
    kwargs = stub_sdk.created[0]
    assert kwargs["allow_internet_access"] is True
    # The whole point of ADR 0005's "not a policy an operator can switch off".
    for cidr in ALWAYS_DENIED_CIDRS:
        assert cidr in kwargs["network"]["deny_out"]
    assert "169.254.0.0/16" in kwargs["network"]["deny_out"]
    assert kwargs["network"]["allow_out"] == []


def test_allowlist_policy_denies_everything_then_permits_named_hosts(
    stub_sdk: Any,
) -> None:
    _provider().create(
        SandboxSpec(
            workspace_id="w1", network="allowlist", allow_hosts=("pypi.org",)
        )
    )
    network = stub_sdk.created[0]["network"]
    assert network["allow_out"] == ["pypi.org"]
    assert network["deny_out"][0] == ALL_TRAFFIC
    assert stub_sdk.created[0]["allow_internet_access"] is True


def test_none_policy_disables_internet_entirely(stub_sdk: Any) -> None:
    _provider().create(SandboxSpec(workspace_id="w1", network="none"))
    kwargs = stub_sdk.created[0]
    assert kwargs["allow_internet_access"] is False

    deny = kwargs["network"]["deny_out"]
    # The strictest policy must also be the strictest *list*. Disabling the
    # internet flag is what actually stops egress here, but a driver that
    # honoured deny_out and ignored the flag would otherwise get `none` wrong —
    # and `0.0.0.0/0` alone says nothing about IPv6, so a dual-stack host would
    # route around it the moment a hostname resolved to a AAAA record.
    assert ALL_TRAFFIC in deny
    assert ALL_TRAFFIC_V6 in deny
    for cidr in ALWAYS_DENIED_CIDRS:
        assert cidr in deny


def test_create_passes_lifecycle_metadata_env_and_timeout(stub_sdk: Any) -> None:
    handle = _provider().create(
        SandboxSpec(
            workspace_id="w1",
            template="tpl",
            timeout_seconds=300,
            env={"GRAIN_SANDBOX": "1"},
            metadata={"workspace_id": "w1"},
        )
    )
    kwargs = stub_sdk.created[0]
    # Pause-on-timeout is what makes a session's filesystem survive an idle gap.
    assert kwargs["lifecycle"] == {
        "on_timeout": {"action": "pause", "keep_memory": True}
    }
    assert kwargs["timeout"] == 300
    assert kwargs["template"] == "tpl"
    assert kwargs["envs"] == {"GRAIN_SANDBOX": "1"}
    assert kwargs["metadata"] == {"workspace_id": "w1"}
    assert handle == SandboxHandle(provider="e2b", external_id="sbx-1")


# --- Execution -> ExecResult ---------------------------------------------


def test_run_code_joins_log_chunks_and_streams_them(stub_sdk: Any) -> None:
    stub_sdk.execution = Execution(logs=Logs(stdout=["one\n", "two\n"], stderr=["warn"]))
    seen: List[tuple] = []
    result = _provider().run_code(
        HANDLE, "print('one')", on_output=lambda stream, text: seen.append((stream, text))
    )
    assert result.stdout == "one\ntwo\n"
    assert result.stderr == "warn"
    assert result.ok
    assert seen == [("stdout", "one\n"), ("stdout", "two\n")]


def test_run_code_reports_a_traceback_as_a_result_not_an_exception(
    stub_sdk: Any,
) -> None:
    stub_sdk.execution = Execution(
        error=ExecutionError(
            name="ValueError", value="bad input", traceback="Traceback...\nValueError"
        )
    )
    result = _provider().run_code(HANDLE, "raise ValueError('bad input')")
    assert result.error == "ValueError: bad input"
    assert "Traceback" in result.traceback
    assert not result.ok


def test_run_code_converts_a_png_result_into_an_artifact(stub_sdk: Any) -> None:
    stub_sdk.execution = Execution(
        results=[Result(png="aGVsbG8=", text="<Figure size 640x480>", is_main_result=True)]
    )
    result = _provider().run_code(HANDLE, "plt.show()")
    assert [a.kind for a in result.artifacts] == ["png"]
    artifact = result.artifacts[0]
    # The SDK already base64-encodes it; re-encoding would double-wrap it.
    assert artifact.data == "aGVsbG8="
    assert artifact.mime == "image/png"
    assert artifact.is_main is True


def test_run_code_keeps_text_only_when_it_is_the_sole_representation(
    stub_sdk: Any,
) -> None:
    stub_sdk.execution = Execution(results=[Result(text="42", is_main_result=True)])
    result = _provider().run_code(HANDLE, "6 * 7")
    assert [a.kind for a in result.artifacts] == ["text"]
    assert result.artifacts[0].data == "42"


def test_run_code_prefers_the_structured_chart_over_the_image(stub_sdk: Any) -> None:
    # A line chart as the SDK actually delivers one — the deserialiser is strict
    # about these keys, and a looser fixture would silently produce no chart.
    chart = {
        "type": "line",
        "title": "Sales",
        "elements": [],
        "x_label": "month",
        "y_label": "revenue",
        "x_unit": None,
        "y_unit": None,
        "x_scale": "linear",
        "y_scale": "linear",
        "x_ticks": [1, 2],
        "y_ticks": [10, 20],
        "x_tick_labels": ["1", "2"],
        "y_tick_labels": ["10", "20"],
    }
    stub_sdk.execution = Execution(
        results=[Result(png="aGVsbG8=", chart=chart, is_main_result=True)]
    )
    artifacts = _provider().run_code(HANDLE, "plot()").artifacts
    assert [a.kind for a in artifacts] == ["chart", "png"]
    assert '"title": "Sales"' in artifacts[0].chart_json


# --- commands ------------------------------------------------------------


def test_run_command_returns_the_exit_code(stub_sdk: Any) -> None:
    stub_sdk.command_result = CommandExitException(
        stdout="", stderr="No matching distribution", exit_code=1, error=None
    )
    result = _provider().run_command(HANDLE, "pip install nope")
    # A failed install is something the model must read, not an exception it
    # cannot see.
    assert result.exit_code == 1
    assert "No matching distribution" in result.stderr
    assert not result.ok


# --- failure handling ----------------------------------------------------


def test_kill_is_idempotent_when_the_sandbox_is_already_gone(stub_sdk: Any) -> None:
    stub_sdk.raise_on_kill = NotFoundException("sbx-1 not found")
    _provider().kill(HANDLE)  # must not raise: the reaper races explicit deletes
    stub_sdk.raise_on_kill = None
    _provider().kill(HANDLE)
    assert stub_sdk.killed == ["sbx-1"]


def test_sdk_failures_become_sandbox_errors_without_leaking_the_key(
    stub_sdk: Any,
) -> None:
    stub_sdk.raise_on_create = SandboxException(
        f"POST https://api.e2b.dev/sandboxes failed, api_key={API_KEY}"
    )
    with pytest.raises(SandboxError) as exc:
        _provider().create(SandboxSpec(workspace_id="w1"))
    message = str(exc.value)
    assert API_KEY not in message
    assert "e2b.dev" not in message
    assert "Could not start a sandbox" in message


def test_a_missing_sandbox_is_named_plainly(stub_sdk: Any) -> None:
    stub_sdk.raise_on_create = NotFoundException("gone")
    with pytest.raises(SandboxError) as exc:
        _provider().create(SandboxSpec(workspace_id="w1"))
    assert "no longer exists" in str(exc.value)


# --- the test double itself ----------------------------------------------


def test_fake_provider_hands_out_deterministic_ids_and_records_calls() -> None:
    fake = FakeProvider()
    first = fake.create(SandboxSpec(workspace_id="w1"))
    second = fake.create(SandboxSpec(workspace_id="w1"))
    assert (first.external_id, second.external_id) == ("fake-1", "fake-2")
    assert [name for name, _ in fake.calls] == ["create", "create"]


def test_fake_provider_echoes_a_literal_print() -> None:
    fake = FakeProvider()
    handle = fake.create(SandboxSpec(workspace_id="w1"))
    result = fake.run_code(handle, "print('hello')\nprint(\"world\")")
    assert result.stdout == "hello\nworld\n"
    assert result.ok


def test_fake_provider_does_not_evaluate_code() -> None:
    fake = FakeProvider()
    handle = fake.create(SandboxSpec(workspace_id="w1"))
    # If this ever produced "9", the double would be executing generated code on
    # the machine running the suite — the exact thing the sandbox exists to stop.
    assert fake.run_code(handle, "print(3 * 3)").stdout == ""


def test_fake_provider_serves_scripted_results_in_order() -> None:
    fake = FakeProvider()
    handle = fake.create(SandboxSpec(workspace_id="w1"))
    fake.script(
        ExecResult(stdout="first"), ExecResult(error="ValueError: nope", traceback="tb")
    )
    assert fake.run_code(handle, "anything").stdout == "first"
    second = fake.run_code(handle, "anything")
    assert second.error == "ValueError: nope"
    assert not second.ok


def test_fake_provider_refuses_a_killed_sandbox_but_resumes_a_paused_one() -> None:
    fake = FakeProvider()
    handle = fake.create(SandboxSpec(workspace_id="w1"))
    fake.pause(handle)
    assert fake.run_code(handle, "print('up')").stdout == "up\n"
    fake.kill(handle)
    fake.kill(handle)  # idempotent
    with pytest.raises(SandboxError):
        fake.run_code(handle, "print('up')")


def test_fake_provider_has_a_real_filesystem() -> None:
    fake = FakeProvider()
    handle = fake.create(SandboxSpec(workspace_id="w1"))
    fake.write_files(handle, {"/home/user/a.csv": b"x,y", "/home/user/sub/b.txt": b"hi"})
    assert fake.read_file(handle, "/home/user/a.csv") == b"x,y"
    listing = {entry.name: entry for entry in fake.list_files(handle, "/home/user")}
    assert listing["a.csv"].size == 3
    assert listing["sub"].is_dir is True
    with pytest.raises(SandboxError):
        fake.read_file(handle, "/home/user/missing")
