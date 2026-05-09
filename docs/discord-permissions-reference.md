# Discord permissions, scopes & intents reference

> **Purpose:** Single source of truth for what mom-bot needs from Discord at every configuration layer. Generated 2026-05-08 during Pre-Epic-0 conversation. See plan `docs/superpowers/plans/2026-05-08-mom-bot-framework.md` § Pre-Epic-0 and issue #1 for the audit checklist that consumes this reference.
>
> **Status:** Conservative install profile (no `Manage Events`). Permissive variant documented at the bottom — flip if/when human-admin-created event editing becomes a real need.

## The six layers

Discord permissions are split across six distinct configuration surfaces. The same capability often touches more than one — confusion almost always traces back to setting one layer and assuming the others followed.

| # | Layer | Where configured | What it controls |
|---|---|---|---|
| 1 | **OAuth2 scopes** | Developer Portal → OAuth2 → URL Generator (or Installation tab) | What the *application* installs as |
| 2 | **Bot install bitfield** | Same install URL, `permissions=` integer | Default permissions the bot's role gets at invite time |
| 3 | **Gateway intents** | Developer Portal → Bot tab + `Intents(...)` in code | Which real-time events the bot subscribes to |
| 4 | **Guild role permissions** | Server Settings → Roles → bot's role | Actual permissions the bot's role has in the guild (admins can override layer 2) |
| 5 | **Channel permission overwrites** | Channel → Edit Channel → Permissions | Per-channel grant/deny on top of layer 4 |
| 6 | **Application command permissions** | `default_member_permissions` in code, or Server Settings → Integrations → mom-bot → command | Member-side restriction on who can invoke each slash command |

## Layer 1 — OAuth2 scopes

| Scope | Required | Why |
|---|---|---|
| `bot` | yes | Install as a bot user |
| `applications.commands` | **yes (new for mom-bot)** | Enables slash-command registration. There is no equivalent permission bit at layer 2 — `Use Application Commands` is a member permission, not a bot permission |

## Layer 2 — Bot install bitfield

| Permission | Bit | Required | Why mom-bot needs it |
|---|---|---|---|
| `Send Messages` | `1 << 11` | yes (existing) | Channel posts (sidecar `post-message`, reminders) |
| `Embed Links` | `1 << 14` | yes (existing) | Ephemeral embed responses; reminder formatting |
| `Attach Files` | `1 << 15` | yes (existing) | Sidecar `post-image` |
| **`Create Events`** | **`1 << 44`** | **yes (new)** | Autonomous tank-week creation; admin manual create; cancel of bot-created events |
| `Manage Events` | `1 << 33` | **no (deliberate)** | Only required to edit/cancel events created by **human admins** in the Discord UI. v1.0 doesn't need this. See § Permissive variant |
| `Read Message History` | `1 << 16` | no | mom-bot doesn't read history; slash commands deliver structured payloads |
| `Mention Everyone` | `1 << 17` | maybe | Only if reminders use `@here` / `@everyone`. The plan uses **role mentions**, which require the role itself to be mentionable in role settings, not this bit |
| `Use Application Commands` | `1 << 31` | **N/A — wrong layer** | Member permission controlling who can invoke commands. Not a bot grant |

**Combined integer for v1.0 (conservative):** `Send Messages | Embed Links | Attach Files | Create Events` = `2048 + 16384 + 32768 + 17592186044416` = `17592186095616`.

> **Recommendation:** don't hand-encode this. Use Developer Portal → OAuth2 → URL Generator and tick the boxes; it computes the integer for you and produces the install URL atomically.

## Layer 3 — Gateway intents

Set both in Developer Portal → Bot → Privileged Gateway Intents (for the privileged ones) AND in code via `discord.Intents(...)`. The portal toggle gates whether Discord *sends* the events; the code flag gates whether discord.py *subscribes*.

| Intent | Privileged? | Required | Why |
|---|---|---|---|
| `GUILDS` | no | yes (default-on) | Receive guild create/update events; populate guild cache |
| `GUILD_MESSAGES` | no | no | mom-bot doesn't react to message events |
| `GUILD_MEMBERS` | **yes** | **yes** | Member lookup (`/siege member`, sidecar `get_members`). Toggle in portal first |
| `GUILD_SCHEDULED_EVENTS` | no | yes | Required for tank-week externally-deleted detection |
| `MESSAGE_CONTENT` | yes | no | mom-bot doesn't read message body content |
| `GUILD_PRESENCES` | yes | no | Not needed |
| `DIRECT_MESSAGES` | no | maybe | Only if `send_dm` needs DM-channel inbound events; outbound DMs work without it |

> **Easy-to-forget gotcha:** `GUILD_SCHEDULED_EVENTS` is not privileged, so it's invisible in the portal toggle list — it has to be turned on in the bot's `Intents(...)` flags in code. Skip it and tank-week's externally-deleted detection just silently never fires.

