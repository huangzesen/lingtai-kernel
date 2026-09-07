---
related_files:
- src/lingtai/tools/mcp/skills/mcp-manual/SKILL.md
- src/lingtai/tools/mcp/skills/mcp-manual/reference/troubleshooting.md
maintenance: |
  Kernel-curated MCP addon setup contract routed to from mcp/skills/mcp-manual/SKILL.md and cross-linked from troubleshooting.md; update it whenever a curated addon (imap/telegram/feishu/wechat/whatsapp/cloud_mail) changes its config fields or install story.
---

# Curated addons — imap / telegram / feishu / wechat / whatsapp / cloud_mail

LingTai's first-party email and chat integrations. They now ship inside the `lingtai` distribution under `lingtai.mcp_servers.{imap,telegram,feishu,wechat,whatsapp,cloud_mail}` so a single kernel release carries the curated MCP surface atomically. Historical `lingtai_*` names may still appear in old configs or compatibility source, but new configurations must use the bundled `lingtai.mcp_servers.*` modules; do not assume a historical wrapper is importable in the active runtime. Historical standalone package names remain useful as provenance/homepage names, but the normal runtime path no longer depends on separate addon wheels.

## The four-step setup

1. **Read the curated setup docs before editing config.** The table below gives the registry/module/env/config-file names. If exact provider-specific fields are needed, inspect the shipped module resources or the catalog `homepage` for that addon. Field names like `email_password` (imap), `bot_token` (telegram), `app_id`/`app_secret` (feishu), and gewechat host (wechat) are addon-specific; do not guess them from memory. Make exact configuration changes only within explicit human authorization; this contract is the setup procedure, not a TUI setup screen.

2. **Add the addon to `init.json`.** Append the registry name to the top-level `addons:` list, then add an `mcp.<name>` activation entry. For a curated addon this entry is **activation + account configuration only** — the canonical shape carries no `command`/`args`/`type` at all, just the `env` your addon's config file path lives under. The running kernel's own catalog derives the entire launcher (`type`, interpreter, module `args`, and the `lingtai` import source) at load time — see "MCP child processes run in their own runtime" below — so there is nothing launcher-shaped to keep in sync across a venv move:

   ```json
   {
     "addons": ["imap"],
     "mcp": {
       "imap": {
         "env": {
           "LINGTAI_IMAP_CONFIG": ".secrets/imap.json"
         }
       }
     }
   }
   ```

   An older config that still carries `type`/`command`/`args` (e.g. copied from a pre-existing setup, or from the worked example above in an older version of this doc) keeps working: those fields are read-compatible legacy inputs, silently accepted by the JSON schema, but ignored for the actual launch with one bounded warning naming them. LingTai never rewrites or migrates them out of your `init.json` — trim them yourself if you want the file to read as canonical, or leave them; either way the running launcher is unaffected.

3. **Create the config file** at the path referenced by the env var (e.g. `.secrets/imap.json`). Use the schema from the addon docs — copy it verbatim, don't paraphrase.

4. **Run `system(action="refresh")`.** The `mcp` capability decompresses the catalog record into `mcp_registry.jsonl`, the loader spawns the subprocess, and the omnibus tool (`imap`, `telegram`, etc.) appears in your tool surface.

## MCP child processes run in their own runtime

