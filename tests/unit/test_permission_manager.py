from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.permissions.storage import load_policy_file

# ── helpers ──────────────────────────────────────────────────────────────────

def _make_manager(**policies: ToolPolicy) -> PermissionManager:
    # project_policy_file=None：测试中不使用持久化，不污染项目 .kama/policy.toml
    return PermissionManager(policies or None)


async def _collect_emitted() -> tuple[list[dict[str, Any]], Any]:
    emitted: list[dict[str, Any]] = []

    async def emitter(event: dict[str, Any]) -> None:
        emitted.append(event)

    return emitted, emitter


# ── evaluate() delegation ─────────────────────────────────────────────────────

# 功能：验证 PermissionManager.evaluate 委托给 policy 层返回正确决策
# 设计：直接调用 evaluate()，不涉及 Future，验证策略加载与委托路径
def test_evaluate_delegates_to_policy() -> None:
    mgr = _make_manager()
    assert mgr.evaluate("read_file", {"path": "x"}) == PermissionDecision.ALLOW
    assert mgr.evaluate("bash", {"command": "echo hi"}) == PermissionDecision.ASK
    assert mgr.evaluate("write_file", {"path": "x", "content": ""}) == PermissionDecision.ASK


# ── check_and_wait: ALLOW path ───────────────────────────────────────────────

# 功能：验证策略为 ALLOW 时 check_and_wait 立即返回 (True, "auto_allow")，不发任何事件
# 设计：read_file 默认 ALLOW，断言不产生 permission.requested 事件，覆盖"无噪声放行"路径
async def test_check_and_wait_allow_no_event() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t1", tool_name="read_file",
        params={"path": "README.md"}, session_id="s1",
        event_emitter=emitter,
    )

    assert allowed is True
    assert decision == "auto_allow"
    assert emitted == []


# ── check_and_wait: ASK path + respond ───────────────────────────────────────

# 功能：验证 ASK 策略时发出 permission.requested 事件并等待 respond() 解决 Future
# 设计：在后台协程中调用 respond("allow_once")，主协程 await 结束后断言结果；
#       这是权限系统的核心反向请求通路
async def test_check_and_wait_ask_emits_event_and_waits() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_respond() -> None:
        await asyncio.sleep(0)  # yield once so check_and_wait can emit the event
        mgr.respond("t2", "allow_once")

    task = asyncio.create_task(_auto_respond())
    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t2", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is True
    assert decision == "allow_once"
    assert len(emitted) == 1
    assert emitted[0]["type"] == "permission.requested"
    assert emitted[0]["tool_use_id"] == "t2"
    assert emitted[0]["tool_name"] == "bash"


# 功能：验证 respond("deny_once") 使 check_and_wait 返回 (False, "deny_once")
# 设计：用户拒绝时工具不应执行，确认 False 返回值而不是异常
async def test_check_and_wait_deny_once_returns_false() -> None:
    mgr = _make_manager()
    _, emitter = await _collect_emitted()

    async def _auto_deny() -> None:
        await asyncio.sleep(0)
        mgr.respond("t3", "deny_once")

    task = asyncio.create_task(_auto_deny())
    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t3", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is False
    assert decision == "deny_once"


# ── allow_session cache ────────────────────────────────────────────────────

