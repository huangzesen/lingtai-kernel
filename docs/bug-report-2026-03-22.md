# lingtai-kernel Bug Report — 2026-03-22

Audited by Claude Opus 4.6. Do NOT fix without review — report only.

---

## CRITICAL (2)

### BUG-001 — Soul whisper 与 agent 主线程竞争 ChatInterface

**File:** `base_agent.py:492-528`, `intrinsics/soul.py:82-159`

Soul timer 在独立线程运行。`whisper()` 访问 `agent._chat` 时没有锁保护。如果 whisper 执行过程中有消息到达（mail/外部），agent 主线程被唤醒，两个线程同时操作 `_chat`。`Timer.cancel()` 对已经开始执行的回调无效。

场景：
1. Agent 空闲，soul timer 启动
2. Timer 到期，`_soul_whisper()` 在独立线程开始执行
3. `whisper()` 读取 `agent._chat.interface`，开始克隆对话历史
4. 恰好此时一封 mail 到达，agent 主线程被唤醒
5. 主线程调用 `_cancel_soul_timer()` —— 但 `Timer.cancel()` 对已经在执行的回调无效
6. 主线程进入 `_chat.send()`，修改 conversation history
7. Soul 线程还在用同一个 `_chat` 对象克隆/读取

### BUG-023 — 最后一次 LLM 超时后 future 被遗弃，阻塞后续所有调用

**File:** `llm_utils.py:200-315`

所有重试耗尽后，最后一个超时的 future 没有被 cancel，底层 HTTP 线程继续运行。由于 `_timeout_pool` 只有 1 个 worker（BUG-008），这个遗弃的 future 会阻塞后续所有 LLM 调用，直到远端响应或连接断开。

---

## HIGH (16)

### BUG-002 — `_stop_heartbeat()` 不 join 线程

**File:** `base_agent.py:561-568`

heartbeat 可能在 working dir 被删后仍在写文件。

### BUG-003 — `_seen` set 无锁保护

**File:** `services/mail.py:106,213-237`

`listen()` 可被重复调用导致竞争写入。

### BUG-004 — `_mailman` 文件写入与通知竞争

**File:** `intrinsics/mail.py:312`

`_mailman` 写文件后立即 `_mail_arrived.set()`，agent 可能在文件写入完成前就尝试读取。

### BUG-005 — `_perform_aed()` 从 heartbeat 线程设 `_session.chat = None`

**File:** `base_agent.py:638-651`

与主线程的 `send()` 竞争，可导致 `AttributeError` 或 `NoneType` 错误。

### BUG-007 — 每次并行执行创建新 ThreadPoolExecutor

**File:** `tool_executor.py:235-258`

`shutdown(wait=False)` 不等待完成，卡住的工具线程会累积。

### BUG-008 — `_timeout_pool` 单 worker 无法并发重试

**File:** `session.py:84`

超时重试排队在卡住的调用后面，实际上无法并发重试。

### BUG-009 — `whisper()` 创建的临时 ChatSession 从不关闭

**File:** `soul.py:138-148`

`tracked=False` 的 session 无法被审计或清理，可能泄漏连接。

### BUG-013 — STUCK 状态错误地设置 `_idle` event

**File:** `base_agent.py:679-698`

外部代码误判 agent 为空闲。

### BUG-015 — `_perform_refresh` 不重置 `_uptime_anchor`

**File:** `base_agent.py:701-742`

刷新后 vigil 可能立即过期。

### BUG-018 — 延迟 mail 的 `_mailman` 线程醒来时 agent 可能已停止

**File:** `intrinsics/mail.py:281-347`

写入死 inbox，消息永不处理。

### BUG-019 — `_move_to_sent` 非原子读写移动

**File:** `intrinsics/mail.py:259-278`

中断可能丢失消息。

### BUG-024 — bad-request reset 快照只存 reset 前状态

**File:** `llm_utils.py:280-295`

调查困难。

### BUG-025 — soul whisper 失败后 `_soul_oneshot` 未清除

**File:** `base_agent.py:510-528`

导致失败的 inquiry 无限重试。

### BUG-026 — `_persist_soul_entry` 和 eigen 主线程并发 git 操作

**File:** `base_agent.py:530-543`

可能损坏 git index。

### BUG-028 — auto-forget 失败时返回值被忽略

**File:** `base_agent.py:816-822`, `eigen.py:183-192`

`_compaction_warnings` 仍被重置为 0，context 持续膨胀。

### BUG-034 — 文档状态数不一致

**File:** `base_agent.py` docstring vs CLAUDE.md

代码文档说 4-state lifecycle，CLAUDE.md 说 2-state。

### BUG-036 — 无 `_mail_service` 时延迟发送静默失败

**File:** `intrinsics/mail.py:397`

返回 "sent" 但消息永远不会送达，无警告。

---

## MEDIUM (9)

### BUG-006 — `_cancel_event.clear()` 可能吞掉中断信号

**File:** `base_agent.py:862`

### BUG-010 — `JSONLLoggingService` 构造失败时文件句柄泄漏

**File:** `services/logging.py:39-75`

### BUG-011 — Billboard `.tmp` 文件在 `os.replace()` 失败后不清理

**File:** `base_agent.py:183-188`

### BUG-016 — soul timer 竞争可能导致连续两次 whisper

**File:** `base_agent.py:492-528`

### BUG-017 — `_chat is None` 时 `context_forget` 静默失败

**File:** `eigen.py:127-168`

### BUG-020 — 自发消息可能被双重通知

**File:** `services/mail.py` + `intrinsics/mail.py`

### BUG-021 — bounce 通知无流量控制

**File:** `intrinsics/mail.py:336-347`

### BUG-029 — 自身地址比较用字符串而非 resolve 后的路径

**File:** `system.py:206-219`

### BUG-035 — `read_manifest()` 只返回 covenant 字段，命名误导

**File:** `workdir.py:220-234`

---

## LOW (2)

### BUG-012 — lock 文件无 `__del__` 保底

**File:** `workdir.py:53-64`

### BUG-022 — schema 声明 integer，实际接受 float

**File:** `intrinsics/mail.py:360`

---

## 核心主题

最突出的问题集中在**线程安全**：soul timer 线程、heartbeat 线程、mailman daemon 线程和 agent 主线程之间缺乏同步。BUG-001（soul/chat 竞争）和 BUG-026（并发 git 操作）是最可能在生产中出现问题的。
