"""Role-targeted interactive question command (/questions) with VIP animated cards and automated resolution.

Presents a question targeted at a specific role, collects answers via Discord
Modals, updates answers on resubmission, and automatically summarizes respondents
and non-respondents when time expires or all role members answer.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
import discord
from discord import app_commands
from discord.ext import commands

from utils.gifs import NOTO_GIFS
from utils.style import (
    Panel,
    ACCENT,
    edit_to_panel,
    is_vip_context,
    send_panel,
)

logger = logging.getLogger(__name__)

# Animated GIF assets for rich VIP visual presentation
ANIMATED_ARROW_GIF = NOTO_GIFS.get(
    "point_down", "https://fonts.gstatic.com/s/e/notoemoji/latest/1f447/512.gif"
)

# Duration parsing regex and unit factors
_DURATION_RE = re.compile(r"(\d+)\s*([a-zA-Z]+)?")
_UNIT_MULTIPLIERS: dict[str, int] = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
}


def parse_duration(text: str) -> int:
    """Parse duration string (e.g. '10m', '1h', '24h') into total seconds.

    Raises ValueError on invalid format, zero, or durations exceeding 7 days.
    """
    raw = (text or "").strip().lower()
    if not raw:
        raise ValueError("Duration cannot be empty.")

    if not re.fullmatch(r"(?:\d+\s*[a-zA-Z]*\s*)+", raw):
        raise ValueError(f"Could not parse duration '{text}'. Use e.g. 10m, 1h, 24h.")

    matches = list(_DURATION_RE.finditer(raw))
    if not matches:
        raise ValueError(f"Could not parse duration '{text}'. Use e.g. 10m, 1h, 24h.")

    total_seconds = 0
    for match in matches:
        count = int(match.group(1))
        unit = match.group(2)
        if unit:
            unit = unit.rstrip(".")
            multiplier = _UNIT_MULTIPLIERS.get(unit)
            if multiplier is None:
                raise ValueError(f"Unknown time unit '{unit}'. Use s, m, h, d, w.")
            total_seconds += count * multiplier
        else:
            total_seconds += count * 60  # bare number defaults to minutes

    if total_seconds <= 0:
        raise ValueError("Duration must be greater than zero.")
    if total_seconds > 7 * 86400:
        raise ValueError("Duration cannot exceed 7 days.")

    return total_seconds


def format_duration(seconds: int) -> str:
    """Format seconds into concise human-readable string like '10m', '1h', '24h'."""
    seconds = int(seconds)
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def check_questions_access(interaction: discord.Interaction) -> bool:
    """Verify authorization to run /questions.

    Permitted for:
    - VIP/Premium bot context (is_vip_context)
    - Server managers / administrators (manage_guild or administrator permission)
    """
    if is_vip_context(interaction):
        return True

    user = getattr(interaction, "user", None)
    perms = getattr(user, "guild_permissions", None) if user else None
    if perms is None:
        perms = getattr(interaction, "permissions", None)

    if perms and (
        getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False)
    ):
        return True

    raise app_commands.CheckFailure(
        "You must be a server manager/admin or in a VIP context to use /questions."
    )


def is_default_role(role: discord.Role | None) -> bool:
    """Check if the role is the guild default (@everyone) role."""
    if not role:
        return False
    if getattr(role, "name", None) == "@everyone":
        return True
    guild = getattr(role, "guild", None)
    if guild and hasattr(guild, "id") and getattr(role, "id", None) == guild.id:
        return True
    return False


def resolve_interaction_member(interaction: discord.Interaction) -> discord.Member | discord.User:
    """Resolve member object with roles from interaction."""
    user = getattr(interaction, "user", None)
    if user and getattr(user, "roles", None):
        return user
    member = getattr(interaction, "member", None)
    if member and getattr(member, "roles", None):
        return member
    guild = getattr(interaction, "guild", None)
    if user and guild and hasattr(guild, "get_member"):
        guild_member = guild.get_member(getattr(user, "id", 0))
        if guild_member:
            return guild_member
    return user or member


def member_has_role(member: discord.Member | discord.User, role: discord.Role) -> bool:
    """Verify whether a member holds the specified role or if the role is @everyone."""
    if is_default_role(role):
        return True
    member_roles = getattr(member, "roles", [])
    role_id = getattr(role, "id", None)
    return any(getattr(r, "id", None) == role_id for r in member_roles)


class QuestionSession:
    """Active question session state and answer aggregator."""

    def __init__(
        self,
        session_id: str,
        guild_id: int,
        channel_id: int,
        question_text: str,
        role: discord.Role,
        duration_seconds: int,
        expires_at: float,
        created_by: discord.Member | discord.User,
        message: discord.Message | None = None,
        cog: Questions | None = None,
        channel: discord.abc.Messageable | None = None,
    ):
        self.session_id = session_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.question_text = question_text
        self.role = role
        self.duration_seconds = duration_seconds
        self.expires_at = expires_at
        self.created_by = created_by
        self.message = message
        self.cog = cog
        self.channel = channel
        # user_id -> {"member": Member, "answer": str, "timestamp": float}
        self.answers: dict[int, dict] = {}
        self.resolved: bool = False
        self.resolution_lock = asyncio.Lock()
        self.task: asyncio.Task | None = None

    def record_answer(self, member: discord.Member | discord.User, answer: str) -> bool:
        """Record or update a member's response.

        Returns True if updating an existing answer, False if new submission.
        """
        if self.resolved:
            return False
        is_update = member.id in self.answers
        self.answers[member.id] = {
            "member": member,
            "answer": (answer or "").strip(),
            "timestamp": time.time(),
        }
        return is_update

    def get_answer(self, user_id: int) -> str:
        """Get member's previously submitted answer, or empty string."""
        return self.answers.get(user_id, {}).get("answer", "")

    def get_role_members(self) -> list[discord.Member]:
        """Get human members currently holding the target role."""
        if is_default_role(self.role):
            guild = getattr(self.role, "guild", None)
            members = getattr(guild, "members", []) if guild else getattr(self.role, "members", [])
        else:
            members = getattr(self.role, "members", [])
        return [m for m in members if not getattr(m, "bot", False)]

    def calculate_results(self) -> tuple[list[dict], list[discord.Member]]:
        """Calculate respondent list and non-respondents holding the target role."""
        role_members = self.get_role_members()
        answered_ids = set(self.answers.keys())

        respondents = list(self.answers.values())
        respondents.sort(key=lambda item: item["timestamp"])

        non_respondents = [m for m in role_members if m.id not in answered_ids]
        return respondents, non_respondents

    def all_members_answered(self) -> bool:
        """Whether every member currently holding the target role has responded."""
        role_members = self.get_role_members()
        if not role_members:
            return False
        return all(m.id in self.answers for m in role_members)


