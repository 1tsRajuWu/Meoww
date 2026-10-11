"""Basic auto-moderation: spam detection and invite/link filtering.

Two things here are as important as the filters themselves, and both were
missing until a pass over the bot-platform rules asked what happens *after* an
automatic decision:

* **Every automatic action leaves a record.** A deleted invite and a five-minute
  timeout used to live only in a notice that deleted itself after ten seconds, so
  nobody but the member had ever seen it and a moderator asked "did the bot mute
  somebody?" had nothing to read. Each decision now posts a card — the rule that
  fired, who it hit, where, and whether Discord accepted the action — to the same
  log channel a manual case goes to (``utils.modlog``).
* **The member can appeal it.** Automated filters are wrong sometimes, and the
  notice now says so out loud, naming ``/appeal``; the command files a review with
  the humans instead of leaving the member to argue with a bot.

**This cog answers to its own switch** (``MODULE`` below). It was merged into
Moderation & Logs once, and that is exactly why a server that wanted human
moderators but no robot deleting messages had no switch: the only way to stop the
filters was to stop bans, kicks, warnings and logging with them. Every rule is
also this server's to tune — the count, the window and the timeout length are
declared in ``utils/features.py`` and read here, so the dashboard, the slash
command and the listener all ask the registry instead of remembering a literal.
"""
import datetime
import re
import time
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands

from utils import db, features, modlog
from utils.style import INFO, WARN

try:
    from discord.automod import AutoModRuleAction, AutoModTrigger
    from discord.enums import AutoModRuleEventType, AutoModRuleTriggerType
except ImportError:  # pragma: no cover - only old discord.py installations
    AutoModRuleAction = AutoModTrigger = None
    AutoModRuleEventType = AutoModRuleTriggerType = None

INVITE_REGEX = re.compile(r"(discord\.gg|discordapp\.com/invite|discord\.com/invite)/\S+", re.IGNORECASE)
LINK_REGEX = re.compile(r"https?://\S+", re.IGNORECASE)

#: One sentence, in every notice an automatic action leaves behind. A member who
#: disagrees with a bot needs a door, and it has to be named where they are
#: standing — a policy page three clicks away is not one.
APPEAL_POINTER = "If that was wrong, run `/appeal` — a human will read it."

#: The one switch this whole cog answers to. The id is named once, here, because
#: several gates read it (the command gate and every filter branch) and a second
#: copy of it is how the two would come to disagree about which switch turns the
#: filters off.
MODULE = "automod"

# Discord's native rules are deliberately named and owned by this bot.  This
# gives the dashboard an idempotent sync target and makes it safe to leave rules
# created by a server administrator alone.  The native rules complement (rather
# than replace) the richer local filter: Discord can block a message before it
# reaches the gateway, while the local listener still handles allowlists and
# detailed case cards.
NATIVE_INVITE_RULE = "DaCat AutoMod · invite links"
NATIVE_SPAM_RULE = "DaCat AutoMod · spam"
NATIVE_RULE_NAMES = frozenset({NATIVE_INVITE_RULE, NATIVE_SPAM_RULE})
NATIVE_INVITE_KEYWORDS = ("discord.gg/", "discordapp.com/invite/", "discord.com/invite/")


def _owns_native_rule(bot, rule) -> bool:
    """Names are a label, not authority: only edit rules created by this bot."""
    bot_id = getattr(getattr(bot, "user", None), "id", None)
    creator_id = getattr(rule, "creator_id", None)
    try:
        return bot_id is not None and creator_id is not None and int(bot_id) == int(creator_id)
    except (TypeError, ValueError):
        return False


def native_rule_actions(*, timeout_minutes: int = 0):
    """Build safe native actions without retaining message content.

    Discord requires a block action alongside a timeout action.  The timeout is
    capped again here even though the registry validates it, because this helper
    is also used by migration/repair code and must never send an invalid payload.
    """
    if AutoModRuleAction is None:
        return []
    actions = [AutoModRuleAction()]
    minutes = max(0, min(28 * 24 * 60, int(timeout_minutes or 0)))
    if minutes:
        actions.append(AutoModRuleAction(duration=datetime.timedelta(minutes=minutes)))
    return actions


