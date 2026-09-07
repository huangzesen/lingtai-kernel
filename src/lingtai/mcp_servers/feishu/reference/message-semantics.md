---
related_files:
- src/lingtai/mcp_servers/ANATOMY.md
- src/lingtai/mcp_servers/feishu/SKILL.md
- src/lingtai/mcp_servers/feishu/_family.py
- src/lingtai/mcp_servers/feishu/_errors.py
- src/lingtai/mcp_servers/feishu/account.py
- src/lingtai/mcp_servers/feishu/control_cards.py
- src/lingtai/mcp_servers/feishu/manager.py
- src/lingtai/mcp_servers/feishu/service.py
- src/lingtai/mcp_servers/feishu/settings.py
- src/lingtai/mcp_servers/feishu/task_card.py
- src/lingtai/mcp_servers/local_commands/core.py
- src/lingtai/mcp_servers/task_card/event_projection.py
- src/lingtai/mcp_servers/task_card/resident.py
- src/lingtai/mcp_servers/feishu/reference/setup.md
- src/lingtai/mcp_servers/feishu/reference/diagnostics.md
- src/lingtai/mcp_servers/feishu/reference/capability-matrix.md
maintenance: |
  Keep the Feishu model-facing action and message semantics aligned with the
  parent SKILL.md router, the strict family schema, provider adapters, and
  focused behavior tests. This sidecar owns operational depth; do not copy its
  detailed rules back into the resident tool description.
---
# Feishu MCP message semantics

This is the deep, model-facing companion to [`../SKILL.md`](../SKILL.md). The
parent manual is the concise entry point returned by `action='manual'`; load this
sidecar when a call needs message, card, notification, Task Card, settings, or
failure details. Setup and rollout belong to [`setup.md`](setup.md), symptom-led
investigation to [`diagnostics.md`](diagnostics.md), and the capability/non-goal
inventory to [`capability-matrix.md`](capability-matrix.md).

## SETTINGS SHOW

Call `action='settings', input={}, reasoning='inspect Feishu settings'` to read
the current Feishu-owned inventory. Every row contains only `key`, `current`,
`default`, `configurable`, and the exact section pointer in `comment`. SHOW has
no set or reset form and performs no configuration write. Make changes only
through the existing owner procedures below, then call SHOW again after the
stated live/relaunch boundary. If any current value is unavailable or not
JSON-safe, the whole bounded inventory fails without partial rows or raw
exception text.

### Setting config path

`config.path` is the `LINGTAI_FEISHU_CONFIG` reference captured by the running
service at startup. The launcher environment is its only source, so there is no
meaningful default or lower-precedence value. The startup resolver requires a
non-empty reference, expands `~`, resolves a relative path against
`LINGTAI_AGENT_DIR` or the MCP working directory, and then reads strict JSON;
failure prevents manager construction. The reference is sensitive machine
metadata, so SHOW redacts both `current` and `default`. An authorized owner
changes the Feishu MCP environment through the existing Agent configuration
procedure, refreshes or relaunches the MCP, and verifies with another SHOW.

### Setting account aliases

`accounts.aliases` is the service's ordered startup snapshot of
`accounts[].alias`; the first entry is the default outbound account. The owner
JSON selected by `LINGTAI_FEISHU_CONFIG` is the only source, there is no
meaningful default, and a change requires MCP refresh or relaunch. The loader
directly indexes the field but does not validate string type, non-emptiness, or
uniqueness: duplicate aliases remain in order while later entries replace the
lookup-map value. Use stable, non-empty, unique strings as operator guidance,
not as a claimed runtime schema check. Edit the protected owner JSON and verify
the rebuilt service with SHOW.

### Setting account app ids

`accounts.app_ids` is the ordered startup snapshot of the required
`accounts[].app_id` fields paired with the aliases above. App IDs are public
identifiers. The loader does not enforce string type or the conventional
`cli_...` shape before account construction, so those are setup guidance and
provider failures remain visible rather than being presented as schema
validation. The owner JSON is the only source, there is no meaningful default,
and changes require MCP refresh or relaunch before SHOW reflects them.

### Setting account app secrets

`accounts.app_secrets` represents the required `accounts[].app_secret` startup
values paired with the configured accounts. The loader requires the key but does
not add a stronger type/format validator. The owner JSON is the only source and
there is no meaningful default. SHOW always renders both values as `<redacted>`.
Rotate a secret in the Feishu Developer Console, update the protected JSON
through the existing owner procedure, refresh or relaunch the MCP, and use SHOW
only to verify that the complete inventory is available.

