"""Decat — ask the operator's own Mac, from Discord.

One folder for a feature that spans two machines:

* ``protocol.py``, ``config.py``, ``copy.py`` — the shared facts, stdlib only.
* ``relay.py`` — the Space's half: a queue that stores nothing.
* ``agent.py``, ``ollama.py``, ``net.py``, ``prompts.py``, ``tools/``,
  ``control.py``, ``doctor.py`` — the laptop's half: the model, the web, the
  switch and the checks.
* ``cogs/decat.py`` (in the bot's own tree) is the only Discord-facing file.

Nothing in this package imports ``discord``. The laptop's half must run on a
machine that has never heard of this bot, and the shared half must be testable
with no gateway, no token and no network.
"""