Each current curated addon shown in this file is launched as a separate **stdio Python subprocess** with its own environment. For a **non-curated** route — a third-party `init.json` entry, or the legacy `mcp/servers.json` direct mount — `type`/`command`/`args`/`env` all come from that entry's own **active activation spec**, exactly as before. A **curated** addon activated via `init.json`'s `mcp.<name>` entry (registry record's `source` exactly `lingtai-curated`) is different in kind, not just interpreter-pinned: the running kernel's own packaged `mcp_catalog.json` owns the *entire* launcher — `type`, `command` (the running Agent's own `sys.executable`), and module `args` — and the child's `PYTHONPATH` is set outright to this Agent's own currently-imported `lingtai` source root. `init.json`'s `mcp.<name>` entry for a curated addon is activation + account config only: its presence activates the addon, and its non-launch `env` (e.g. `LINGTAI_IMAP_CONFIG`) passes through untouched. Any `command`/`args`/`type`/`env.PYTHONPATH` the entry also carries are read-compatible legacy inputs — accepted by the schema, ignored for the launch with one bounded warning, never read from and never rewritten into `init.json` or `mcp_registry.jsonl`. This is not merely interpreter pinning: the MCP stdio transport's default env allowlist excludes `PYTHONPATH`, so even the right interpreter could still resolve `lingtai` from that interpreter's own site-packages instead of this Agent's actual source; owning `PYTHONPATH` outright is what makes the child's package source actually match the main agent's. If the catalog cannot supply a safe stdio launcher for a name registered as curated, the entry is skipped with a warning rather than falling back to any legacy field. Daemon-launched MCPs (task `mcp` registrations, plugin `mcp.json` servers) are a separate route entirely: they spawn from that task/plugin's own config, never from `init.json`'s `mcp.<name>` entries or `mcp_registry.jsonl`, so none of this applies to them. Do not assume a non-curated, legacy, or daemon-launched child shares the main agent's Python, site-packages, `PYTHONPATH`, or environment:

- **A non-curated (third-party) `init.json` entry, a legacy `mcp/servers.json` child, or a daemon-launched MCP does not automatically share the main agent's runtime.** Refreshing the agent onto a different source tree (for example an agent-scoped `PYTHONPATH` override or an editable checkout) changes the main process imports, but that stdio child still resolves its own modules from the interpreter named by its activation `command`. If the child needs the new code too, set `PYTHONPATH` (or the equivalent import override) explicitly in that entry's `env`, or install the new package version into the child's venv. A curated addon activated via `init.json`'s `mcp.<name>` entry needs none of this — see above.
- **Behavior that lives in an MCP child is only as new as that child's code.** Rendering, projection, and channel-side logic inside `lingtai.mcp_servers.*` (for example Telegram Task Card footer projection) executes in the child. After a code change to those modules, verify the child's interpreter and import resolution before reporting that a change is live: run a one-shot probe with the exact configured `command` and `env` that prints `sys.executable`, `lingtai.__file__`, and the selected `lingtai.mcp_servers.<name>.__file__`. For a curated addon, the whole probe tuple — interpreter, module `args`, and `PYTHONPATH` — is the running Agent's own, never the `command`/`args`/`PYTHONPATH` strings (if any) stored in `init.json`. (Process inspection such as `lsof` is at best a platform-specific corroboration, not proof of the loaded Python module.)
- **The child venv is a normal package environment.** Site-packages for that interpreter is authoritative unless the activation `env` (or, for a curated addon, the kernel-owned `PYTHONPATH`) overrides the import path. For a curated addon, that interpreter and source root are the main agent's own, so diagnosing "change not visible" starts with whether the Agent process itself has relaunched onto the new venv. For a non-curated, legacy, or daemon-launched child, it still starts with that entry's own activation `command`/`env` tuple. HTTP MCP entries have no local subprocess or interpreter at all; the stdio/subprocess guidance above applies only to stdio entries.

## Venv swap does not auto-reconcile registry records

- **A venv swap does not move live children by itself, and boot/refresh never rewrites existing registry records.** The [self-contained reconcile command and exact contract](runtime-and-identity.md#authorized-venv-swap-and-registry-truth) define which records it rewrites, the fail-closed invalid-registry abort, and why registry truth is not any child's spawn source; do not restate that mechanism here. What is specific to curated addons: for a non-curated or legacy child, the live interpreter/env still come from that entry's own activation spec (`init.json`'s `mcp.<name>` or `mcp/servers.json`), not from this reconcile. For a **daemon-launched** MCP (task `mcp` registrations, plugin `mcp.json` servers), the live command/env come from that task/plugin's own config, never from this registry, so the reconcile has no effect on a daemon-launched child either way. Only a curated **main-agent** `init.json` entry is affected, and even there the effect is cosmetic to the registry file only — the Agent derives its live launcher fresh from its own catalog at load time (see above), so the reconcile is never required for that child's own spawn, only for keeping the durable registry record itself truthful. Either way, a full agent relaunch (not just `refresh`, which only retries failed MCPs) is required before an already-running curated child picks up a new venv. Verify with the one-shot provenance probe above; if it still resolves to the old venv, relaunch — do not claim PASS.

## Module names

| Registry name | Historical distribution | Module name        |
|---------------|-------------------------|--------------------|
| `imap`        | formerly `lingtai-imap`     | `lingtai.mcp_servers.imap`     |
| `telegram`    | formerly `lingtai-telegram` | `lingtai.mcp_servers.telegram` |
| `feishu`      | formerly `lingtai-feishu`   | `lingtai.mcp_servers.feishu`   |
| `wechat`      | formerly `lingtai-wechat`   | `lingtai.mcp_servers.wechat`   |
| `whatsapp`    | formerly `lingtai-whatsapp` | `lingtai.mcp_servers.whatsapp` |
| `cloud_mail`  | (no standalone distribution) | `lingtai.mcp_servers.cloud_mail` |

This table is for orientation, not for `init.json`: a curated `mcp.<name>` entry does not need `["-m", "lingtai.mcp_servers.feishu"]` (or any other `args`) at all — the running kernel's own `mcp_catalog.json` supplies the module `args` for you at load time (see "MCP child processes run in their own runtime" above). The module name matters when reading `mcp_catalog.json` itself, tracing a curated child's stack trace, or writing the legacy `mcp/servers.json` route for a non-curated server. Historical distribution names are retained only for provenance and compatibility notes.

## Telegram setup/readiness checklist

Use this checklist as the Telegram setup acceptance test. It is intentionally separate from the generic catalog/registry steps above: a healthy registry record is not proof that the live listener is usable.

1. **Confirm the live child resolves the current Telegram module.** For a curated `telegram` addon (`source` exactly `lingtai-curated`), the launch module (`-m lingtai.mcp_servers.telegram`), interpreter, and `PYTHONPATH` are derived fresh from the running kernel's own catalog at load time — see "MCP child processes run in their own runtime" above. Editing `command`/`args` in `init.json` or `mcp_registry.jsonl` for a curated entry has no effect on the actual launch; only `LINGTAI_TELEGRAM_CONFIG` (or the agent-relative equivalent) in that entry's `env` matters. If the child instead fails with `ModuleNotFoundError`, leaves the stdio child down, or surfaces to the parent as a closed MCP/closed-resource symptom, that means the running kernel's own installed source is stale or broken, not that the registry needs a module-name edit: run the one-shot provenance probe above to confirm the resolved `sys.executable` and `lingtai.mcp_servers.telegram.__file__`, then correct the kernel install/venv rather than hand-editing `init.json`/`mcp_registry.jsonl`. Do not diagnose that symptom as a Telegram token failure until the probe confirms the live module. Only a **non-curated** or legacy `mcp/servers.json` Telegram entry keeps its own literal `command`/`args` — a stale `-m lingtai_telegram` is an actual, editable bug only there.

2. **Check both registry and runtime layers.** `mcp(action="info", input={}, reasoning="check registry state after addon setup")` proves only that the registry is readable and reports its records/problems. It does **not** prove that a child is mounted. After one controlled refresh or relaunch, confirm there is one live Telegram MCP child/server, the `telegram` tool is mounted, and the intended configured account is mounted; an `info` entry by itself is insufficient.

3. **Separate outbound from inbound proof.** Startup `getMe` and one deliberate direct send prove only outbound Bot API reachability. They do not prove that the listener is receiving updates or that inbound events reach the host agent. Do not call the Bot API `getUpdates` yourself while the Telegram listener may own long polling; a second poller can contend for updates and invalidate the test.

4. **Make one controlled lifecycle change.** After editing a sidecar or Telegram config, perform exactly one controlled `system(action="refresh")` or one controlled relaunch, then inspect the resulting child and mount. Do not start a duplicate parent. A passing readiness check requires the live Telegram MCP child/server **and** its account mounted after that single transition.

5. **Prove the complete inbound/reply path.** Have an allowed-user producer send a fresh test message. Verify the producer's inbound read reaches the host (LICC inbox delivery or `telegram(action="read", chat_id=<chat-id>)`), then reply on that same channel with `telegram(action="reply", message_id=<inbound-message-id>, text=<sanitized-test-reply>)` or the equivalent channel send. An account listing, `getMe`, or an outbound send alone is not end-to-end proof.

6. **Lock down the config.** The secrets directory must be mode `0700` and the Telegram config must be mode `0600`:

   ```bash
   chmod 700 .secrets
   chmod 600 .secrets/telegram.json
   ```

   Use placeholders such as `<bot-token>`, `<allowed-user-id>`, `<chat-id>`, and `<inbound-message-id>` in examples and reports; never include real tokens, IDs, private paths, or raw logs.

## Cloud Mail setup

`cloud_mail` is a REST client for a self-hosted [Cloud Mail](https://github.com/maillab/cloud-mail) deployment (Cloudflare Workers). It is **not** IMAP/SMTP — it talks to Cloud Mail's HTTP API. Inbound mail is discovered by polling Cloud Mail's `POST /public/emailList` and delivered to your inbox via LICC.

- **Env var:** `LINGTAI_CLOUD_MAIL_CONFIG` — path to the config JSON (resolved relative to the agent dir when not absolute).
- **Omnibus tool:** `cloud_mail`. Its action surface is owned by the addon's own manual — `cloud_mail(action="manual")`.
- **Auth model:** the addon mints a *public token* from `admin_email`/`admin_password` via `/public/genToken` for read/poll/search, and logs in with `user_email`/`user_password` via `/login` for `send`. If user creds are absent, read/check/search/poll still work; only `send` is disabled with a clear error.
- **Watermark:** the first poll seeds the per-account high-water mark silently (no flood of old mail) unless `notify_existing: true`. State lives under `<agent_dir>/cloud_mail/<alias>/watermark.json`.

Config schema (plaintext; copy verbatim, never commit real passwords):

```json
{
  "accounts": [
    {
      "alias": "cloudmail",
      "base_url": "https://mail.example.com",
      "admin_email": "admin@example.com",
      "admin_password": "REDACTED",
      "user_email": "admin@example.com",
      "user_password": "REDACTED",
      "send_account_id": 1,
      "allowed_senders": ["only-this@example.com"],
      "poll_interval": 30,
      "notify_existing": false
    }
  ]
}
```

`user_email`/`user_password`/`send_account_id` are optional and only required for `send`. `allowed_senders` (case-insensitive) limits which inbound senders raise an inbox event; the watermark still advances for filtered senders so they never replay. Attachments are not supported in this first pass.

## After it's running

Inbound events (new emails, chat messages) flow into your `.mcp_inbox/<name>/` via the LICC v1 inbox callback contract — the kernel auto-injects them into your next turn as `[system]` messages. You don't poll; the kernel does. Outbound calls go through the omnibus tool: `imap(action="send", ...)`, `telegram(action="send", ...)`, etc. Each addon owns its own action surface and side-effect rules — pull it with `<addon>(action="manual")`; this file stops at setup.

## WeChat setup checklist

WeChat has unique pitfalls that catch agents off-guard. Walk this checklist on every new WeChat setup to avoid wasting the human's time:

1. **Ensure LingTai's runtime venv is current** — the `lingtai-wechat-bootstrap` script is installed by the `lingtai` wheel and lives inside the venv, not necessarily on the system PATH.

2. **Run bootstrap with the full venv path.** The `LINGTAI_WECHAT_CONFIG` relative path (typically `.secrets/wechat/config.json`) resolves against `LINGTAI_AGENT_DIR` first (the agent working dir, like imap/telegram/feishu), then falls back to the project root for backward compatibility. **Preferred:** write secrets into the agent dir, e.g. from the project root:
   ```bash
   ~/.lingtai-tui/runtime/venv/bin/lingtai-wechat-bootstrap .lingtai/<agent>/.secrets/wechat
   ```
   Older setups that wrote `.secrets/wechat` at the **project root** still work via the backward-compat fallback — no migration is required (`Lingtai-AI/lingtai#336`).

3. **No manual credential copy needed** — `config.json` and `credentials.json` are written together in whichever directory you point bootstrap at, and the MCP reads `credentials.json` next to `config.json`.

4. **WSL users**: bootstrap auto-detects WSL and uses `cmd.exe /c start` or `wslview` to open the browser. If neither works, it prints the HTML file path for manual opening.

5. **Refresh the MCP** after bootstrap writes credentials:
   ```
   system(action="refresh")
   ```

6. **Test the connection**:
   ```
   wechat(action="check")
   ```

7. **Session expiry** — WeChat sessions expire (~30 days). When expired, a LICC event with `metadata.event_type: "session_expired"` arrives. Re-run the bootstrap to re-authenticate.