def native_rule_payloads(settings: dict) -> tuple[dict, ...]:
    """Return the two native rule definitions from the server's settings."""
    if AutoModTrigger is None or AutoModRuleTriggerType is None:
        return ()
    return (
        {
            "name": NATIVE_INVITE_RULE,
            "enabled": bool(settings.get("block_invites")),
            "trigger": AutoModTrigger(keyword_filter=list(NATIVE_INVITE_KEYWORDS)),
            "actions": native_rule_actions(),
        },
        {
            "name": NATIVE_SPAM_RULE,
            "enabled": bool(settings.get("anti_spam")),
            "trigger": AutoModTrigger(type=AutoModRuleTriggerType.spam),
            "actions": native_rule_actions(
                timeout_minutes=settings.get("spam_timeout_minutes", 0)
            ),
        },
    )


def _off_sentence() -> str:
    """The refusal, labelled from the registry rather than by hand."""
    feature = features.get(MODULE)
    label = feature.name if feature else MODULE.replace("_", " ").title()
    return (
        f"⚠️ The **{label}** system is currently **turned off** on this server.\n"
        "An administrator can re-enable it in the [Web Dashboard](https://dacatdashboard.pages.dev/dashboard)."
    )


def settings_specs():
    """Every setting this cog reads, declared beside the system it belongs to."""
    return features.settings_for(MODULE)


async def read_settings(guild_id) -> dict:
    """The rules in force for one guild, from the registry's own declarations.

    Async because it reads the database, and every branch below goes through it
    rather than through a literal, so a threshold changed on the dashboard is a
    threshold the next message is judged by.
    """
    settings = {}
    for spec in settings_specs():
        raw = await db.get_setting(guild_id, features.setting_key(MODULE, spec.id), None)
        settings[spec.id] = features.setting_value(spec, raw)
    return settings


def allowed_hosts(raw) -> tuple[str, ...]:
    """The allowlist, as domains, from however the text was typed.

    Commas, spaces and newlines all separate; a leading ``*.`` and a leading dot
    are accepted because that is how people write "any subdomain of"; and the
    answer is lowercased because a hostname is case-insensitive and a comparison
    that was not is a filter an attacker walks around with a capital letter.
    """
    text = str(raw or "")
    parts = re.split(r"[,\s;]+", text)
    hosts = []
    for part in parts:
        host = part.strip().lower().lstrip("*").lstrip(".").strip("/")
        if host:
            hosts.append(host)
    return tuple(hosts)


def link_allowed(url: str, hosts) -> bool:
    """Whether one link survives the filter: pure, so the rule is testable.

    A host matches when it is the allowed domain or a subdomain of it — never by
    suffix of the *string*, which is how ``evil-github.com`` walks through a
    naive ``endswith("github.com")`` check.
    """
    try:
        host = (urlsplit(url).hostname or "").lower().strip(".")
    except ValueError:
        return False
    if not host:
        return False
    for allowed in hosts:
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def _audit_embed(
    action: str,
    member: discord.abc.User,
    channel,
    rule: str,
    detail: str,
    outcome: str,
) -> discord.Embed:
    """What a moderator would need to judge an automatic action.

    No message content: the rules live on a count and a pattern, and copying the
    text into a log would keep a copy of something the server never asked to
    keep. The member, the rule, the channel and the outcome are the whole of it.
    """
    embed = discord.Embed(
        title=f"🤖 Auto-mod — {action}",
        color=WARN,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name="DaCat Moderation Log", icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="👤 Member", value=f"{member.mention}\n`{member.name}`", inline=True)
    embed.add_field(name="📍 Channel", value=getattr(channel, "mention", "—"), inline=True)
    embed.add_field(name="📐 Rule", value=rule, inline=False)
    embed.add_field(name="⚙️ Outcome", value=f"{detail}\n{outcome}", inline=False)
    embed.set_footer(text=f"Automatic action • Member ID: {member.id}")
    return embed


