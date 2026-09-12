"""Build and run the single-VF QEMU integration scenario."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from harness.events import encode_event, format_marker, iter_events
from harness.topology import complex_pci_args


REQUIRED_MARKERS = (
    "contains BAR 0 for 8 VFs",
    "Intel(R) Gigabit Ethernet Network Connection",
    "MK_STAGE_POOL_OK",
    "MK_STAGE_IOMMU_ENABLED",
    "MK_STAGE_IOMMU_GROUP",
    "MK_STAGE_VF_CREATED",
    "MK_STAGE_PF_RETAINED",
    "MK_STAGE_VF_HOST_OWNED",
    "MK_STAGE_KERF_INIT_OK",
    "MK_STAGE_KERF_CREATE_OK",
    "MK_STAGE_STATUS_ready",
    "MK_STAGE_VF_LEASED",
    "MK_STAGE_IOMMU_DOMAIN",
    "MK_STAGE_VF_ASSIGNED",
    "MK_STAGE_KERF_LOAD_OK",
    "MK_STAGE_STATUS_loaded",
    "MK_STAGE_MKTTY_CONNECTED",
    "MK_STAGE_KERF_EXEC_OK",
    "MK_STAGE_STATUS_active",
    "MK_STAGE_PF_LINK_UP pf=0000:00:02.0",
    "MK_STAGE_PF_ADDRESS pf=0000:00:02.0",
    "MK_SECONDARY_VF_ENUMERATED instance=1 bdf=0000:00:12.0 vendor=0x8086 device=0x10ca",
    "MK_SECONDARY_VF_BAR index=0",
    "MK_SECONDARY_PF_ABSENT instance=1 bdf=0000:00:02.0",
    "MK_SECONDARY_PCI_ISOLATED instance=1 devices=1 vf=0000:00:12.0",
    "MK_SECONDARY_VF_READY instance=1 bdf=0000:00:12.0 driver=igbvf netdev=",
    "Multikernel PCI control plane:",
    "Allocated 3 host-owned MSI-X vectors for 0000:00:12.0",
    "for 0000:00:12.0 vector 0",
    "for 0000:00:12.0 vector 1",
    "MK_SECONDARY_VF_TRAFFIC_BEFORE instance=1 netdev=",
    "MK_SECONDARY_VF_DATAPATH instance=1 netdev=",
    "MK_SECONDARY_PRIMARY_REACHABLE instance=1 netdev=",
    "MK_SECONDARY_VF_REBIND_PASS instance=1 bdf=0000:00:12.0",
    "MK_SECONDARY_VF_FLR_PASS instance=1 bdf=0000:00:12.0",
    "MK_SECONDARY_PCI_CONFIG_STRESS_READY instance=1 reads=64",
    "MK_SECONDARY_PCI_CONFIG_STRESS_PROGRESS instance=1 reads=",
    "MK_SECONDARY_ALIVE",
    "MK_CONCURRENT_CPU_PCI_RPC_PASS cycles=3",
    "MK_PRIMARY_STILL_ALIVE",
    "MK_STAGE_PF_ACTIVE pf=0000:00:02.0 driver=igb",
    "MK_STAGE_KERF_KILL_OK",
    "MK_HALTED_IRQ_QUIESCE_PASS instance=1 vectors=3",
    "MK_STAGE_KERF_UNLOAD_OK",
    "MK_STAGE_KERF_DELETE_OK",
    "MK_STAGE_VF_RESTORED",
    "MK_HOSTILE_CREATE_REJECTED stage=pf-assignment",
    "MK_HOSTILE_CREATE_REJECTED stage=duplicate-vf",
    "MK_HOSTILE_CREATE_REJECTED stage=second-owner",
    "MK_HOSTILE_VF_DISABLE_REJECTED phase=ready",
    "MK_HOSTILE_VF_REBIND_REJECTED phase=ready",
    "MK_HOSTILE_VF_REPROBE_CONTAINED phase=ready",
    "MK_HOSTILE_PF_BIND_REJECTED phase=ready",
    "MK_HOSTILE_VF_DISABLE_REJECTED phase=active",
    "MK_HOSTILE_VF_REBIND_REJECTED phase=active",
    "MK_HOSTILE_VF_REPROBE_CONTAINED phase=active",
    "MK_HOSTILE_PF_BIND_REJECTED phase=active",
    "MK_HOSTILE_LEASE_PERSISTED phase=after-kill",
    "MK_RESTART_VF_DATAPATH_PASS instance=1",
    "MK_HOSTILE_LEASE_PERSISTED phase=after-unload",
    "MK_STAGE_VF_RESTORED phase=cycle-1",
    "MK_REPEAT_CYCLE_PASS cycle=2",
    "MK_REPEAT_CYCLE_PASS cycle=3",
    "MK_REPEAT_CYCLE_PASS cycle=4",
    "MK_HOSTILE_SURPRISE_UNBIND_FAIL_CLOSED id=104",
    "MK_HOSTILE_SURPRISE_UNBIND_RECOVERED id=104",
    "MK_COMPLEX_TOPOLOGY_READY pfs=3 vfs=8 assigned=0 noise=2",
    "MK_COMPLEX_PF_REJECTED family=igb2",
    "MK_COMPLEX_SECOND_OWNER_REJECTED family=igb0",
    "MK_COMPLEX_INSTANCE_ACTIVE family=igb0 id=1",
    "MK_COMPLEX_HOSTILE_CONTAINED family=igb0",
    "MK_COMPLEX_HOSTILE_CONTAINED family=igb1",
    "MK_COMPLEX_HOSTILE_CONTAINED family=igb2",
    "MK_COMPLEX_CONCURRENT_LEASES_PASS leases=3 active_instances=1 families=igb0,igb1,igb2",
    "MK_COMPLEX_UNASSIGNED_VFS_INTACT count=5 families=3 owner=host",
    "MK_COMPLEX_RESTORED families=3 vfs=8 ownership=host",
    "MK_STAGE_VF_TEARDOWN pf=0000:00:02.0 vfs=0",
    "MK_DEMO_PASS simultaneous_kernels=verified",
)
FAILURE_MARKERS = ("MK_DEMO_FAIL", "MK_SECONDARY_FAIL")
REQUIRED_EVENT_NAMES = (
    "MK_STAGE_IOMMU_DOMAIN",
    "MK_SECONDARY_PCI_CONFIG_STRESS_READY",
    "MK_SECONDARY_PCI_CONFIG_STRESS_PROGRESS",
    "MK_SECONDARY_VF_REBIND_PASS",
    "MK_SECONDARY_VF_FLR_PASS",
    "MK_SECONDARY_ALIVE",
    "MK_CONCURRENT_CPU_PCI_RPC_PASS",
    "MK_HALTED_IRQ_QUIESCE_PASS",
    "MK_RESTART_VF_DATAPATH_PASS",
    "MK_COMPLEX_CONCURRENT_LEASES_PASS",
    "MK_COMPLEX_RESTORED",
    "MK_DEMO_PASS",
)
MIN_CPUSET_GROWTH_CPUS = 9
DEFAULT_QEMU_TIMEOUT = 1200
DEFAULT_QEMU_IDLE_TIMEOUT = 120
REQUIRED_TOPOLOGY_MARKERS = (
    "setup_percpu: NR_CPUS:12",
    "MK_STAGE_KERF_INIT_OK cpus=2,3,4,5,6,7,8,9,10,11 memory=1024M",
)



class HarnessError(RuntimeError):
    """A deterministic harness configuration or verification failure."""


class ProgressWatchdog:
    """Track structured harness progress while ignoring console chatter."""

    def __init__(self, idle_seconds: float, clock=time.monotonic) -> None:
        self.idle_seconds = idle_seconds
        self._clock = clock
        self._last_progress = clock()
        self._buffer = ""
        self.progress_events = 0

    def feed(self, chunk: bytes | str) -> None:
        self._buffer += chunk.decode(errors="replace") if isinstance(chunk, bytes) else chunk
        lines = self._buffer.splitlines(keepends=True)
        self._buffer = lines.pop() if lines and not lines[-1].endswith(("\n", "\r")) else ""
        for line in lines:
            if "MK_EVENT " in line:
                self._last_progress = self._clock()
                self.progress_events += 1

    def stalled(self) -> bool:
        return self._clock() - self._last_progress >= self.idle_seconds


def _numeric_setting(environ: Mapping[str, str], name: str, default: int) -> int:
    value = environ.get(name, str(default))
    if not value.isdigit():
        raise HarnessError("QEMU tunables must be numeric")
    return int(value)


@dataclass(frozen=True)
class HarnessConfig:
    root: Path
    build_dir: Path
    qemu: str
    cpus: int
    memory_mb: int
    timeout_seconds: int
    idle_timeout_seconds: int

    @classmethod
    def from_environment(
        cls, root: Path, environ: Mapping[str, str] = os.environ
    ) -> "HarnessConfig":
        build_dir = Path(environ.get("BUILD_DIR", root / "build"))
        config = cls(
            root=root,
            build_dir=build_dir,
            qemu=environ.get("QEMU", "qemu-system-x86_64"),
            cpus=_numeric_setting(environ, "QEMU_CPUS", 12),
            memory_mb=_numeric_setting(environ, "QEMU_MEMORY_MB", 8192),
            timeout_seconds=_numeric_setting(environ, "QEMU_TIMEOUT", DEFAULT_QEMU_TIMEOUT),
            idle_timeout_seconds=_numeric_setting(
                environ, "QEMU_IDLE_TIMEOUT", DEFAULT_QEMU_IDLE_TIMEOUT
            ),
        )
        config.validate()
        return config

    @property
    def kernel(self) -> Path:
        return self.build_dir / "kernel/arch/x86/boot/bzImage"

    @property
    def initrd(self) -> Path:
        return self.build_dir / "host-initrd.cpio.gz"

    @property
    def log(self) -> Path:
        return self.build_dir / "qemu-serial.log"

    @property
    def event_log(self) -> Path:
        return self.build_dir / "qemu-events.jsonl"

    @property
    def qmp_socket(self) -> Path:
        return self.build_dir / "qemu-qmp.sock"

    def validate(self) -> None:
        if self.cpus != 12:
            raise HarnessError("QEMU_CPUS must be exactly 12")
        if self.memory_mb != 8192:
            raise HarnessError("QEMU_MEMORY_MB must be exactly 8192")
        if self.timeout_seconds < 30:
            raise HarnessError("QEMU_TIMEOUT must be at least 30 seconds")
        if self.idle_timeout_seconds < 1:
            raise HarnessError("QEMU_IDLE_TIMEOUT must be at least 1 second")

    def qemu_args(self) -> list[str]:
        return [
            "-machine",
            "q35,accel=tcg",
            "-cpu",
            "max",
            "-smp",
            str(self.cpus),
            "-m",
            str(self.memory_mb),
            "-device",
            "intel-iommu,intremap=on",
            "-kernel",
            str(self.kernel),
            "-initrd",
            str(self.initrd),
            "-append",
            "console=ttyS0,115200 rdinit=/init panic=-1 intel_iommu=on iommu.strict=1",
            "-nographic",
            "-monitor",
            "none",
            "-qmp",
            f"unix:{self.qmp_socket},server=on,wait=off",
            "-no-reboot",
            *complex_pci_args(),
        ]


def _event_field_int(event: dict[str, object], name: str) -> int | None:
    fields = event.get("fields")
    if not isinstance(fields, dict):
        return None
    try:
        return int(fields[name])
    except (KeyError, TypeError, ValueError):
        return None


def _validate_topology_growth_evidence(
    log_text: str, events: Sequence[dict[str, object]]
) -> None:
    for marker in REQUIRED_TOPOLOGY_MARKERS:
        if marker not in log_text:
            raise HarnessError(f"missing-topology-evidence marker={marker!r}")

    max_cpus_values = [
        max_cpus
        for event in events
        if event.get("event") == "MK_CONCURRENT_CPU_PCI_RPC_PASS"
        for max_cpus in [_event_field_int(event, "max_cpus")]
        if max_cpus is not None
    ]
    if not max_cpus_values or max(max_cpus_values) < MIN_CPUSET_GROWTH_CPUS:
        raise HarnessError("insufficient-cpuset-growth max_cpus<9")


def _validate_counter_evidence(events: Sequence[dict[str, object]]) -> None:
    for event in events:
        if event.get("event") != "MK_SECONDARY_VF_DATAPATH":
            continue
        fields = event.get("fields")
        if not isinstance(fields, dict):
            raise HarnessError("malformed-counter-evidence")
        try:
            values = {
                name: int(fields[name])
                for name in ("tx_before", "tx_after", "rx_before", "rx_after")
            }
        except (KeyError, TypeError, ValueError) as error:
            raise HarnessError("malformed-counter-evidence") from error
        if any(value < 0 for value in values.values()):
            raise HarnessError("forbidden-counter-value")
        if values["tx_after"] < values["tx_before"] or values["rx_after"] < values["rx_before"]:
            raise HarnessError("forbidden-counter-value")


def validate_log(log_text: str) -> None:
    for marker in FAILURE_MARKERS:
        if marker in log_text:
            raise HarnessError("guest-failure-marker")
    for marker in REQUIRED_MARKERS:
        if marker not in log_text:
            raise HarnessError(f"missing-marker marker={marker!r}")
    try:
        events = list(iter_events(log_text))
    except (ValueError, json.JSONDecodeError) as error:
        raise HarnessError("malformed-event") from error
    event_names = {event["event"] for event in events}
    for event_name in REQUIRED_EVENT_NAMES:
        if event_name not in event_names:
            raise HarnessError(f"missing-event event={event_name!r}")
    _validate_topology_growth_evidence(log_text, events)
    _validate_counter_evidence(events)


class QmpClient:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, path: Path, timeout: float = 10) -> "QmpClient":
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(path)
                break
            except OSError as error:
                if loop.time() >= deadline:
                    raise HarnessError("qmp-connect") from error
                await asyncio.sleep(0.05)
        client = cls(reader, writer)
        greeting = await client._read_message()
        if "QMP" not in greeting:
            await client.close()
            raise HarnessError("qmp-greeting")
        await client.execute("qmp_capabilities")
        return client

    async def _read_message(self) -> dict[str, object]:
        line = await self.reader.readline()
        if not line:
            raise HarnessError("qmp-eof")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise HarnessError("qmp-json") from error
        if not isinstance(message, dict):
            raise HarnessError("qmp-message")
        return message

    async def execute(self, command: str) -> object:
        request = json.dumps({"execute": command}, separators=(",", ":"))
        self.writer.write(f"{request}\r\n".encode())
        await self.writer.drain()
        while True:
            response = await self._read_message()
            if "error" in response:
                raise HarnessError(f"qmp-command command={command}")
            if "return" in response:
                return response["return"]

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except OSError:
            pass


def _host_event(
    event: str, fields: dict[str, object], recorded: list[dict[str, object]]
) -> None:
    payload = {"event": event, "fields": fields, "source": "host"}
    recorded.append(payload)
    print(format_marker(event, fields), flush=True)
    print(encode_event(event, fields, "host"), flush=True)


async def _stream_serial(
    reader: asyncio.StreamReader, log: Path, watchdog: ProgressWatchdog | None = None
) -> None:
    with log.open("wb") as log_file:
        while chunk := await reader.read(64 * 1024):
            if watchdog is not None:
                watchdog.feed(chunk)
            log_file.write(chunk)
            log_file.flush()
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()


async def _wait_for_qemu(
    process: asyncio.subprocess.Process,
    watchdog: ProgressWatchdog,
    timeout_seconds: int,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while process.returncode is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HarnessError("timeout")
        try:
            await asyncio.wait_for(process.wait(), timeout=min(1.0, remaining))
        except asyncio.TimeoutError:
            if watchdog.stalled():
                raise HarnessError("idle-timeout")


async def _stop_qemu(
    process: asyncio.subprocess.Process, qmp: QmpClient | None
) -> None:
    if qmp is not None:
        try:
            await asyncio.wait_for(qmp.execute("quit"), timeout=2)
        except (HarnessError, asyncio.TimeoutError):
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
        return
    except asyncio.TimeoutError:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


def _write_event_log(
    path: Path, host_events: list[dict[str, object]], serial_text: str
) -> int:
    events = [*host_events, *iter_events(serial_text)]
    lines = [
        json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
        for event in events
    ]
    path.write_text("\n".join(lines) + "\n")
    return len(events)


async def _run_test_async(config: HarnessConfig) -> int:
    config.build_dir.mkdir(parents=True, exist_ok=True)
    config.qmp_socket.unlink(missing_ok=True)
    host_events: list[dict[str, object]] = []
    _host_event(
        "MK_QEMU_START",
        {"timeout": f"{config.timeout_seconds}s", "log": config.log},
        host_events,
    )
    process = await asyncio.create_subprocess_exec(
        config.qemu,
        *config.qemu_args(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None
    watchdog = ProgressWatchdog(config.idle_timeout_seconds)
    serial_task = asyncio.create_task(_stream_serial(process.stdout, config.log, watchdog))
    qmp: QmpClient | None = None
    try:
        qmp = await QmpClient.connect(config.qmp_socket)
        status = await qmp.execute("query-status")
        status_name = status.get("status", "unknown") if isinstance(status, dict) else "unknown"
        _host_event("MK_QMP_CONNECTED", {"status": status_name}, host_events)
        try:
            await _wait_for_qemu(process, watchdog, config.timeout_seconds)
        except HarnessError as error:
            await _stop_qemu(process, qmp)
            raise error
        await serial_task
    finally:
        if process.returncode is None:
            await _stop_qemu(process, qmp)
        if not serial_task.done():
            serial_task.cancel()
            await asyncio.gather(serial_task, return_exceptions=True)
        if qmp is not None:
            await qmp.close()
        config.qmp_socket.unlink(missing_ok=True)
    if process.returncode != 0:
        raise HarnessError(f"exit-status status={process.returncode}")
    serial_text = config.log.read_text(errors="replace")
    validate_log(serial_text)
    _host_event(
        "MK_QEMU_TEST_PASS",
        {"markers": len(REQUIRED_MARKERS), "log": config.log},
        host_events,
    )
    event_count = _write_event_log(config.event_log, host_events, serial_text)
    print(f"MK_QEMU_EVENTS_OK events={event_count} log={config.event_log}")
    return 0


def _run_test(config: HarnessConfig) -> int:
    return asyncio.run(_run_test_async(config))


def run(mode: str, config: HarnessConfig) -> int:
    if mode == "run":
        config.build_dir.mkdir(parents=True, exist_ok=True)
        config.qmp_socket.unlink(missing_ok=True)
        os.execvpe(config.qemu, [config.qemu, *config.qemu_args()], os.environ)
        raise AssertionError("execvpe returned")
    return _run_test(config)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "test"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        return run(args.mode, HarnessConfig.from_environment(root))
    except HarnessError as error:
        build_dir = Path(os.environ.get("BUILD_DIR", root / "build"))
        log = build_dir / "qemu-serial.log"
        print(f"MK_QEMU_FAIL reason={error} log={log}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