class QuestionSubmitButton(discord.ui.Button):
    """Interactive button to submit an answer to the question card."""

    def __init__(self, session: QuestionSession):
        super().__init__(
            label="Submit Answer",
            style=discord.ButtonStyle.primary,
            emoji="✍️",
            custom_id=f"questions_submit:{session.session_id}",
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction):
        if self.session.cog:
            await self.session.cog.handle_submit_button(interaction, self.session)


class QuestionAnswerModal(discord.ui.Modal):
    """Discord modal for submitting / updating text responses."""

    def __init__(self, session: QuestionSession, existing_answer: str = ""):
        super().__init__(title=f"Question: {session.role.name[:30]}")
        self.session = session

        self.answer_input = discord.ui.TextInput(
            label="Your Answer",
            style=discord.TextStyle.paragraph,
            placeholder="Type your response here...",
            default=existing_answer if existing_answer else None,
            required=True,
            max_length=1000,
        )
        self.add_item(self.answer_input)

    async def on_submit(self, interaction: discord.Interaction):
        # Verify member currently holds the target role
        member = resolve_interaction_member(interaction)
        if not member_has_role(member, self.session.role):
            msg = f"❌ Only members with the {self.session.role.mention} role can submit an answer to this question."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return

        answer_text = (self.answer_input.value or "").strip()
        if not answer_text:
            msg = "❌ Answer cannot be blank."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return

        should_resolve_early = False
        async with self.session.resolution_lock:
            if self.session.resolved:
                msg = "⚠️ Time expired or question closed while you were submitting."
                if not interaction.response.is_done():
                    await interaction.response.send_message(msg, ephemeral=True)
                else:
                    await interaction.followup.send(msg, ephemeral=True)
                return

            is_update = self.session.record_answer(member, answer_text)
            if self.session.all_members_answered():
                should_resolve_early = True

        msg = (
            "✅ Your response has been updated!"
            if is_update
            else "✅ Your response has been recorded!"
        )
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)

        # Early completion check: if all role members answered, resolve immediately
        if should_resolve_early and self.session.cog:
            asyncio.create_task(
                self.session.cog.resolve_session(self.session, reason="all_answered")
            )


