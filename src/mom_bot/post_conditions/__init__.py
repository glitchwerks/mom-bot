"""Post-condition preference commands and siege-web client for mom-bot.

Provides three Discord slash commands that proxy to siege-web's
per-member preferences API:

- ``/post-conditions``      — view the full post-condition catalog.
- ``/post-conditions-get``  — view the invoking user's current preferences.
- ``/post-conditions-set``  — paginated UI to update the invoking user's
                              preferences.

Public surface
--------------
:class:`~mom_bot.post_conditions.client.SiegeWebClient`
    HTTP wrapper for siege-web's post-condition endpoints.

:func:`~mom_bot.post_conditions.commands.register`
    Attaches all three slash commands to a discord.py command tree.
"""
