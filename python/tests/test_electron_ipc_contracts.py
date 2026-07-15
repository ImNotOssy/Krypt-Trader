from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_main_optimizer_ipc_uses_long_running_rpc_timeout():
    ipc = (ROOT / "electron" / "ipc.ts").read_text(encoding="utf-8")

    assert "const OPTIMIZER_RPC_TIMEOUT_MS" in ipc
    assert "pythonBackend.request('mainOptimize', args || {}, OPTIMIZER_RPC_TIMEOUT_MS)" in ipc