### Setting account allowed users

`accounts.allowed_users` represents each running account's sender-admission set.
The optional owner-JSON field is the only source; omission, `null`, or any other
falsy value means unrestricted compatibility behavior. A truthy value is passed
to Python `set()` without a separate list/string/open-ID schema check, so use a
non-empty list of sender `open_id` strings as operator guidance and allow
construction failures to remain visible. Authorization data and the absence
default are both redacted by SHOW. Edit the protected owner JSON, refresh or
relaunch the MCP, and verify inventory availability; saved contacts never
change this policy.

### Setting task card enabled

`taskcard.enabled` is the live service value for Feishu's Agent-wide resident
card projection. At service construction, an exact boolean `taskcard` in
`<workdir>/feishu/taskcard.json` wins; every other value falls back to `true`.
There is no environment peer. An admitted Feishu user may use the existing
`/taskcard on` or `/taskcard off` command, whose validated setter persists and
applies the change live; a direct owner-file edit requires MCP relaunch. SHOW
is read-only and reads the live getter again on every call.

### Setting task card normal rows

`taskcard.normal_rows` is the live number of normal automatic rows. At service
construction, only a Python integer from `1` through `10` in
`<workdir>/feishu/taskcard.json` is accepted; booleans and every out-of-range or
other value fall back to `1`. There is no environment peer. The existing
`/taskcard N` command validates `1..10`, persists the value, and applies it live;
a direct owner-file edit requires MCP relaunch. SHOW reads the live getter again
and never writes the file.

## RECIPIENTS: receive_id / receive_id_type

- `send` targets a recipient by `receive_id` plus `receive_id_type`. Use
  `receive_id_type='open_id'` for an individual user (`ou_xxx`) and
  `receive_id_type='chat_id'` for a group chat (`oc_xxx`). `receive_id_type`
  defaults to `open_id` when omitted.
- `email`, `user_id`, and `union_id` are also accepted as `receive_id_type`
  values when you only have that identifier for a user.

## SEND vs REPLY

- `send`, `reply`, and `edit` require exactly one of legacy `text` or structured
  `content`; passing both is rejected before Feishu I/O. `text='...'` remains
  the plain-text shortcut.
- Structured content is a strict tagged union in this slice:
  `{'type':'text','text':'...'}`,
  `{'type':'markdown','markdown':'...'}`, or
  `{'type':'post','post':{...}}`,
  `{'type':'card','card':{'schema':'2.0',...}}`, plus the
  media/share/sticker forms below. Unknown keys or mixed variants are rejected.
- `reply` (`message_id` from `read` results or an inbound event, plus `text` or
  `content`) replies to a specific incoming message; prefer it when answering
  that message. It defaults `reply_in_thread=true` when the persisted target has a `thread_id`,
  otherwise false. An explicit boolean overrides that default. If the reply
  target is gone, the call fails and never silently starts a fresh message.
- `send` (`receive_id`, `receive_id_type`, `text` or `content`) starts a fresh
  message; use it for unsolicited or standalone messages.
- Markdown is converted by the channel SDK to a Feishu post and split at safe
  boundaries when long. Successful send/reply results include the primary
  compound `message_id`, ordered `message_ids`, `chunk_count`, and `chunks`;
  every chunk of a topic reply stays in that topic.
- If a later chunk fails, the action returns the normal `status='failed'` error
  fields plus `partial_delivery=true`, the exact delivered `message_ids`,
  `failed_chunk_index`, total `chunk_count`, `delivered_chunk_count`, and
  `automatic_retry_allowed=false`. One `status='partial'` sent record preserves
  those exact side effects. Never replay the whole action: even when the failed
  chunk's provider classification says `retryable=true`, doing so can duplicate
  the already delivered chunks. Partial replies do not add the done reaction.
- `edit` accepts text, markdown, post, or a complete schema-2.0 card and updates
  every physical chunk in the logical sent record, even when called with a
  secondary compound ID, and updates the persisted record only after Feishu
  confirms every edit. `delete` resolves the same logical chunk group and
  deletes every physical member. For either action, supply only a bot-authored
  outgoing message ID from this adapter's sent history; the adapter resolves
  the local record but does not independently prove provider-side authorship.
  Successful lifecycle results expose the ordered `message_ids`, `chunk_count`,
  and `chunks`. A partial edit/delete persists exact successes and failures,
  disables whole-operation automatic replay, and requires provider-state
  reconciliation before another attempt. Card edits replace the existing card
  in place through Feishu's native card update API. Feishu does not expose media
  messages through the same edit path.