## Layer 4 — Guild role permissions (live verification)

After install/re-invite, the bot's role inherits the layer-2 bitfield. Verify in the live guild:

- [ ] mom-bot integration role exists in Server Settings → Roles
- [ ] Each layer-2 permission is ticked on the role
- [ ] Bot's role rank is above any role it might `@`-mention (matters for role mentions in reminders)

Admins can strip permissions at this layer at any time — the bot has no way to know until an API call returns 403. If reminders or events stop working post-deploy, layer 4 is the first thing to check.

## Layer 5 — Channel permission overwrites

For each **destination channel** mom-bot posts to:

| Channel kind | Bot needs |
|---|---|
| Reminders channel | `Send Messages`, `Embed Links`, `Mention Everyone` (only if `@everyone` / `@here` used; not needed for `@role`) |
| Image-post target channels (sidecar) | `Send Messages`, `Embed Links`, `Attach Files` |
| Slash-command invocation channels | None bot-side — Discord routes interactions directly to the gateway |

## Layer 6 — Application command permissions

`default_member_permissions` baked into each slash command at registration. This is **member-side soft enforcement** — Discord uses it to hide commands from members who lack the named permission. Admins can override per-command via Server Settings → Integrations.

> **Layer 6 is not the security boundary.** mom-bot's `@require_admin_role` decorator is. Layer 6 is for UX (hiding admin commands from autocomplete for non-admins).

| Slash command | `default_member_permissions` | Notes |
|---|---|---|
| `/ping` | none (open) | Health check |
| `/siege me`, `/siege next`, `/siege status`, `/siege member` | none (open) | Member self-service reads |
| `/siege preferences view`, `/siege preferences set` | none (open) | Per-user `me` semantics |
| `/reminder list`, `/reminder tank-week list` | none (open) | Reads |
| `/reminder add`, `/reminder remove`, `/reminder pause`, `/reminder resume` | `Permissions.manage_guild` | Belt-and-suspenders with decorator |
| `/reminder tank-week create`, `/reminder tank-week cancel` | `Permissions.manage_events` | Same |
| `/admin audit-log <member>` | `Permissions.manage_guild` | Admin-only |

## Saving / persisting the install configuration

Discord doesn't have a "save install template" CLI, but there are three durable artifacts:

1. **Developer Portal → Installation tab** — set "Default Install Settings" (scopes + permissions integer) once. The portal then exposes a permanent "Install Link" that uses these defaults. This is the closest thing to a saved template — it travels with the application itself, survives session, and any future re-invite uses the same configuration.
2. **OAuth2 URL Generator** — produces a one-off URL for ad-hoc installs. Useful for testing variations; not durable.
3. **This doc + the actual install URL committed below** — the canonical project-side record. If the Discord application is ever recreated, the bitfield integer + scope list here is the recipe to recreate it.

### Pinned install configuration (filled in once Pre-Epic-0 audit completes)

> The values below are placeholders until the audit confirms token inheritance is feasible. Update via PR after Pre-Epic-0 closes.

- **Application ID:** `<TBD-pre-epic-0>`
- **OAuth2 scopes:** `bot applications.commands`
- **Permissions integer:** `17592186095616` (conservative — see Layer 2)
- **Install URL:** `https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot+applications.commands&permissions=17592186095616`
- **Privileged gateway intents enabled in portal:** `GUILD_MEMBERS`
- **Code-side intents flag:** `GUILDS | GUILD_MEMBERS | GUILD_SCHEDULED_EVENTS`

## Permissive variant — when to flip

The conservative profile excludes `Manage Events`. Adopt the permissive variant if:

- Human admins routinely create tank-week events in the Discord UI, and want mom-bot to be able to cancel/edit them
- An incident occurs where a stale human-created event needs bot cleanup and the admin can't reach the Discord UI in time

To flip: re-generate install URL with permissions integer `17601875988480` (adds `1 << 33` = `8589934592`), re-invite the bot, then update this doc's "pinned install configuration" section.

> **One-way-door warning:** removing a permission later is silent (no notification to anyone), but adding one requires re-invite. So conservative-first is the lower-regret default.

## References

- Discord docs § Permissions: https://docs.discord.com/developers/topics/permissions (fetched 2026-05-08)
- Discord docs § Gateway intents: https://docs.discord.com/developers/topics/gateway#gateway-intents (fetched 2026-05-08)
- Discord docs § Application command permissions: https://docs.discord.com/developers/interactions/application-commands#permissions (fetched 2026-05-08)
- discord.py § Permissions: https://discordpy.readthedocs.io/en/stable/api.html#permissions
- discord.py § Intents: https://discordpy.readthedocs.io/en/stable/api.html#intents
- Plan: `docs/superpowers/plans/2026-05-08-mom-bot-framework.md` § Pre-Epic-0
- Tracking: issue #1 (audit), issue #3 (this doc)
