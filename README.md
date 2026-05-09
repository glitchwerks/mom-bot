# mom-bot

Discord bot consolidating two existing bots — `siege-web`'s notifications sidecar and the reminder system from `I:\games\raid\siege\clan\` — into a single bot with interactive slash commands.

**Status:** framework planning complete; Pre-Epic-0 audit pending. Implementation has not started.

## Documentation

- **Framework plan:** [`docs/superpowers/plans/2026-05-08-mom-bot-framework.md`](docs/superpowers/plans/2026-05-08-mom-bot-framework.md) — locked design decisions, phasing, risks, and verification per epic
- **Cross-repo dependency:** Epic 2.5 lands as a v1.2 ticket in [glitchwerks/siege-web](https://github.com/glitchwerks/siege-web)

## Roadmap

The plan defines 5 epics + 1 cross-cut + 1 pre-epic gate:

| Phase | Scope |
| --- | --- |
| **Pre-Epic-0** | Discord application audit + reminder-bot deployment typing (gates Epic 0) |
| **Epic 0** | Skeleton: new repo wiring, Discord client, App Insights, SQLite baseline, `/ping` health-check |
| **Epic 1** | Reminder lift-and-shift (port from `I:\games\raid\siege\clan\`; JSON file → SQLite) |
| **Epic 2** | Sidecar lift-and-shift (port `siege-web/bot/`'s 6 HTTP endpoints into mom_bot's service half) |
| **Epic 2.5** | Siege-web cross-cut (`/me/preferences` endpoints + `X-Acting-Discord-Id` header support — lands in siege-web v1.2) |
| **Epic 3** | Interactive slash commands (~13 commands across `/siege` and `/reminder` groups) |
| **Epic 4** | Cutover (deploy to new Azure RG `mom-bot-prod`, retire siege-bot + old reminder-bot) |

See the framework plan for design decisions, scope locks, risks, and verification per epic.

## Versioning

Mom-bot is its own product on its own version track (`mom-bot v0.1` → `v1.0`), separate from siege-web. The runtime is coupled to siege-web by design (shared Discord token, sidecar HTTP contract, shared guild) — the separate-repo / separate-versioning is for code-organization clarity, not real separability.

## License

TBD — to be set before first public release.