## INTERACTIVE CARDS AND BUSINESS CALLBACKS

- `send` and `reply` accept a complete schema-2.0 interactive card through
  `content.type='card'`; `edit` replaces a previously sent card with another
  complete schema-2.0 card. The sent record keeps the exact card JSON, while
  its text preview extracts visible card text and never traverses button callback
  values.
- A business button click is admitted only when Feishu supplies an actor and
  that actor passes the account's `allowed_users` gate. Authorized callbacks
  are serialized per account/chat, durably deduplicated by Feishu's stable event
  id, persisted in the original conversation with `message_type='card_action'`,
  and wake the agent. Distinct later clicks on the same button remain distinct
  events even when actor, source card, and callback value are identical.
- `read` exposes the normalized callback under `card_action`, its exact
  `feishu_event_id`, the source card's `source_message_ref`, and the complete
  raw envelope under `feishu`. A callback record is not itself a Feishu message
  that can be replied to: use its `source_message_ref` to update the source card
  when appropriate, or `send` a fresh response to the callback's chat.
- The Feishu application must enable card callback delivery over the same
  long-connection mode and publish that configuration. If clicking a button
  produces only client-side success feedback but no `card_action` record or
  agent wake, verify that application callback setting; ordinary event
  subscriptions and messaging permissions do not prove card callbacks are
  being delivered.

## LOCAL COMMANDS AND CONTROL CARDS

- `/help`, `/status`, `/kanban`, `/system`, `/refresh`, `/sleep`, `/clear`, and
  `/taskcard` execute inside the Feishu MCP without an LLM call. Direct-message
  commands are handled immediately. Group and topic commands still pass the
  normal account `allowed_users` gate and require an explicit `@Bot`; unknown
  slash commands remain ordinary Agent input.
- Responses are updateable Feishu schema-2.0 control cards. `/kanban` exposes
  seven drill-down layers, `/system` provides document navigation, and buttons
  update their source control card in place. Internal control callbacks never
  become `card_action` inbox records and never wake the Agent. Ordinary
  business-card values keep the business callback behavior described above.
- Control-card clicks reuse the account actor/allowlist gate and the manager's
  per-account/chat serialization. Their stable Feishu event ids are stored
  only as bounded SHA-256 hashes in `feishu/control_callbacks.json`, so a
  callback replay after refresh cannot repeat a local signal.
- User-facing card titles, navigation, command descriptions, and feedback use
  `agent.language`: `zh` is Chinese, `en` is English, and `wen` is literary
  Chinese. Unknown or missing languages use English.
- `/taskcard on|off` and `/taskcard N` (1–10) configure Feishu's Agent-wide
  resident-card presentation. The durable owner is
  `<workdir>/feishu/taskcard.json`; exact resident targets remain independently
  routed and persisted by `account + chat + optional thread`. Turning cards off
  suppresses projection without guessing or deleting unknown cards; turning
  them on reprojects known routes conservatively.
- `/refresh`, `/sleep`, and `/clear` write the same established Agent signals
  as the shared command core. Their control-card feedback stays local and does
  not create a second Agent conversation turn.

## OUTBOUND MEDIA, SHARES, AND STICKERS

- `send` and `reply` additionally accept `image`, `file`, `audio`, `video`,
  `share_chat`, `share_user`, and `sticker` content.
- Media uses one strict source: `{'type':'path','path':'/absolute/file'}` uploads
  a readable local file, while `{'type':'key','key':'<provider key>'}` reuses an
  already uploaded Feishu key. Relative paths and URL downloads are rejected;
  use a downloaded attachment path from `read`, or an explicit provider key.
  Provider keys must be owned by this Bot; a key copied from an inbound user
  message may be readable yet still be rejected for outbound reuse by Feishu.
- Shapes:
  `{'type':'image','source':SOURCE,'caption':'optional markdown'}`,
  `{'type':'file','source':SOURCE,'file_name':'optional name'}`,
  `{'type':'audio','source':SOURCE}`, and
  `{'type':'video','source':SOURCE,'caption':'optional markdown'}`.
  Image/video captions are rendered as Feishu post messages. File/audio
  captions are not supported by Feishu and are intentionally absent.
- Sharing/sticker shapes are
  `{'type':'share_chat','chat_id':'oc_...'}`,
  `{'type':'share_user','user_id':'ou_...'}`, and
  `{'type':'sticker','file_key':'...'}`.