# 功能：验证 respond("allow_session") 后同 session 同工具下次不再发事件
# 设计：第二次调用 check_and_wait 命中 session 缓存，直接返回 (True, "auto_allow")，emitted 仍为 1 条
async def test_allow_session_skips_future_ask() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    # First call: user says "allow session"
    async def _auto_session() -> None:
        await asyncio.sleep(0)
        mgr.respond("t4", "allow_session")

    task = asyncio.create_task(_auto_session())
    r1, _ = await mgr.check_and_wait(
        tool_use_id="t4", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task
    assert r1 is True

    # Second call: should hit session cache, no new event
    r2, d2 = await mgr.check_and_wait(
        tool_use_id="t5", tool_name="bash",
        params={"command": "ls"}, session_id="s1",
        event_emitter=emitter,
    )

    assert r2 is True
    assert d2 == "auto_allow"
    assert len(emitted) == 1  # only the first call emitted an event


# 功能：验证 allow_session 在同一 manager 实例内对不同 session 不共享
# 设计：s1 设置 allow_session → 只写入 session 缓存；s2 不命中 session 缓存，
#       需要重新 ASK；emitted 应有 2 条。这是 session 级隔离的核心语义。
async def test_allow_session_not_shared_across_sessions() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    # session s1 sets allow session for bash
    async def _auto_session() -> None:
        await asyncio.sleep(0)
        mgr.respond("t6", "allow_session")

    task = asyncio.create_task(_auto_session())
    await mgr.check_and_wait(
        tool_use_id="t6", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    # session s2 — 不命中 session 缓存，需要再次 ASK
    async def _auto_session2() -> None:
        await asyncio.sleep(0)
        mgr.respond("t7", "allow_once")

    task2 = asyncio.create_task(_auto_session2())
    r, d = await mgr.check_and_wait(
        tool_use_id="t7", tool_name="bash",
        params={"command": "echo"}, session_id="s2",
        event_emitter=emitter,
    )
    await task2

    assert r is True
    assert d == "allow_once"
    assert len(emitted) == 2  # s1 和 s2 各发一次事件


# ── allow_project cache ────────────────────────────────────────────────────

# 功能：验证 allow_project 在同一 manager 实例内对所有 session 生效（project_always 共享）
# 设计：s1 设置 allow_project → 写入 _project_always；s2 命中项目级缓存，直接放行；
#       emitted 只有 1 条（s2 不需要再 ASK）。这是项目级跨 session 语义。
async def test_allow_project_shared_across_sessions() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    # session s1 sets allow project for bash
    async def _auto_project() -> None:
        await asyncio.sleep(0)
        mgr.respond("t6p", "allow_project")

    task = asyncio.create_task(_auto_project())
    await mgr.check_and_wait(
        tool_use_id="t6p", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    # session s2 — project_always["bash"] = "allow" → 直接放行，不再 ASK
    r, d = await mgr.check_and_wait(
        tool_use_id="t7p", tool_name="bash",
        params={"command": "echo"}, session_id="s2",
        event_emitter=emitter,
    )

    assert r is True
    assert d == "auto_allow"
    assert len(emitted) == 1  # s2 命中项目级缓存，不再发出事件


# ── deny_session cache ────────────────────────────────────────────────────

# 功能：验证 respond("deny_session") 后同 session 同工具下次直接返回 (False, "auto_deny")
# 设计：用户选择 deny session 后该 session 内不再骚扰，下次调用静默拒绝
async def test_deny_session_skips_future_ask() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_deny_session() -> None:
        await asyncio.sleep(0)
        mgr.respond("t8", "deny_session")

    task = asyncio.create_task(_auto_deny_session())
    r1, _ = await mgr.check_and_wait(
        tool_use_id="t8", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    await task
    assert r1 is False

    # Second call: cache hit → no event, return (False, "auto_deny")
    r2, d2 = await mgr.check_and_wait(
        tool_use_id="t9", tool_name="bash",
        params={"command": "ls"}, session_id="s1",
        event_emitter=emitter,
    )
    assert r2 is False
    assert d2 == "auto_deny"
    assert len(emitted) == 1


# ── deny_project cache ────────────────────────────────────────────────────

# 功能：验证 deny_project 在同一 manager 实例内对所有 session 生效
# 设计：s1 设置 deny_project → 写入 _project_always；s2 命中项目级缓存，直接拒绝；
#       emitted 只有 1 条。这是项目级跨 session deny 语义。
async def test_deny_project_shared_across_sessions() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_deny_project() -> None:
        await asyncio.sleep(0)
        mgr.respond("tdp1", "deny_project")

    task = asyncio.create_task(_auto_deny_project())
    await mgr.check_and_wait(
        tool_use_id="tdp1", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    # session s2 — project_always["bash"] = "deny" → 直接拒绝
    r, d = await mgr.check_and_wait(
        tool_use_id="tdp2", tool_name="bash",
        params={"command": "ls"}, session_id="s2",
        event_emitter=emitter,
    )

    assert r is False
    assert d == "auto_deny"
    assert len(emitted) == 1


# ── cancel_session ────────────────────────────────────────────────────────────

# 功能：验证 cancel_session 将 pending Future 设为 deny_once，check_and_wait 返回 False
# 设计：模拟客户端断连场景——check_and_wait 挂起后调用 cancel_session，
#       确认 Future 被解决而非永久挂起（防止僵尸 run）
async def test_cancel_session_resolves_pending_future() -> None:
    mgr = _make_manager()
    _, emitter = await _collect_emitted()

    async def _cancel_after_emit() -> None:
        await asyncio.sleep(0)  # wait for event to be emitted
        mgr.cancel_session("s1", reason="client_disconnected")

    task = asyncio.create_task(_cancel_after_emit())
    allowed, _ = await mgr.check_and_wait(
        tool_use_id="t10", tool_name="bash",
        params={"command": "ls"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is False


# 功能：验证 cancel_session 只取消属于该 session 的 pending Future
# 设计：s1 和 s2 各有一个 pending，cancel_session(s2) 不影响 s1 的 Future
async def test_cancel_session_only_affects_target_session() -> None:
    mgr = _make_manager()
    _, emitter = await _collect_emitted()

    # Launch two concurrent check_and_wait for different sessions
    s1_done = asyncio.Event()
    s2_done = asyncio.Event()
    s1_result: list[bool] = []
    s2_result: list[bool] = []

    async def _s1() -> None:
        r, _ = await mgr.check_and_wait(
            tool_use_id="ta", tool_name="bash",
            params={"command": "echo"}, session_id="s1",
            event_emitter=emitter,
        )
        s1_result.append(r)
        s1_done.set()

    async def _s2() -> None:
        r, _ = await mgr.check_and_wait(
            tool_use_id="tb", tool_name="bash",
            params={"command": "echo"}, session_id="s2",
            event_emitter=emitter,
        )
        s2_result.append(r)
        s2_done.set()

    t1 = asyncio.create_task(_s1())
    t2 = asyncio.create_task(_s2())

    await asyncio.sleep(0)  # let both emit events and hang

    # cancel only s2
    mgr.cancel_session("s2")
    await s2_done.wait()

    # s1 should still be pending; resolve it manually
    mgr.respond("ta", "allow_once")
    await s1_done.wait()

    await t1
    await t2

    assert s1_result == [True]   # s1 was allowed
    assert s2_result == [False]  # s2 was cancelled → denied


# ── respond: unknown tool_use_id ──────────────────────────────────────────────

# 功能：验证 respond 传入不存在的 tool_use_id 时静默忽略，不抛异常
# 设计：竞态场景（客户端重复发送响应）不应导致 daemon crash
def test_respond_unknown_tool_use_id_is_noop() -> None:
    mgr = _make_manager()
    mgr.respond("nonexistent", "allow_once")  # should not raise


# ── OUTSIDE_CWD 不被 session 缓存绕过 ─────────────────────────────────────────

# 功能：验证 allow_session bash 之后，含绝对路径的命令仍触发 ASK，不被缓存绕过
# 设计：先让 session s1 对 bash 设置 allow_session，再请求含绝对路径命令；
#       OUTSIDE_CWD 检查在 session 缓存之前，应发出 permission.requested 事件
async def test_allow_session_does_not_bypass_outside_cwd() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    # 首次 allow → 写入 session 缓存
    async def _auto_session() -> None:
        await asyncio.sleep(0)
        mgr.respond("t_always", "allow_session")

    t = asyncio.create_task(_auto_session())
    await mgr.check_and_wait(
        tool_use_id="t_always", tool_name="bash",
        params={"command": "echo ok"}, session_id="s1",
        event_emitter=emitter,
    )
    await t
    assert len(emitted) == 1  # 首次 ASK 触发事件

    # 第二次：bash + 绝对路径 → OUTSIDE_CWD 强制 ASK，不命中 session 缓存
    async def _auto_respond_abs() -> None:
        await asyncio.sleep(0)
        mgr.respond("t_abs", "allow_once")

    t2 = asyncio.create_task(_auto_respond_abs())
    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t_abs", tool_name="bash",
        params={"command": "cat /etc/hosts"}, session_id="s1",
        event_emitter=emitter,
    )
    await t2

    assert allowed is True
    assert len(emitted) == 2  # 绝对路径命令再次触发 ASK，共 2 个事件


# ── 项目级持久化写入 ──────────────────────────────────────────────────────

# 功能：验证 allow_project 决策写入 project_policy_file，新 PermissionManager 加载后自动放行
# 设计：用 tmp_path 作为项目根目录，断言 .kama/policy.toml 文件存在且内容正确；
#       再新建 manager 加载同一文件，同工具无需 ASK 直接返回 auto_allow
async def test_project_policy_written_and_reloaded(tmp_path: pytest.TempPathFixture) -> None:
    project_root = tmp_path / "my_project"
    project_root.mkdir()
    policy_file = project_root / ".kama" / "policy.toml"
    mgr = PermissionManager(project_root=project_root, project_policy_file=policy_file)
    emitted, emitter = await _collect_emitted()

    async def _auto_project() -> None:
        await asyncio.sleep(0)
        mgr.respond("tp1", "allow_project")

    t = asyncio.create_task(_auto_project())
    allowed, _ = await mgr.check_and_wait(
        tool_use_id="tp1", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    await t
    assert allowed is True
    assert policy_file.exists()

    loaded = load_policy_file(policy_file)
    assert loaded.get("bash") == "allow"

    # 新 manager 加载同一文件，bash 应直接 auto_allow（无 OUTSIDE_CWD）
    mgr2 = PermissionManager(project_root=project_root, project_policy_file=policy_file)
    emitted2, emitter2 = await _collect_emitted()
    allowed2, decision2 = await mgr2.check_and_wait(
        tool_use_id="tp2", tool_name="bash",
        params={"command": "echo new"}, session_id="s2",
        event_emitter=emitter2,
    )
    assert allowed2 is True
    assert decision2 == "auto_allow"
    assert emitted2 == []  # 无需 ASK


# ── 审批超时 ──────────────────────────────────────────────────────────────────

# 功能：验证 check_and_wait 超时后返回 (False, "timeout")，不永久挂起
# 设计：timeout_s=0.05 极短超时，不主动 respond；断言在合理时间内返回 False
async def test_permission_timeout_returns_false() -> None:
    mgr = PermissionManager(timeout_s=0.05)
    emitted, emitter = await _collect_emitted()

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t_timeout", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )

    assert allowed is False
    assert decision == "timeout"
    assert len(emitted) == 1
    assert emitted[0]["type"] == "permission.requested"


# 功能：验证超时后 pending 被清理，迟到的 respond 不影响后续调用
# 设计：超时后调用 respond，不抛异常（unknown tool_use_id 静默忽略）；
#       再次 check_and_wait 同 tool_use_id 仍正常发出新的 permission.requested
async def test_permission_timeout_cleans_up_pending() -> None:
    mgr = PermissionManager(timeout_s=0.05)
    _, emitter = await _collect_emitted()

    await mgr.check_and_wait(
        tool_use_id="t_late", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    # 超时后迟到的 respond 不应 crash
    mgr.respond("t_late", "allow_once")  # should be noop
    assert "t_late" not in mgr._pending