class Questions(commands.Cog):
    """Interactive role questions with VIP animated cards and automated resolution."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_sessions: dict[str, QuestionSession] = {}

    def cog_unload(self):
        for session in list(self.active_sessions.values()):
            session.resolved = True
            if session.task and not session.task.done():
                session.task.cancel()
        self.active_sessions.clear()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.guild_id
            and self.bot is not None
            and hasattr(self.bot, "is_module_enabled")
        ):
            if not self.bot.is_module_enabled(interaction.guild_id, "questions"):
                await interaction.response.send_message(
                    "⚠️ The **Role Questions** system is currently **turned off** on this server.\n"
                    "An administrator can re-enable it in the [Web Dashboard](https://decat.pages.dev/dashboard).",
                    ephemeral=True,
                )
                return False
        return True

    def build_question_card(self, session: QuestionSession) -> Panel:
        """Render active question message with VIP styling, animated GIFs and countdown."""
        time_ts = int(session.expires_at)
        dur_label = format_duration(session.duration_seconds)

        if session.resolved:
            status_text = "🔒 **Submissions are closed.**"
            button_label = "Submissions Closed"
            button_emoji = "🔒"
            footer_text = f"DaCat • ID: {session.session_id} • Concluded"
            sub_count_text = f"`{len(session.answers)}` recorded (Closed)"
        else:
            status_text = "👇 **Role members: click below to submit or update your answer!**"
            button_label = "Submit Answer"
            button_emoji = "✍️"
            footer_text = f"DaCat • ID: {session.session_id} • Closes <t:{time_ts}:R>"
            sub_count_text = f"`{len(session.answers)}` recorded"

        q_lines = session.question_text.splitlines()
        q_display = "\n> ".join(q_lines) if q_lines else session.question_text
        body = (
            f"> **\"{q_display}\"**\n\n"
            f"🎯 **Target Role:** {session.role.mention}\n"
            f"⏳ **Expires:** <t:{time_ts}:R> (<t:{time_ts}:F>)\n"
            f"⏱️ **Duration:** `{dur_label}`\n\n"
            f"{status_text}"
        )

        role_name = getattr(session.role, "name", "Role")[:100]
        panel = Panel(
            title=f"Question for @{role_name}",
            body=body,
            accent=ACCENT,
            gallery=[ANIMATED_ARROW_GIF],
            fields=[
                ("Target Role", session.role.mention),
                ("Closes", f"<t:{time_ts}:R>"),
                ("Submissions", sub_count_text),
            ],
            footer=footer_text,
            timeout=None,
        )

        if session.resolved:
            panel.add_row(
                discord.ui.Button(
                    label=button_label,
                    style=discord.ButtonStyle.secondary,
                    emoji=button_emoji,
                    disabled=True,
                )
            )
        else:
            submit_btn = QuestionSubmitButton(session)
            panel.add_row(submit_btn)
        return panel

    def build_resolution_panel(
        self,
        session: QuestionSession,
        respondents: list[dict],
        non_respondents: list[discord.Member],
        reason: str = "expired",
    ) -> Panel:
        """Render resolution panel listing answers and non-respondents."""
        total_count = len(respondents) + len(non_respondents)
        percent = int((len(respondents) / total_count * 100)) if total_count > 0 else 0
        if total_count == 0:
            status_line = "⌛ **Time expired — no role members found.**"
        elif reason == "all_answered" or (total_count > 0 and len(non_respondents) == 0):
            status_line = "🎉 **All role members responded before time expired!**"
        else:
            status_line = "⌛ **Time expired — question concluded.**"

        q_lines = session.question_text.splitlines()
        q_display = "\n> ".join(q_lines) if q_lines else session.question_text
        body = (
            f"> **\"{q_display}\"**\n\n"
            f"🎯 **Target Role:** {session.role.mention}\n"
            f"{status_line}\n"
            f"📊 **Participation:** `{len(respondents)} / {total_count}` ({percent}% answered)"
        )

        if respondents:
            resp_lines = []
            truncated_count = 0
            current_len = 0
            for i, item in enumerate(respondents):
                m = item["member"]
                ans = " ".join(item["answer"].split())
                display = ans[:150] + ("..." if len(ans) > 150 else "")
                line = f"• {m.mention} (`{getattr(m, 'name', 'unknown')}`):\n  ↳ *\"{display}\"*"
                if current_len + len(line) + 50 > 850:
                    truncated_count = len(respondents) - i
                    break
                resp_lines.append(line)
                current_len += len(line) + 1
            if truncated_count > 0:
                resp_lines.append(f"*... and {truncated_count} more response(s)*")
            resp_text = "\n".join(resp_lines)
        else:
            resp_text = "*No answers were submitted.*"

        if non_respondents:
            nr_lines = []
            truncated_nr = 0
            current_len = 0
            for i, m in enumerate(non_respondents):
                line = f"• {m.mention} (`{getattr(m, 'name', 'unknown')}`)"
                if current_len + len(line) + 50 > 850:
                    truncated_nr = len(non_respondents) - i
                    break
                nr_lines.append(line)
                current_len += len(line) + 1
            if truncated_nr > 0:
                nr_lines.append(f"*... and {truncated_nr} more non-respondent(s)*")
            nr_text = "\n".join(nr_lines)
        elif not respondents and total_count == 0:
            nr_text = "*No members currently hold this role.*"
        else:
            nr_text = "🎉 *All role members submitted an answer!*"

        fields = [
            (f"Submitted Answers ({len(respondents)})", resp_text),
            (f"Non-Respondents ({len(non_respondents)})", nr_text),
        ]

        role_name = getattr(session.role, "name", "Role")[:100]
        panel = Panel(
            title=f"Question Results • @{role_name}",
            body=body,
            accent=ACCENT,
            fields=fields,
            footer=f"DaCat • Question ID: {session.session_id} • Total Role Members: {total_count}",
            timeout=None,
        )
        return panel

    def _prune_sessions(self):
        """Prune old resolved sessions to keep memory usage bounded."""
        if len(self.active_sessions) > 100:
            now_ts = time.time()
            to_remove = [
                sid
                for sid, s in self.active_sessions.items()
                if s.resolved and (now_ts - s.expires_at > 86400)
            ]
            for sid in to_remove:
                self.active_sessions.pop(sid, None)

    async def handle_submit_button(
        self, interaction: discord.Interaction, session: QuestionSession
    ):
        """Validate member role and present modal input or ephemeral refusal."""
        if interaction.response.is_done():
            return

        if session.resolved:
            try:
                await interaction.response.send_message(
                    "⚠️ This question is closed. Submissions are no longer accepted.",
                    ephemeral=True,
                )
            except (discord.InteractionResponded, discord.HTTPException):
                pass
            return

        member = resolve_interaction_member(interaction)
        if not member_has_role(member, session.role):
            try:
                await interaction.response.send_message(
                    f"❌ Only members with the {session.role.mention} role can submit an answer to this question.",
                    ephemeral=True,
                )
            except (discord.InteractionResponded, discord.HTTPException):
                pass
            return

        existing_answer = session.get_answer(member.id)
        modal = QuestionAnswerModal(session=session, existing_answer=existing_answer)
        try:
            await interaction.response.send_modal(modal)
        except (discord.InteractionResponded, discord.HTTPException):
            pass

    async def _session_timer(self, session: QuestionSession):
        """Countdown timer task that triggers resolution on expiration."""
        try:
            now = time.time()
            remaining = max(0.0, session.expires_at - now)
            if remaining > 0:
                await asyncio.sleep(remaining)
            await self.resolve_session(session, reason="expired")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in question session timer: {e}")

    async def resolve_session(
        self, session: QuestionSession, reason: str = "expired"
    ):
        """Resolve question session, disable submit button, and post summary panel."""
        async with session.resolution_lock:
            if session.resolved:
                return
            session.resolved = True

        if session.task and not session.task.done() and session.task != asyncio.current_task():
            session.task.cancel()

        respondents, non_respondents = session.calculate_results()

        # Update question prompt card to closed state with disabled button
        if session.message:
            try:
                closed_card = self.build_question_card(session)
                await edit_to_panel(session.message, closed_card)
            except Exception as e:
                logger.debug(f"Could not edit question prompt: {e}")

        # Post resolution panel into target channel
        resolution_panel = self.build_resolution_panel(
            session, respondents, non_respondents, reason=reason
        )

        target_channel = (
            session.message.channel
            if session.message and hasattr(session.message, "channel")
            else (
                session.channel
                if session.channel
                else (
                    self.bot.get_channel(session.channel_id)
                    if self.bot and hasattr(self.bot, "get_channel")
                    else None
                )
            )
        )

        if target_channel is None and self.bot and hasattr(self.bot, "fetch_channel"):
            try:
                target_channel = await self.bot.fetch_channel(session.channel_id)
            except Exception:
                pass

        if target_channel:
            try:
                await send_panel(target_channel, resolution_panel)
            except Exception:
                try:
                    await target_channel.send(view=resolution_panel)
                except Exception as e:
                    logger.error(f"Failed to post resolution panel: {e}")

        self._prune_sessions()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Dispatches button clicks for question cards by custom_id."""
        if interaction.type != discord.InteractionType.component:
            return
        data = getattr(interaction, "data", None) or {}
        custom_id = data.get("custom_id", "")
        if not custom_id.startswith("questions_submit:"):
            return
        if interaction.response.is_done():
            return

        session_id = custom_id.split(":", 1)[1]
        session = self.active_sessions.get(session_id)
        if not session:
            try:
                await interaction.response.send_message(
                    "❌ This question is no longer active.",
                    ephemeral=True,
                )
            except (discord.InteractionResponded, discord.HTTPException):
                pass
            return

        try:
            await self.handle_submit_button(interaction, session)
        except (discord.InteractionResponded, discord.HTTPException):
            pass

    @app_commands.command(
        name="questions",
        description="Ask an interactive question targeted at a specific role with automated summary.",
    )
    @app_commands.describe(
        question="The question to ask role members",
        role="The target role whose members should submit answers",
        duration="Expiration duration (e.g. 10m, 1h, 24h)",
    )
    @app_commands.check(check_questions_access)
    async def questions(
        self,
        interaction: discord.Interaction,
        question: str,
        role: discord.Role,
        duration: str = "1h",
    ):
        """Slash command /questions."""
        if not getattr(interaction, "guild", None):
            return await interaction.response.send_message(
                "❌ The `/questions` command can only be used in a server.",
                ephemeral=True,
            )

        question_text = (question or "").strip()
        if not question_text:
            return await interaction.response.send_message(
                "❌ Question text cannot be empty.",
                ephemeral=True,
            )
        if len(question_text) > 500:
            return await interaction.response.send_message(
                "❌ Question text must be under 500 characters.",
                ephemeral=True,
            )

        try:
            duration_seconds = parse_duration(duration)
        except ValueError as exc:
            return await interaction.response.send_message(
                f"❌ {exc}",
                ephemeral=True,
            )

        guild_id = getattr(interaction, "guild_id", 0) or 0
        channel_id = getattr(interaction, "channel_id", 0) or 0
        now = time.time()
        expires_at = now + duration_seconds
        session_id = f"q_{int(now)}_{getattr(role, 'id', 0)}_{uuid.uuid4().hex[:6]}"

        session = QuestionSession(
            session_id=session_id,
            guild_id=guild_id,
            channel_id=channel_id,
            question_text=question_text,
            role=role,
            duration_seconds=duration_seconds,
            expires_at=expires_at,
            created_by=interaction.user,
            channel=getattr(interaction, "channel", None),
            cog=self,
        )
        self.active_sessions[session_id] = session

        panel = self.build_question_card(session)
        try:
            await send_panel(interaction, panel)
        except Exception:
            self.active_sessions.pop(session_id, None)
            raise

        try:
            if hasattr(interaction, "original_response"):
                msg = await interaction.original_response()
                session.message = msg
        except Exception:
            pass

        session.task = asyncio.create_task(self._session_timer(session))


async def setup(bot: commands.Bot):
    await bot.add_cog(Questions(bot))