- Sent records preserve the exact source descriptor for `read`, while bounded
  notification previews expose only safe media summaries such as type, filename,
  and size — never provider keys or the local source path.
- Each materialized wire chunk is attempted exactly once. A rejected post or
  caption is returned as a failure; it is never silently resent as plain text.

## READING: check / read / search

- `check`: list recent conversations with unread counts (optional `account`).
- `read`: read messages from one chat (`chat_id`; optional `limit`, `account`).
- `search`: regex search over inbox messages (`query`; optional `account`,
  `chat_id`).
- Reactions, read receipts, and Bot join/leave events are retained in the
  reserved `chat_id='events'` conversation. They do not enter the LICC
  notification mirror and never wake the agent; use `read` or `search` when the
  event history is relevant. Each record carries a concise `event` projection
  plus the complete raw envelope under `feishu`.
- Channel-event actors pass through the same account `allowed_users` gate. The
  reserved events conversation is read-only: do not use it as a `send` recipient.

## PLACEHOLDER / PROGRESS

- For responses that take more than ~5s, send `action='send'` with
  `placeholder=true` and interim text, Markdown, or post content. Feishu sends
  it as a native schema-2.0 progress card and returns a compound `message_id`.
- Update that same card with `action='edit'` only when the work enters a
  meaningful new phase. A progress-card edit remains a progress card even when
  the edit input uses text/Markdown/post; custom-card and media replacement are
  rejected on this path.
- Send the final answer as a separate durable `send` or `reply` message. Never
  edit the progress card into the final answer, and do not update it for every
  token or trivial internal step.
- Incoming messages receive the native `Typing` reaction while work is pending;
  it is removed when the first response/progress card is sent. Existing `OK`
  (seen) and `THUMBSUP` (done after reply) reactions continue independently.

## AUTOMATIC RESIDENT TASK CARD

- The Bot automatically maintains one schema-2.0 resident Task Card for every
  admitted `account + chat + optional thread` route. This is a mechanical,
  bounded projection of the agent's safe public event rows; the model
  should not send, edit, answer, or otherwise manage it through the public
  `feishu` actions.
- Direct chats and ordinary group conversations receive a card in the chat.
  Topic messages receive their own resident card inside that exact topic. The
  automatic route never guesses a topic from another conversation.
- A card that is still last is updated in place. After this process observes a
  newer message below it, rotation is old-first: the exact persisted card is
  deleted (or confirmed gone) before one replacement is sent. A refresh has no
  trusted ordering high-water mark, so it conservatively updates the persisted
  card in place until a later message is actually observed; it never guesses
  and sends a duplicate.
- Automatic Task Cards and `placeholder=true` progress cards are independent.
  The automatic card summarizes agent behavior; a placeholder communicates a
  user-meaningful phase. Final answers remain separate durable messages.
- The same resident also carries the channel-neutral intrinsic Task Card body
  from `<workdir>/taskcard/taskcard.md` when `<workdir>/taskcard/status` is
  exact `active`. It is composed below the automatic frame under
  `— TASK CARD —`; the model manages that artifact only through the public
  intrinsic `task_card` tool, never through Feishu message actions.
- Exact `inactive` clears only the programmable `WATCH` slot and preserves the
  automatic frame. Missing, unreadable, invalid, or blank producer state is a
  no-op that preserves the last successfully delivered programmable frame. One
  route's delivery failure does not stop projection to other chats/topics.

## REACTIONS

- `react` adds or removes one Feishu reaction on a compound `message_id`.
  Adding requires `operation='add'` plus Feishu's symbolic `emoji_type` (for
  example `SMILE`) and returns the provider `reaction_id`. Removing requires
  `operation='remove'` plus that exact `reaction_id`; do not substitute an
  emoji glyph or `emoji_type` for removal.
- Add and remove are each attempted exactly once. A missing or revoked target
  returns `error_code='TARGET_REVOKED'` and is never converted into a new
  message or another reaction.

## CONTACTS / ACCOUNTS

- `contacts`: list saved contacts (optional `account`).
- `add_contact`: save a contact alias (`open_id`, `alias`; optional `name`,
  `chat_id`). Saving an alias does not grant inbound permission on its own.
- `remove_contact`: remove a contact (`alias` or `open_id`).
- `accounts`: list configured app accounts.

## MESSAGE IDS