def _native_audit_embed(execution, rule_name: str, member, channel) -> discord.Embed:
    """A native-rule execution card that never copies message content."""
    embed = discord.Embed(
        title="🤖 AutoMod — native rule",
        color=WARN,
        timestamp=discord.utils.utcnow(),
    )
    if member is not None:
        embed.add_field(name="👤 Member", value=f"{member.mention}\n`{member.name}`", inline=True)
    else:
        embed.add_field(name="👤 Member", value=f"User ID `{execution.user_id}`", inline=True)
    embed.add_field(name="📍 Channel", value=getattr(channel, "mention", "—"), inline=True)
    embed.add_field(name="📐 Rule", value=rule_name, inline=False)
    embed.add_field(name="⚙️ Outcome", value="Discord's native AutoMod blocked the message.", inline=False)
    embed.set_footer(text="Native Discord AutoMod • Message content is not retained")
    return embed


class AutoMod(commands.Cog):
    """Basic auto-moderation: spam detection and invite/link filtering."""

    automod_group = app_commands.Group(name="automod", description="Automod moderation filters")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings_cache = {}  # {guild_id: {setting_id: value}}
        self.message_log = {}  # {(guild_id, user_id): [timestamps]}

    def _running(self, guild_id) -> bool:
        """Whether this cog's modules are on for a guild.

        The one place the lookup happens. A bot without the module readers (the
        cog tests build it with a stub) answers "running": the gate is about a
        switch the bot has to be able to see, and a bot that cannot see one is not
        evidence that a server turned it off.
        """
        check = getattr(self.bot, "is_module_enabled", None)
        return True if check is None else bool(check(guild_id, MODULE))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and not self._running(interaction.guild_id):
            await interaction.response.send_message(_off_sentence(), ephemeral=True)
            return False
        return True

    async def get_settings(self, guild_id):
        if guild_id not in self.settings_cache:
            self.settings_cache[guild_id] = await read_settings(guild_id)
        return self.settings_cache[guild_id]

    async def update_setting(self, guild_id, key, value):
        await db.set_setting(guild_id, features.setting_key(MODULE, key), value)
        if guild_id in self.settings_cache:
            self.settings_cache[guild_id][key] = value

    async def native_status(self, guild_id) -> dict:
        """Read this bot's native Discord AutoMod rules for one server.

        A missing gateway, missing permission, or older library is reported as an
        unreadable status rather than as an empty rule list.  Empty and unknown
        are different operational answers: the first means the server has no
        managed native rules, the second means Discord was not asked.
        """
        if AutoModTrigger is None:
            return {"read": False, "why": "This deployment's Discord library has no native AutoMod support."}
        guild = getattr(self.bot, "get_guild", lambda _id: None)(int(guild_id))
        if guild is None:
            return {"read": False, "why": "The bot is not connected to this server right now."}
        try:
            rules = await guild.fetch_automod_rules()
        except discord.Forbidden:
            return {"read": False, "why": "Discord refused the AutoMod read. Give the bot Manage Server."}
        except discord.HTTPException as exc:
            print(f"[AUTOMOD] Could not read native rules for {guild_id}: {exc}", flush=True)
            return {"read": False, "why": "Discord did not return the native AutoMod rules."}

        managed = []
        for rule in rules:
            if rule.name not in NATIVE_RULE_NAMES or not _owns_native_rule(self.bot, rule):
                continue
            trigger_type = getattr(getattr(rule, "trigger", None), "type", None)
            managed.append({
                "name": rule.name,
                "enabled": bool(rule.enabled),
                "trigger": getattr(trigger_type, "name", str(trigger_type or "unknown")),
            })
        return {"read": True, "rules": managed, "count": len(managed)}

    async def sync_native_rules(self, guild_id) -> dict:
        """Create/update only the native rules managed by DaCat.

        This is idempotent and is safe to call after every dashboard settings
        save.  Existing rules with other names are never edited or deleted.
        """
        if AutoModTrigger is None or AutoModRuleEventType is None:
            return {"success": False, "error": "Native AutoMod is unavailable in this discord.py version."}
        guild = getattr(self.bot, "get_guild", lambda _id: None)(int(guild_id))
        if guild is None:
            return {"success": False, "error": "The bot is not connected to this server right now."}
        try:
            rules = await guild.fetch_automod_rules()
            existing = {
                rule.name: rule
                for rule in rules
                if rule.name in NATIVE_RULE_NAMES and _owns_native_rule(self.bot, rule)
            }
            settings = await self.get_settings(guild.id)
            module_enabled = self._running(guild.id)
            changed = []
            for payload in native_rule_payloads(settings):
                payload["enabled"] = bool(payload["enabled"] and module_enabled)
                rule = existing.get(payload["name"])
                if rule is None:
                    # Do not create disabled rules on first sync.  An admin who
                    # has not enabled a shield should not grow hidden Discord
                    # configuration just by opening the dashboard.
                    if not payload["enabled"]:
                        continue
                    await guild.create_automod_rule(
                        name=payload["name"],
                        event_type=AutoModRuleEventType.message_send,
                        trigger=payload["trigger"],
                        actions=payload["actions"],
                        enabled=True,
                        reason="DaCat native AutoMod sync",
                    )
                    changed.append(payload["name"])
                    continue

                await rule.edit(
                    enabled=payload["enabled"],
                    actions=payload["actions"],
                    reason="DaCat native AutoMod sync",
                )
                changed.append(payload["name"])
            return {"success": True, "changed": changed, "said": (
                "Discord AutoMod is synced."
                if changed else "Discord AutoMod already matches this server's settings."
            )}
        except discord.Forbidden:
            return {"success": False, "error": "Discord refused the native AutoMod change. Give the bot Manage Server."}
        except discord.HTTPException as exc:
            print(f"[AUTOMOD] Could not sync native rules for {guild_id}: {exc}", flush=True)
            return {"success": False, "error": "Discord rejected the native AutoMod change."}

    @automod_group.command(name="native", description="Sync Discord's native AutoMod rules with this server's settings.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_native(self, interaction: discord.Interaction):
        result = await self.sync_native_rules(interaction.guild.id)
        await interaction.response.send_message(
            result.get("said") or result.get("error") or "Native AutoMod sync finished.",
            ephemeral=True,
        )

    def _invalidate(self, guild_id) -> None:
        """Drop one guild's cache after a write from anywhere else.

        The dashboard writes these settings through its own endpoint, and a
        cached copy in this process would keep judging messages by the old rule
        until a restart — the exact drift the registry exists to prevent.
        """
        self.settings_cache.pop(guild_id, None)

    @automod_group.command(name="invites", description="Toggle blocking of Discord invite links.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_invites(self, interaction: discord.Interaction, enabled: bool):
        await self.update_setting(interaction.guild.id, "block_invites", enabled)
        native = await self.sync_native_rules(interaction.guild.id)
        suffix = " Native Discord AutoMod is synced." if native.get("success") else " Native sync needs attention: " + native.get("error", "unknown error")
        await interaction.response.send_message(
            f"Invite link blocking: **{'ON' if enabled else 'OFF'}**.{suffix}"
        )

    @automod_group.command(name="links", description="Toggle blocking of ordinary http(s) links.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_links(self, interaction: discord.Interaction, enabled: bool):
        await self.update_setting(interaction.guild.id, "block_links", enabled)
        allowed = allowed_hosts((await self.get_settings(interaction.guild.id)).get("link_allowlist"))
        tail = (
            f" Links from {', '.join(f'`{host}`' for host in allowed)} stay allowed."
            if enabled and allowed else
            (" Every link is blocked while this is on — set an allowlist on the dashboard to keep some."
             if enabled else "")
        )
        await interaction.response.send_message(
            f"Link blocking: **{'ON' if enabled else 'OFF'}**.{tail}"
        )

    @automod_group.command(name="antispam", description="Toggle anti-spam (rapid message) detection.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_antispam(self, interaction: discord.Interaction, enabled: bool):
        await self.update_setting(interaction.guild.id, "anti_spam", enabled)
        native = await self.sync_native_rules(interaction.guild.id)
        suffix = " Native Discord AutoMod is synced." if native.get("success") else " Native sync needs attention: " + native.get("error", "unknown error")
        await interaction.response.send_message(
            f"Anti-spam: **{'ON' if enabled else 'OFF'}**.{suffix}"
        )

    @automod_group.command(name="status", description="What the automatic filters are doing here right now.")
    async def automod_status(self, interaction: discord.Interaction):
        settings = await self.get_settings(interaction.guild.id)
        native = await self.native_status(interaction.guild.id)
        embed = discord.Embed(
            title="🤖 Auto Moderation — this server",
            description=(
                "Every automatic action here is written to the moderation log and "
                f"tells the member about `/appeal`.\nThe switch itself is on the "
                "[Web Dashboard](https://dacatdashboard.pages.dev/dashboard/systems)."
            ),
            color=INFO,
        )
        for spec in settings_specs():
            value = settings.get(spec.id)
            if spec.kind == "bool":
                shown = "On" if value else "Off"
            elif spec.id in ("spam_timeout_minutes",) and not value:
                shown = "0 — the flood is logged, nobody is timed out"
            else:
                shown = str(value) if str(value) else "—"
            embed.add_field(name=spec.label, value=shown, inline=True)
        if native.get("read"):
            rules = native.get("rules", [])
            embed.add_field(
                name="Discord native AutoMod",
                value=(
                    "\n".join(
                        f"{'On' if rule['enabled'] else 'Off'} · {rule['name']}"
                        for rule in rules
                    )
                    if rules else "No DaCat native rules are managed yet."
                ),
                inline=False,
            )
        else:
            embed.add_field(name="Discord native AutoMod", value=f"Not read: {native.get('why', 'unknown')}", inline=False)
        gate = features.get(MODULE)
        embed.set_footer(
            text=(
                f"{(gate.name if gate else MODULE)} is "
                f"{'on' if self._running(interaction.guild_id) else 'off'} in this server"
            )
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_automod_action(self, execution):
        """Record native Discord AutoMod actions in the same moderation log."""
        guild = getattr(execution, "guild", None)
        if guild is None or not self._running(guild.id):
            return
        try:
            rule = await execution.fetch_rule()
            rule_name = rule.name
        except (discord.Forbidden, discord.HTTPException):
            rule_name = f"native rule {execution.rule_id}"
        member = getattr(execution, "member", None)
        channel = getattr(execution, "channel", None)
        await modlog.post(
            guild,
            _native_audit_embed(execution, rule_name, member, channel),
            what=f"the native automod action for user {execution.user_id}",
        )

    def _spam_rule(self, settings) -> str:
        """The flood rule in the server's own numbers, for the log card."""
        count = settings.get("spam_messages", 6)
        window = settings.get("spam_window_seconds", 5)
        return (
            f"{count} or more messages in {window} second"
            f"{'' if window == 1 else 's'} (`/automod antispam`)."
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if message.author.guild_permissions.manage_messages:
            return  # don't automod staff

        if not self._running(message.guild.id):
            return

        settings = await self.get_settings(message.guild.id)

        # Invite link filter
        if settings["block_invites"] and INVITE_REGEX.search(message.content):
            # Deleting can fail (missing Manage Messages in this channel, message
            # already gone). An unhandled raise here aborted the listener, so the
            # notice and the rest of automod never ran.
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
            try:
                await message.channel.send(
                    f"{message.author.mention}, invite links aren't allowed here. "
                    f"{APPEAL_POINTER}",
                    delete_after=15,
                )
            except discord.Forbidden:
                pass
            await modlog.post(
                message.guild,
                _audit_embed(
                    "Invite deleted",
                    message.author,
                    message.channel,
                    rule="Invite links are blocked in this server (`/automod invites`).",
                    detail="The message was deleted; a notice naming `/appeal` was posted.",
                    outcome="No timeout, no warning — the message was the whole of it.",
                ),
                what=f"the automod invite deletion for member {message.author.id}",
            )
            return

        # Link filter — the shield the old card advertised before any code
        # implemented it. An allowlist is the difference between "no links" and
        # "no links except the ones this server uses", and it is what makes the
        # switch usable in a server that shares its own domain.
        if settings["block_links"]:
            hosts = allowed_hosts(settings.get("link_allowlist"))
            offenders = [
                link for link in LINK_REGEX.findall(message.content)
                if not link_allowed(link, hosts)
            ]
            if offenders:
                try:
                    await message.delete()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass
                try:
                    await message.channel.send(
                        f"{message.author.mention}, links aren't allowed here. "
                        f"{APPEAL_POINTER}",
                        delete_after=15,
                    )
                except discord.Forbidden:
                    pass
                allowed_note = (
                    f"Links to {', '.join(f'`{host}`' for host in hosts)} stay allowed."
                    if hosts else
                    "No domains are allowed, so every link is blocked."
                )
                await modlog.post(
                    message.guild,
                    _audit_embed(
                        "Link deleted",
                        message.author,
                        message.channel,
                        rule=f"Links are blocked in this server (`/automod links`). {allowed_note}",
                        detail="The message was deleted; a notice naming `/appeal` was posted.",
                        outcome="No timeout, no warning — the message was the whole of it.",
                    ),
                    what=f"the automod link deletion for member {message.author.id}",
                )
                return

        # Basic anti-spam: N messages in W seconds, both read from the registry
        # so a threshold set on the dashboard is the threshold this branch uses.
        if settings["anti_spam"]:
            window = max(1, int(settings.get("spam_window_seconds", 5) or 5))
            trigger = max(2, int(settings.get("spam_messages", 6) or 6))
            timeout_minutes = max(0, int(settings.get("spam_timeout_minutes", 5) or 0))
            key = (message.guild.id, message.author.id)
            now = time.time()
            timestamps = [t for t in self.message_log.get(key, []) if now - t < window]
            timestamps.append(now)
            self.message_log[key] = timestamps

            # Prune users who have gone quiet, otherwise this dict grows forever
            # (one entry per member who ever sent a message, held in RAM).
            if len(self.message_log) > 5000:
                for stale_key, stamps in list(self.message_log.items()):
                    if not stamps or now - stamps[-1] > 60:
                        del self.message_log[stale_key]
                # Still over the cap (e.g. a burst from thousands of distinct
                # accounts): drop the least-recently-seen trackers until we're back
                # under it, so the dict has a hard ceiling.
                if len(self.message_log) > 5000:
                    over = len(self.message_log) - 5000
                    oldest = sorted(self.message_log.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)
                    for old_key, _ in oldest[:over]:
                        del self.message_log[old_key]

            if len(timestamps) >= trigger:
                rule = self._spam_rule(settings)
                if timeout_minutes == 0:
                    # The length is the server's null: the flood is recorded and
                    # the member keeps their voice. Saying so on the card is the
                    # difference between a deliberate setting and a broken filter.
                    await modlog.post(
                        message.guild,
                        _audit_embed(
                            "Flood recorded",
                            message.author,
                            message.channel,
                            rule=rule,
                            detail=(
                                "Nothing was done to the member: this server's timeout "
                                "length is 0, so the record is the whole of it."
                            ),
                            outcome="No timeout — `/automod status` shows the length as 0.",
                        ),
                        what=f"the automod flood record for member {message.author.id}",
                    )
                    self.message_log[key] = []
                    return
                outcome = "Discord accepted the timeout."
                try:
                    await message.author.timeout(discord.utils.utcnow() + datetime.timedelta(minutes=timeout_minutes),
                                                   reason="Auto-mod: spamming")
                    await message.channel.send(
                        f"{message.author.mention} was muted for {timeout_minutes} "
                        f"minute{'' if timeout_minutes == 1 else 's'} for spamming. "
                        f"{APPEAL_POINTER}",
                        delete_after=15,
                    )
                except discord.Forbidden:
                    outcome = (
                        "Discord refused it (missing permission, or this member's role "
                        "is above mine) — they were **not** muted. Fix that in Server "
                        "Settings → Roles, or the filter cannot act on them."
                    )
                except discord.HTTPException as e:
                    # e.g. the target holds a role above the bot; don't let one
                    # unmutable spammer take the listener down every message.
                    print(f"[AUTOMOD] Could not time out {message.author}: {e}", flush=True)
                    outcome = f"Discord rejected it ({type(e).__name__}) — they were **not** muted."
                await modlog.post(
                    message.guild,
                    _audit_embed(
                        "Member timed out",
                        message.author,
                        message.channel,
                        rule=rule,
                        detail="A timeout was attempted after rapid messages.",
                        outcome=outcome,
                    ),
                    what=f"the automod timeout for member {message.author.id}",
                )
                self.message_log[key] = []

async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
