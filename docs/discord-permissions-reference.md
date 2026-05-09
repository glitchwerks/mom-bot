# Discord permissions, scopes & intents reference

> **Purpose:** Single source of truth for what mom-bot needs from Discord at every configuration layer. Generated 2026-05-08 during Pre-Epic-0 conversation. See plan `docs/superpowers/plans/2026-05-08-mom-bot-framework.md` § Pre-Epic-0 and issue #1 for the audit checklist that consumes this reference.
>
> **Status:** Conservative install profile (no `Manage Events`). Permissive variant documented at the bottom — flip if/when human-admin-created event editing becomes a real need.

## The six layers

Discord permissions are split across six distinct configuration surfaces. The same capability often touches more than one — confusion almost always traces back to setting one layer and assuming the others followed.

| # | Layer | Where configured | What it controls |
|---|---|---|---|
| 1 | **OAuth2 scopes** | Developer Portal → Installation tab (preferred, persistent) — or OAuth2 → URL Generator (one-off scratchpad) | What the *application* installs as |
| 2 | **Bot install bitfield** | Same screen as layer 1 — Installation tab → Default Install Settings → Permissions picker (or URL Generator's Permissions section). Stored as the `permissions=` integer in the Install Link | Default permissions the bot's role gets at invite time |
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
| **`Manage Roles`** | **`1 << 28`** | **yes (new)** | Day-role sync — toggle membership on `Attack Day N` roles when siege-web pushes assignment changes (Epic 2.6) |
| **`Create Events`** | **`1 << 44`** | **yes (new)** | Autonomous tank-week creation; admin manual create; cancel of bot-created events |
| `Manage Events` | `1 << 33` | **no (deliberate)** | Only required to edit/cancel events created by **human admins** in the Discord UI. v1.0 doesn't need this. See § Permissive variant |
| `Read Message History` | `1 << 16` | no | mom-bot doesn't read history; slash commands deliver structured payloads |
| `Mention Everyone` | `1 << 17` | maybe | Only if reminders use `@here` / `@everyone`. The plan uses **role mentions**, which require the role itself to be mentionable in role settings, not this bit |
| `Use Application Commands` | `1 << 31` | **N/A — wrong layer** | Member permission controlling who can invoke commands. Not a bot grant |

**Combined integer for v1.0 (conservative):** `Send Messages | Embed Links | Attach Files | Manage Roles | Create Events` = `2048 + 16384 + 32768 + 268435456 + 17592186044416` = `17592454531072`.

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

### Role-ordering caveat (`Manage Roles` only)

Discord enforces a hard rule on `Manage Roles`: a bot can only assign or remove roles that are **strictly lower in the role list than its own highest role**. This is layer-4 configuration — the install bitfield (layer 2) grants the *capability*, but layer 4's role-ordering decides which *specific* roles the bot can actually touch.

**For the day-role sync feature (Epic 2.6) this means:**

- mom-bot's role must be positioned **above** every `Attack Day N` role in the guild's Role list (Server Settings → Roles, drag-to-reorder)
- Any human-managed role above mom-bot (e.g. `Clan Deputies`, `Admin`) is naturally outside mom-bot's reach — that's a feature, not a bug
- If a day-role is accidentally moved above mom-bot's role, every role-toggle call for that day returns 403 silently — the only signal is in App Insights / failed-call telemetry

**Audit checkpoint:** during Pre-Epic-0 (issue #1) and any time roles are reordered, verify the bot's role rank.

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

**Use the Installation tab as the canonical save mechanism.** It is the only Discord-side surface that stores the configuration permanently and travels with the application itself.

### Recommended: Installation tab → Default Install Settings

1. Developer Portal → mom-bot app → **Installation** (left sidebar)
2. Under **Default Install Settings → Guild Install**:
   - **Scopes:** tick `bot` and `applications.commands`
   - **Permissions:** open the picker and tick the layer-2 permissions from this doc — `Send Messages`, `Embed Links`, `Attach Files`, `Manage Roles`, `Create Events`
3. Click **Save Changes**. Discord computes the bitfield and stores it on the application
4. The page now exposes a permanent **Install Link** at the top — copy it into the "Pinned install configuration" section below
5. (Optional) Under **Authorization Methods**, leave only **Discord Provided Link** enabled to disable ad-hoc installs entirely

The link encodes the *application's stored defaults at the time of click*, not the time the link was copied. So one Install Link, indefinitely re-used, always reflects the current saved configuration. To change permissions later: edit the defaults, save, re-invite — same link, new perms.

### Not recommended for canonical save: OAuth2 → URL Generator

The URL Generator is a scratchpad: every visit resets the pickers, the URL it generates is the only output, and there's no concept of "saved defaults." Useful for:

- Generating an alternate install URL with different perms for testing
- Generating a user-install URL (`integration_type=1`) variant

If you find yourself relying on the URL Generator as the canonical install path, that's a sign you should move the configuration to the Installation tab instead.

### Project-side record

The Installation tab is Discord's source of truth; this doc is the project's source of truth. They should agree. If the Discord application is ever recreated (account loss, fresh start), the "Pinned install configuration" section below is the recipe to recreate the same setup.

### Pinned install configuration

> Confirmed via Discord Developer Portal Installation tab on `2026-05-08`. Update via PR if defaults change.

- **Application ID:** `1362590154002530494`
- **Install Link (canonical):** `https://discord.com/oauth2/authorize?client_id=1362590154002530494`
  - This is the Installation-tab link format. Discord stores `scope` and `permissions` server-side; the URL itself does not echo them. Editing the Default Install Settings in the portal silently changes what this link installs
- **Saved scopes (Default Install Settings → Guild Install):** `bot`, `applications.commands` — pending visual reconfirmation in the portal
- **Saved permissions integer (Default Install Settings → Guild Install):** `17592454531072` (conservative profile — see Layer 2) — pending visual reconfirmation in the portal
- **Privileged gateway intents enabled in portal:** `GUILD_MEMBERS` ✓ confirmed `2026-05-08`
- **Code-side intents flag (to be set in mom-bot's `Intents(...)` at Epic 0):** `GUILDS | GUILD_MEMBERS | GUILD_SCHEDULED_EVENTS`
- **URL Generator equivalent (for reference only — NOT the canonical link):** `https://discord.com/oauth2/authorize?client_id=1362590154002530494&scope=bot+applications.commands&permissions=17592454531072`

> The "URL Generator equivalent" is included as a sanity-check reference: if you ever need to verify what the Installation tab is *currently configured to install*, generate this URL via OAuth2 → URL Generator with the conservative permissions ticked, and compare. Drift between the two URLs is the signal that someone edited the Installation tab defaults outside this doc.

## Permissive variant — when to flip

The conservative profile excludes `Manage Events`. Adopt the permissive variant if:

- Human admins routinely create tank-week events in the Discord UI, and want mom-bot to be able to cancel/edit them
- An incident occurs where a stale human-created event needs bot cleanup and the admin can't reach the Discord UI in time

To flip: re-generate install URL with permissions integer `17601044465664` (conservative `17592454531072` + `Manage Events` `8589934592` = `17601044465664` — recompute via the portal picker rather than trusting this number; the conservative-base integer shifts whenever a layer-2 permission is added, and integer arithmetic on Discord permission bitfields is famously easy to fat-finger).

> **One-way-door warning:** removing a permission later is silent (no notification to anyone), but adding one requires re-invite. So conservative-first is the lower-regret default.

## References

- Discord docs § Permissions: https://docs.discord.com/developers/topics/permissions (fetched 2026-05-08)
- Discord docs § Gateway intents: https://docs.discord.com/developers/topics/gateway#gateway-intents (fetched 2026-05-08)
- Discord docs § Application command permissions: https://docs.discord.com/developers/interactions/application-commands#permissions (fetched 2026-05-08)
- discord.py § Permissions: https://discordpy.readthedocs.io/en/stable/api.html#permissions
- discord.py § Intents: https://discordpy.readthedocs.io/en/stable/api.html#intents
- Plan: `docs/superpowers/plans/2026-05-08-mom-bot-framework.md` § Pre-Epic-0
- Tracking: issue #1 (audit), issue #3 (this doc)