- `message_id` is the compound id returned by `read` or supplied by an inbound
  event (`{alias}:{chat_id}:{feishu_message_id}`); pass it back verbatim to
  `reply`, or use a bot-authored outgoing ID from sent history for `edit` and
  `delete`.

## INBOUND CONVERSATIONS

- Direct messages are admitted without an `@Bot` mention. Group and topic
  messages are admitted only when they explicitly mention this bot; `@all`
  alone does not wake it.
- `allowed_users`, when configured for an account, still filters the sender's
  `open_id` in both direct and group chats. Saving a contact does not change
  this admission rule.
- `read` preserves the legacy fields and adds `thread_id`, `root_id`,
  `reply_to`, resolved `mentions`, the SDK-normalized `content` union,
  normalized sender identity fields, downloaded `attachments`, and the complete
  raw event under `feishu`.
- Image, file, audio, video, sticker, video-cover, and rich-post resources are
  stored under the message's `attachments/` directory. Each attachment keeps
  its Feishu `type` and `file_key`; `status='downloaded'` adds the safe local
  `filename`, absolute `path`, and byte `size`, while `status='failed'` keeps
  the original descriptor plus a bounded `error` instead of discarding it.
- The current message's persistent notification context includes at most eight
  secret-safe attachment projections (`type`, download/transcription status,
  local `path`, filename, and size). Provider file keys and the raw envelope
  remain on `read` only. When the user's intent depends on an image, inspect the
  listed path with `vision`; use the appropriate local tool/skill for documents,
  audio, or video instead of replying from the media placeholder.
- Audio messages continue through local Whisper transcription after download. A
  successful transcript remains in `voice_transcript` and becomes the message
  text. Download or transcription failure stays attached to the resource record,
  while the normalized content/raw envelope remain available for diagnosis;
  failure is not collapsed into a text-only message.
- For group commands, the normalized `text` removes this bot's own mention;
  other resolved mentions remain visible. `content.kind` identifies the original
  Feishu content family.
- Topic/thread routing metadata drives `reply`: an omitted `reply_in_thread`
  follows the persisted target's `thread_id`, so topic messages stay in their
  topic while ordinary messages remain flat.

## NOTIFICATIONS: TRANSIENT HOOK vs PERSISTENT CONTEXT

Inbound Feishu messages surface to the agent in two `_meta` lanes:

- `_meta.agent_meta.notifications.attention.mcp.feishu` — a compact high-
  attention hook only: `data.message_ids` and dismiss guidance, never message
  text or routing context.
- `_meta.agent_meta.notifications.persistent.mcp.feishu` — durable context:
  recent conversation messages (bounded text, both directions), sender/chat
  routing hooks, reply refs when present, and per-message comments for the
  agent's own outgoing messages or truncated text.

The feishu tool remains the source of truth: neither lane marks anything read,
so use `read`/`check` for exact producer state — especially when a persistent
message is truncated. Reply in Feishu when the message arrived through Feishu
(`reply` with the compound message id, or `send` to the chat/open_id). After
handling, dismiss the transient hook with
`notification.dismiss_channel("mcp.feishu")`; the persistent block is context
history, not unread state. Generic mirror-vs-canonical-state and dismiss-safety
rules live in
[`notification-manual`](../../../tools/notification/manual/SKILL.md).

## SIDE EFFECTS & ERROR SURFACING

- `send`, `reply`, `edit`, `delete`, and `react` affect real Feishu state — they
  are external side effects, so confirm recipient and content before sending
  unsolicited messages.
- Every action failure has the stable fields `status='failed'`, compatible
  `error` text, identical `message`, `error_code`, `retryable`, and
  `retry_after_seconds` (number or null). Permission, format, target-revoked,
  and rate-limit failures retain their channel classification. Start a new
  attempt only when `retryable=true`, and honor a non-null
  `retry_after_seconds`; the Bot never hides an automatic outbound retry.

## PUBLIC TOOL FAMILY: strict LTP-v2

Raw MCP discovery exposes exactly one public tool, `feishu`. It is an
independent strict LTP-v2 family with the closed root
`{action, input, reasoning, summarize?}` (`action`, `input`, and `reasoning`
required) and a closed action-owned input branch. `feishu` actions are exactly
`send`, `check`, `read`, `reply`, `react`, `search`, `delete`, `edit`,
`contacts`, `add_contact`, `remove_contact`, `accounts`, `settings`, and
`manual`. `settings` is read-only SHOW; `manual` is the discovery path for this
packaged documentation. Do not use the retired flat/legacy shape, `_reasoning`,
aliases, or a generic dispatcher.
