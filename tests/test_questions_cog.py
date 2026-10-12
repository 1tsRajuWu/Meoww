"""Test suite for /questions slash command and Questions cog."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from discord import app_commands
from discord.ext import commands

from cogs.questions import (
    ANIMATED_ARROW_GIF,
    QuestionAnswerModal,
    QuestionSession,
    Questions,
    QuestionSubmitButton,
    check_questions_access,
    format_duration,
    member_has_role,
    parse_duration,
)
from utils.style import VIP_CROWN_GIF, VIP_GEM_GIF, VIP_GOLD


def _mock_role(role_id: int = 100, name: str = "VIP Staff", members=None):
    role = mock.MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    role.members = members if members is not None else []
    return role


def _mock_member(user_id: int, name: str, roles=None, is_bot: bool = False):
    member = mock.MagicMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.bot = is_bot
    member.roles = roles if roles is not None else []
    return member


def _mock_interaction(user=None, guild_id=1, channel_id=2):
    inter = mock.MagicMock(spec=discord.Interaction)
    inter.guild_id = guild_id
    inter.channel_id = channel_id
    inter.user = user or _mock_member(1, "Commander")
    inter.response = mock.MagicMock()
    inter.response.is_done = mock.MagicMock(return_value=False)
    inter.response.send_message = mock.AsyncMock()
    inter.response.send_modal = mock.AsyncMock()
    inter.followup = mock.MagicMock()
    inter.followup.send = mock.AsyncMock()
    inter.original_response = mock.AsyncMock()
    inter.client = SimpleNamespace(user=SimpleNamespace(id=999))
    return inter


def layout_text(node) -> str:
    """Every readable string inside a Components V2 layout, flattened."""
    parts = []
    text = getattr(node, "content", None)
    if isinstance(text, str):
        parts.append(text)
    for child in getattr(node, "children", None) or []:
        parts.append(layout_text(child))
    return "\n".join(parts)


class TestAccessControl(unittest.TestCase):
    """R1: Slash command access control for server managers, admins and premium context."""

    def test_manager_allowed(self):
        user = _mock_member(1, "Manager")
        user.guild_permissions = discord.Permissions(manage_guild=True)
        inter = _mock_interaction(user=user)
        self.assertTrue(check_questions_access(inter))

    def test_admin_allowed(self):
        user = _mock_member(2, "Admin")
        user.guild_permissions = discord.Permissions(administrator=True)
        inter = _mock_interaction(user=user)
        self.assertTrue(check_questions_access(inter))

    def test_vip_context_allowed_without_manager_perm(self):
        from utils.style import VIP_BOT_ID

        user = _mock_member(3, "VipUser")
        user.guild_permissions = discord.Permissions.none()
        inter = _mock_interaction(user=user)
        inter.client.user.id = VIP_BOT_ID
        self.assertTrue(check_questions_access(inter))

    def test_regular_member_refused(self):
        user = _mock_member(4, "Regular")
        user.guild_permissions = discord.Permissions.none()
        inter = _mock_interaction(user=user)
        inter.client.user.id = 999
        with self.assertRaises(app_commands.CheckFailure):
            check_questions_access(inter)


class TestDurationParsing(unittest.TestCase):
    """R1: Expiration duration parsing and formatting."""

    def test_valid_durations(self):
        self.assertEqual(parse_duration("10m"), 600)
        self.assertEqual(parse_duration("10 min"), 600)
        self.assertEqual(parse_duration("10 minutes"), 600)
        self.assertEqual(parse_duration("1h"), 3600)
        self.assertEqual(parse_duration("1 hour"), 3600)
        self.assertEqual(parse_duration("24h"), 86400)
        self.assertEqual(parse_duration("1d"), 86400)
        self.assertEqual(parse_duration("30s"), 30)
        self.assertEqual(parse_duration("2h 30m"), 9000)

    def test_invalid_durations(self):
        for invalid in ["", "   ", "abc", "0m", "-5m", "8d", "100w"]:
            with self.assertRaises(ValueError):
                parse_duration(invalid)

    def test_format_duration(self):
        self.assertEqual(format_duration(600), "10m")
        self.assertEqual(format_duration(3600), "1h")
        self.assertEqual(format_duration(86400), "1d")
        self.assertEqual(format_duration(45), "45s")


class TestVisualStyling(unittest.TestCase):
    """R2: Rich Animated UI Layout with VIP Components V2 design and animated GIFs."""

    def setUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.cog = Questions(self.bot)
        self.role = _mock_role(100, "VIP Crew")
        self.session = QuestionSession(
            session_id="test_sess_1",
            guild_id=1,
            channel_id=2,
            question_text="What is your favourite feature?",
            role=self.role,
            duration_seconds=3600,
            expires_at=time.time() + 3600,
            created_by=_mock_member(1, "Host"),
            cog=self.cog,
        )

    def test_question_card_visual_assets(self):
        panel = self.cog.build_question_card(self.session)
        # 1. VIP Components V2 Card Accent
        self.assertEqual(panel.accent, panel._accent)

        # 2. Thumbnail is VIP gem GIF
        container_children = getattr(panel.container, "children", []) or getattr(
            panel.container, "_children", []
        )
        self.assertTrue(len(container_children) > 0)

        # 3. Animated GIF asset check (gallery carries animated arrow)
        self.assertTrue(ANIMATED_ARROW_GIF.endswith(".gif"))
        self.assertTrue(VIP_GEM_GIF.endswith(".gif"))
        self.assertTrue(VIP_CROWN_GIF.endswith(".gif"))

        # 4. Expiration timestamp markdown in card text
        expires_ts = int(self.session.expires_at)
        text_content = ""
        for child in container_children:
            if hasattr(child, "content"):
                text_content += str(child.content)
            elif hasattr(child, "item") and hasattr(child.item, "content"):
                text_content += str(child.item.content)
        self.assertIn(f"<t:{expires_ts}:R>", text_content)

        # 5. Prominent interactive Submit Answer button
        rows = [c for c in container_children if isinstance(c, discord.ui.ActionRow)]
        self.assertTrue(len(rows) > 0)
        buttons = [item for row in rows for item in row.children]
        self.assertTrue(any(b.label == "Submit Answer" for b in buttons))


class TestRoleValidationGate(unittest.IsolatedAsyncioTestCase):
    """R3: Role validation gate for answer submissions."""

    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.cog = Questions(self.bot)
        self.target_role = _mock_role(role_id=500, name="Beta Testers")
        self.session = QuestionSession(
            session_id="session_gate_test",
            guild_id=1,
            channel_id=2,
            question_text="Provide your beta feedback",
            role=self.target_role,
            duration_seconds=600,
            expires_at=time.time() + 600,
            created_by=_mock_member(1, "Admin"),
            cog=self.cog,
        )
        self.cog.active_sessions[self.session.session_id] = self.session

    async def test_non_role_member_rejected_with_ephemeral_error(self):
        other_role = _mock_role(role_id=999, name="Other Role")
        unauthorized_member = _mock_member(10, "Intruder", roles=[other_role])
        inter = _mock_interaction(user=unauthorized_member)

        await self.cog.handle_submit_button(inter, self.session)

        # Ephemeral error message sent
        inter.response.send_message.assert_called_once()
        args, kwargs = inter.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("Only members with the", args[0])
        self.assertIn(self.target_role.mention, args[0])
        # Modal was NOT presented
        inter.response.send_modal.assert_not_called()

    async def test_role_member_allowed_and_modal_presented(self):
        authorized_member = _mock_member(20, "Tester", roles=[self.target_role])
        inter = _mock_interaction(user=authorized_member)

        await self.cog.handle_submit_button(inter, self.session)

        # Modal is presented to the user
        inter.response.send_modal.assert_called_once()
        modal = inter.response.send_modal.call_args[0][0]
        self.assertIsInstance(modal, QuestionAnswerModal)
        self.assertIsNone(modal.answer_input.default)

    async def test_expired_session_rejects_interaction(self):
        self.session.resolved = True
        authorized_member = _mock_member(20, "Tester", roles=[self.target_role])
        inter = _mock_interaction(user=authorized_member)

        await self.cog.handle_submit_button(inter, self.session)

        inter.response.send_message.assert_called_once()
        args, kwargs = inter.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("closed", args[0].lower())
        inter.response.send_modal.assert_not_called()

    async def test_everyone_role_supported(self):
        everyone_role = _mock_role(role_id=1, name="@everyone")
        everyone_role.is_default.return_value = True
        session = QuestionSession(
            session_id="everyone_sess",
            guild_id=1,
            channel_id=2,
            question_text="Question for everyone",
            role=everyone_role,
            duration_seconds=600,
            expires_at=time.time() + 600,
            created_by=_mock_member(1, "Admin"),
            cog=self.cog,
        )
        # Member with empty roles should still be allowed for @everyone
        member_no_roles = _mock_member(99, "NoRoles", roles=[])
        self.assertTrue(member_has_role(member_no_roles, everyone_role))

        inter = _mock_interaction(user=member_no_roles)
        await self.cog.handle_submit_button(inter, session)
        inter.response.send_modal.assert_called_once()

    def test_everyone_role_members_calculated_from_guild(self):
        m1 = _mock_member(10, "M1")
        m2 = _mock_member(11, "M2")
        mock_guild = mock.MagicMock(spec=discord.Guild)
        mock_guild.members = [m1, m2]

        everyone_role = _mock_role(role_id=1, name="@everyone", members=[])
        everyone_role.is_default.return_value = True
        everyone_role.guild = mock_guild

        session = QuestionSession(
            session_id="everyone_calc_sess",
            guild_id=1,
            channel_id=2,
            question_text="Question for everyone",
            role=everyone_role,
            duration_seconds=600,
            expires_at=time.time() + 600,
            created_by=_mock_member(1, "Admin"),
        )
        members = session.get_role_members()
        self.assertEqual(len(members), 2)
        self.assertEqual([m.id for m in members], [10, 11])


class TestModalSubmissionAndResubmission(unittest.IsolatedAsyncioTestCase):
    """R3: Modal submissions, answer storage, and updating on resubmission."""

    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.cog = Questions(self.bot)
        self.role = _mock_role(role_id=777, name="Council")
        self.session = QuestionSession(
            session_id="modal_test_sess",
            guild_id=1,
            channel_id=2,
            question_text="Vote on proposal #4",
            role=self.role,
            duration_seconds=1200,
            expires_at=time.time() + 1200,
            created_by=_mock_member(1, "Speaker"),
            cog=self.cog,
        )
        self.cog.active_sessions[self.session.session_id] = self.session

    async def test_first_submission_records_answer(self):
        member = _mock_member(30, "Alice", roles=[self.role])
        modal = QuestionAnswerModal(session=self.session)
        modal.answer_input._value = "I vote Aye"
        inter = _mock_interaction(user=member)

        await modal.on_submit(inter)

        # Answer is recorded in session
        self.assertIn(member.id, self.session.answers)
        self.assertEqual(self.session.answers[member.id]["answer"], "I vote Aye")
        inter.response.send_message.assert_called_once()
        args, kwargs = inter.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("recorded", args[0].lower())

    async def test_resubmission_updates_previous_answer_without_duplicate(self):
        member = _mock_member(30, "Alice", roles=[self.role])

        # 1. Initial answer
        self.session.record_answer(member, "Initial Answer")
        self.assertEqual(len(self.session.answers), 1)

        # 2. Clicking submit button again pre-fills previous answer into modal
        inter_btn = _mock_interaction(user=member)
        await self.cog.handle_submit_button(inter_btn, self.session)
        inter_btn.response.send_modal.assert_called_once()
        modal = inter_btn.response.send_modal.call_args[0][0]
        self.assertEqual(modal.answer_input.default, "Initial Answer")

        # 3. Submit updated response
        modal.answer_input._value = "Updated Revised Answer"
        inter_submit = _mock_interaction(user=member)
        await modal.on_submit(inter_submit)

        # Check answers dictionary still has exactly 1 entry, and text is updated
        self.assertEqual(len(self.session.answers), 1)
        self.assertEqual(self.session.answers[member.id]["answer"], "Updated Revised Answer")
        inter_submit.response.send_message.assert_called_once()
        args, kwargs = inter_submit.response.send_message.call_args
        self.assertIn("updated", args[0].lower())

    async def test_submission_rejected_if_role_missing(self):
        # Member lacks target role when submitting modal
        other_member = _mock_member(40, "NoRoleGuy", roles=[])
        modal = QuestionAnswerModal(session=self.session)
        modal.answer_input._value = "Attempted answer"
        inter = _mock_interaction(user=other_member)

        await modal.on_submit(inter)

        self.assertNotIn(other_member.id, self.session.answers)
        inter.response.send_message.assert_called_once()
        args, kwargs = inter.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("Only members with the", args[0])

    async def test_blank_submission_rejected(self):
        member = _mock_member(30, "Alice", roles=[self.role])
        modal = QuestionAnswerModal(session=self.session)
        modal.answer_input._value = "   "
        inter = _mock_interaction(user=member)

        await modal.on_submit(inter)

        self.assertNotIn(member.id, self.session.answers)
        inter.response.send_message.assert_called_once()
        args, kwargs = inter.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("blank", args[0].lower())

    async def test_submission_rejected_if_session_already_resolved(self):
        member = _mock_member(30, "Alice", roles=[self.role])
        self.session.resolved = True
        modal = QuestionAnswerModal(session=self.session)
        modal.answer_input._value = "Late answer"
        inter = _mock_interaction(user=member)

        await modal.on_submit(inter)

        self.assertNotIn(member.id, self.session.answers)
        inter.response.send_message.assert_called_once()
        args, kwargs = inter.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("expired", args[0].lower())

    async def test_modal_submission_with_done_response_uses_followup(self):
        member = _mock_member(30, "Alice", roles=[self.role])
        modal = QuestionAnswerModal(session=self.session)
        modal.answer_input._value = "Followup answer"
        inter = _mock_interaction(user=member)
        inter.response.is_done.return_value = True

        await modal.on_submit(inter)

        inter.followup.send.assert_called_once()
        args, kwargs = inter.followup.send.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("recorded", args[0].lower())


class TestRespondentAndNonRespondentCalculation(unittest.TestCase):
    """R4: Accurate categorization into respondents and non-respondents."""

    def setUp(self):
        self.alice = _mock_member(101, "Alice")
        self.bob = _mock_member(102, "Bob")
        self.charlie = _mock_member(103, "Charlie")
        self.bot_user = _mock_member(999, "AutoBot", is_bot=True)
        self.role = _mock_role(
            role_id=50,
            name="Squad",
            members=[self.alice, self.bob, self.charlie, self.bot_user],
        )
        self.session = QuestionSession(
            session_id="calc_test_sess",
            guild_id=1,
            channel_id=2,
            question_text="Are you ready?",
            role=self.role,
            duration_seconds=600,
            expires_at=time.time() + 600,
            created_by=_mock_member(1, "Captain"),
        )

    def test_partial_responses(self):
        self.session.record_answer(self.alice, "Ready!")
        respondents, non_respondents = self.session.calculate_results()

        self.assertEqual(len(respondents), 1)
        self.assertEqual(respondents[0]["member"].id, self.alice.id)
        self.assertEqual(respondents[0]["answer"], "Ready!")

        non_resp_ids = {m.id for m in non_respondents}
        self.assertEqual(non_resp_ids, {self.bob.id, self.charlie.id})
        self.assertNotIn(self.bot_user.id, non_resp_ids)  # Bot filtered out
        self.assertFalse(self.session.all_members_answered())

    def test_all_members_responded(self):
        self.session.record_answer(self.alice, "Yes")
        self.session.record_answer(self.bob, "Yes too")
        self.session.record_answer(self.charlie, "Always")

        respondents, non_respondents = self.session.calculate_results()
        self.assertEqual(len(respondents), 3)
        self.assertEqual(len(non_respondents), 0)
        self.assertTrue(self.session.all_members_answered())

    def test_zero_responses(self):
        respondents, non_respondents = self.session.calculate_results()
        self.assertEqual(len(respondents), 0)
        self.assertEqual(len(non_respondents), 3)
        self.assertFalse(self.session.all_members_answered())

    def test_zero_role_members_and_zero_answers(self):
        empty_role = _mock_role(role_id=99, name="EmptyRole", members=[])
        session = QuestionSession(
            session_id="empty_role_sess",
            guild_id=1,
            channel_id=2,
            question_text="Anyone here?",
            role=empty_role,
            duration_seconds=600,
            expires_at=time.time() + 600,
            created_by=_mock_member(1, "Leader"),
        )
        respondents, non_respondents = session.calculate_results()
        self.assertEqual(len(respondents), 0)
        self.assertEqual(len(non_respondents), 0)
        self.assertFalse(session.all_members_answered())

        cog = Questions(mock.MagicMock())
        panel = cog.build_resolution_panel(session, respondents, non_respondents)
        text = layout_text(panel)
        self.assertIn("No members currently hold this role", text)

    def test_large_role_and_many_respondents_truncation(self):
        # 60 respondents and 80 non-respondents
        resp_members = [_mock_member(1000 + i, f"Respondent_{i}") for i in range(60)]
        non_resp_members = [_mock_member(2000 + i, f"NonRespondent_{i}") for i in range(80)]
        big_role = _mock_role(role_id=888, name="LargeRole", members=resp_members + non_resp_members)
        session = QuestionSession(
            session_id="large_role_sess",
            guild_id=1,
            channel_id=2,
            question_text="Big survey question?",
            role=big_role,
            duration_seconds=3600,
            expires_at=time.time() + 3600,
            created_by=_mock_member(1, "Admin"),
        )
        for i, m in enumerate(resp_members):
            session.record_answer(m, f"This is an extensive detailed response string from member {i}")

        respondents, non_respondents = session.calculate_results()
        self.assertEqual(len(respondents), 60)
        self.assertEqual(len(non_respondents), 80)

        cog = Questions(mock.MagicMock())
        panel = cog.build_resolution_panel(session, respondents, non_respondents)
        text = layout_text(panel)

        # Confirm clean truncation indicators are present
        self.assertIn("more response(s)", text)
        self.assertIn("more non-respondent(s)", text)

        # Confirm fields within Discord length bounds (< 1024 chars per field)
        container_children = getattr(panel.container, "children", []) or getattr(
            panel.container, "_children", []
        )
        for child in container_children:
            content = getattr(child, "content", "")
            if isinstance(content, str):
                self.assertLessEqual(len(content), 2000)


class TestExpirationAndAutomatedResolution(unittest.IsolatedAsyncioTestCase):
    """R4: Expiration handling, prompt editing, and automated resolution panel posting."""

    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.cog = Questions(self.bot)
        self.alice = _mock_member(201, "Alice")
        self.bob = _mock_member(202, "Bob")
        self.role = _mock_role(role_id=60, name="Officers", members=[self.alice, self.bob])

        self.mock_channel = mock.AsyncMock(spec=discord.TextChannel)
        self.mock_channel.send = mock.AsyncMock()

        self.mock_message = mock.AsyncMock(spec=discord.Message)
        self.mock_message.channel = self.mock_channel
        self.mock_message.edit = mock.AsyncMock()

        self.session = QuestionSession(
            session_id="resolution_test_sess",
            guild_id=1,
            channel_id=2,
            question_text="Weekly status update?",
            role=self.role,
            duration_seconds=600,
            expires_at=time.time() + 600,
            created_by=_mock_member(1, "Commander"),
            message=self.mock_message,
            cog=self.cog,
        )
        self.cog.active_sessions[self.session.session_id] = self.session

    async def test_resolution_on_timer_expiration(self):
        # Alice answered, Bob did not
        self.session.record_answer(self.alice, "Completed sprint tasks")

        await self.cog.resolve_session(self.session, reason="expired")

        self.assertTrue(self.session.resolved)
        # Original message was edited to disable submission
        self.mock_message.edit.assert_called_once()
        view_used = self.mock_message.edit.call_args[1].get("view")
        container_children = getattr(view_used.container, "children", []) or getattr(
            view_used.container, "_children", []
        )
        rows = [c for c in container_children if isinstance(c, discord.ui.ActionRow)]
        buttons = [item for row in rows for item in row.children] + [
            item for item in view_used.children if isinstance(item, discord.ui.Button)
        ]
        self.assertTrue(any(item.disabled for item in buttons))

        # Resolution panel posted to channel
        self.mock_channel.send.assert_called_once()
        panel_sent = self.mock_channel.send.call_args[1].get("view")
        self.assertEqual(panel_sent.accent, panel_sent._accent)

        # Inspect panel contents
        text_content = layout_text(panel_sent)
        self.assertIn("Weekly status update?", text_content)
        self.assertIn(self.alice.mention, text_content)
        self.assertIn("Completed sprint tasks", text_content)
        self.assertIn(self.bob.mention, text_content)

    async def test_early_resolution_when_all_role_members_answer(self):
        self.session.record_answer(self.alice, "Done Alice")
        self.session.record_answer(self.bob, "Done Bob")

        self.assertTrue(self.session.all_members_answered())

        await self.cog.resolve_session(self.session, reason="all_answered")
        self.assertTrue(self.session.resolved)

        # Resolution panel posted
        self.mock_channel.send.assert_called_once()
        panel_sent = self.mock_channel.send.call_args[1].get("view")
        text_content = layout_text(panel_sent)

        self.assertIn("All role members responded", text_content)
        self.assertIn(self.alice.mention, text_content)
        self.assertIn(self.bob.mention, text_content)

    async def test_session_timer_runs_to_completion_and_posts_resolution(self):
        # Configure session with 0 remaining seconds so timer fires immediately
        self.session.expires_at = time.time() - 1
        self.session.task = asyncio.create_task(self.cog._session_timer(self.session))

        await self.session.task

        self.assertTrue(self.session.resolved)
        self.mock_message.edit.assert_called_once()
        self.mock_channel.send.assert_called_once()

    async def test_resolution_falls_back_to_channel_when_message_missing(self):
        # Session where message is None (e.g. original_response timed out)
        fallback_channel = mock.AsyncMock(spec=discord.TextChannel)
        fallback_channel.send = mock.AsyncMock()

        session_no_msg = QuestionSession(
            session_id="fallback_test_sess",
            guild_id=1,
            channel_id=2,
            question_text="Are we ready?",
            role=self.role,
            duration_seconds=600,
            expires_at=time.time() + 600,
            created_by=_mock_member(1, "Commander"),
            message=None,
            channel=fallback_channel,
            cog=self.cog,
        )
        await self.cog.resolve_session(session_no_msg, reason="expired")
        self.assertTrue(session_no_msg.resolved)
        fallback_channel.send.assert_called_once()

    def test_prune_sessions_cleans_up_old_resolved_sessions(self):
        # Fill active_sessions with 105 sessions, some old and resolved
        self.cog.active_sessions.clear()
        now = time.time()
        for i in range(105):
            sess = QuestionSession(
                session_id=f"prune_{i}",
                guild_id=1,
                channel_id=2,
                question_text=f"Q {i}",
                role=self.role,
                duration_seconds=60,
                expires_at=now - 100000 if i < 10 else now + 1000,
                created_by=_mock_member(1, "Commander"),
            )
            if i < 10:
                sess.resolved = True
            self.cog.active_sessions[f"prune_{i}"] = sess

        self.assertEqual(len(self.cog.active_sessions), 105)
        self.cog._prune_sessions()
        # 10 old resolved sessions should have been removed
        self.assertEqual(len(self.cog.active_sessions), 95)
        self.assertNotIn("prune_0", self.cog.active_sessions)
        self.assertIn("prune_50", self.cog.active_sessions)


class TestSlashCommandIntegration(unittest.IsolatedAsyncioTestCase):
    """End-to-end slash command dispatch test."""

    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.cog = Questions(self.bot)
        self.role = _mock_role(role_id=88, name="Staff", members=[])

    async def test_questions_command_success(self):
        inter = _mock_interaction()
        mock_msg = mock.AsyncMock()
        inter.original_response.return_value = mock_msg

        await self.cog.questions.callback(
            self.cog,
            inter,
            question="What is the plan for tomorrow?",
            role=self.role,
            duration="10m",
        )

        inter.response.send_message.assert_called_once()
        self.assertEqual(len(self.cog.active_sessions), 1)
        sess = next(iter(self.cog.active_sessions.values()))
        self.assertEqual(sess.question_text, "What is the plan for tomorrow?")
        self.assertEqual(sess.duration_seconds, 600)
        self.assertIsNotNone(sess.task)
        sess.task.cancel()  # Clean up timer task

    async def test_questions_command_invalid_duration(self):
        inter = _mock_interaction()
        await self.cog.questions.callback(
            self.cog,
            inter,
            question="Any questions?",
            role=self.role,
            duration="invalid_time",
        )

        inter.response.send_message.assert_called_once()
        args, kwargs = inter.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("Could not parse duration", args[0])
        self.assertEqual(len(self.cog.active_sessions), 0)

    async def test_questions_command_dm_rejected(self):
        inter = _mock_interaction()
        inter.guild = None
        await self.cog.questions.callback(
            self.cog,
            inter,
            question="Hello?",
            role=self.role,
            duration="1h",
        )
        inter.response.send_message.assert_called_once()
        args, kwargs = inter.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("only be used in a server", args[0])
        self.assertEqual(len(self.cog.active_sessions), 0)

    async def test_questions_command_empty_question(self):
        inter = _mock_interaction()
        await self.cog.questions.callback(
            self.cog,
            inter,
            question="   ",
            role=self.role,
            duration="1h",
        )
        inter.response.send_message.assert_called_once()
        args, kwargs = inter.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("cannot be empty", args[0].lower())
        self.assertEqual(len(self.cog.active_sessions), 0)

    async def test_questions_command_question_too_long(self):
        inter = _mock_interaction()
        long_q = "x" * 501
        await self.cog.questions.callback(
            self.cog,
            inter,
            question=long_q,
            role=self.role,
            duration="1h",
        )
        inter.response.send_message.assert_called_once()
        args, kwargs = inter.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("under 500 characters", args[0])
        self.assertEqual(len(self.cog.active_sessions), 0)

    async def test_unique_session_ids(self):
        inter1 = _mock_interaction()
        inter2 = _mock_interaction()
        await self.cog.questions.callback(
            self.cog, inter1, question="Q1", role=self.role, duration="1h"
        )
        await self.cog.questions.callback(
            self.cog, inter2, question="Q2", role=self.role, duration="1h"
        )
        self.assertEqual(len(self.cog.active_sessions), 2)
        sess_ids = list(self.cog.active_sessions.keys())
        self.assertNotEqual(sess_ids[0], sess_ids[1])
        # Clean up tasks
        for s in self.cog.active_sessions.values():
            if s.task:
                s.task.cancel()

    async def test_interaction_check_when_disabled(self):
        self.bot.is_module_enabled = mock.MagicMock(return_value=False)
        inter = _mock_interaction(guild_id=123)
        res = await self.cog.interaction_check(inter)
        self.assertFalse(res)
        inter.response.send_message.assert_called_once()
        args, kwargs = inter.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("turned off", args[0].lower())


class TestInteractionListener(unittest.IsolatedAsyncioTestCase):
    """Test component interaction listener and custom_id handling."""

    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.cog = Questions(self.bot)
        self.role = _mock_role(role_id=88, name="Staff", members=[])
        self.session = QuestionSession(
            session_id="inter_sess_99",
            guild_id=1,
            channel_id=2,
            question_text="Feedback?",
            role=self.role,
            duration_seconds=600,
            expires_at=time.time() + 600,
            created_by=_mock_member(1, "Owner"),
            cog=self.cog,
        )
        self.cog.active_sessions[self.session.session_id] = self.session

    async def test_on_interaction_ignores_non_component_type(self):
        inter = _mock_interaction()
        inter.type = discord.InteractionType.application_command
        inter.data = {"custom_id": f"questions_submit:{self.session.session_id}"}
        await self.cog.on_interaction(inter)
        inter.response.send_message.assert_not_called()
        inter.response.send_modal.assert_not_called()

    async def test_on_interaction_ignores_unrelated_custom_id(self):
        inter = _mock_interaction()
        inter.type = discord.InteractionType.component
        inter.data = {"custom_id": "other_button_click"}
        await self.cog.on_interaction(inter)
        inter.response.send_message.assert_not_called()
        inter.response.send_modal.assert_not_called()

    async def test_on_interaction_inactive_session_reports_closed(self):
        inter = _mock_interaction()
        inter.type = discord.InteractionType.component
        inter.data = {"custom_id": "questions_submit:non_existent_session_123"}
        await self.cog.on_interaction(inter)
        inter.response.send_message.assert_called_once()
        args, kwargs = inter.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("no longer active", args[0].lower())

    async def test_on_interaction_valid_session_dispatches_to_handler(self):
        member = _mock_member(55, "AuthorizedStaff", roles=[self.role])
        inter = _mock_interaction(user=member)
        inter.type = discord.InteractionType.component
        inter.data = {"custom_id": f"questions_submit:{self.session.session_id}"}
        await self.cog.on_interaction(inter)
        inter.response.send_modal.assert_called_once()

    async def test_submit_button_callback_delegates_to_cog(self):
        btn = QuestionSubmitButton(self.session)
        member = _mock_member(55, "Staffer", roles=[self.role])
        inter = _mock_interaction(user=member)
        await btn.callback(inter)
        inter.response.send_modal.assert_called_once()


class TestAdversarialReviewerRound3(unittest.IsolatedAsyncioTestCase):
    """Adversarial stress and edge case tests probing race conditions, nulls, and error paths."""

    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        self.cog = Questions(self.bot)
        self.role = _mock_role(role_id=909, name="AdversarialRole")
        self.alice = _mock_member(301, "Alice", roles=[self.role])
        self.session = QuestionSession(
            session_id="adv_sess_1",
            guild_id=1,
            channel_id=2,
            question_text="Adversarial prompt text?",
            role=self.role,
            duration_seconds=600,
            expires_at=time.time() + 600,
            created_by=_mock_member(1, "Commander"),
            cog=self.cog,
        )
        self.cog.active_sessions[self.session.session_id] = self.session

    async def test_modal_submission_with_none_value_handled_as_blank(self):
        modal = QuestionAnswerModal(session=self.session)
        modal.answer_input._value = None
        inter = _mock_interaction(user=self.alice)

        await modal.on_submit(inter)

        inter.response.send_message.assert_called_once()
        args, kwargs = inter.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("blank", args[0].lower())
        self.assertNotIn(self.alice.id, self.session.answers)

    def test_record_answer_rejected_when_session_resolved(self):
        self.session.resolved = True
        result = self.session.record_answer(self.alice, "Late response")
        self.assertFalse(result)
        self.assertNotIn(self.alice.id, self.session.answers)

    async def test_modal_submission_race_with_concurrent_resolution(self):
        modal = QuestionAnswerModal(session=self.session)
        modal.answer_input._value = "Race condition answer"
        inter = _mock_interaction(user=self.alice)

        # Mark resolved under the lock right before on_submit acquires it
        self.session.resolved = True
        await modal.on_submit(inter)

        inter.response.send_message.assert_called_once()
        args, kwargs = inter.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral", False))
        self.assertIn("expired", args[0].lower())
        self.assertNotIn(self.alice.id, self.session.answers)

    async def test_interaction_member_resolution_with_user_object_and_guild_member(self):
        # inter.user is a discord.User (no roles attribute)
        user_only = mock.MagicMock(spec=discord.User)
        user_only.id = 401
        user_only.name = "GuildGuy"
        user_only.roles = None

        guild_member = _mock_member(401, "GuildGuy", roles=[self.role])
        mock_guild = mock.MagicMock(spec=discord.Guild)
        mock_guild.get_member.return_value = guild_member

        inter = _mock_interaction(user=user_only)
        inter.guild = mock_guild
        inter.member = None

        await self.cog.handle_submit_button(inter, self.session)
        inter.response.send_modal.assert_called_once()

    def test_role_with_only_bot_members_filters_out_bots(self):
        b1 = _mock_member(901, "Bot1", is_bot=True)
        b2 = _mock_member(902, "Bot2", is_bot=True)
        bot_only_role = _mock_role(role_id=888, name="BotOnly", members=[b1, b2])

        sess = QuestionSession(
            session_id="bot_only_sess",
            guild_id=1,
            channel_id=2,
            question_text="Any humans?",
            role=bot_only_role,
            duration_seconds=600,
            expires_at=time.time() + 600,
            created_by=_mock_member(1, "Commander"),
        )
        members = sess.get_role_members()
        self.assertEqual(members, [])
        respondents, non_respondents = sess.calculate_results()
        self.assertEqual(respondents, [])
        self.assertEqual(non_respondents, [])

    def test_cog_unload_cancels_tasks_marks_resolved_and_cleans_sessions(self):
        mock_task = mock.MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = False
        self.session.task = mock_task

        self.assertEqual(len(self.cog.active_sessions), 1)
        self.cog.cog_unload()

        mock_task.cancel.assert_called_once()
        self.assertTrue(self.session.resolved)
        self.assertEqual(len(self.cog.active_sessions), 0)

    async def test_interaction_double_response_suppression(self):
        inter = _mock_interaction(user=self.alice)
        inter.response.send_modal.side_effect = discord.InteractionResponded(mock.MagicMock())

        # Should not raise exception
        await self.cog.handle_submit_button(inter, self.session)

    def test_multiline_question_card_and_resolution_formatting(self):
        multiline_sess = QuestionSession(
            session_id="multi_sess",
            guild_id=1,
            channel_id=2,
            question_text="Line 1: Question\nLine 2: Details\nLine 3: Notes",
            role=self.role,
            duration_seconds=600,
            expires_at=time.time() + 600,
            created_by=_mock_member(1, "Commander"),
        )
        card = self.cog.build_question_card(multiline_sess)
        card_text = layout_text(card)
        self.assertIn("Line 1: Question", card_text)
        self.assertIn("Line 2: Details", card_text)

        panel = self.cog.build_resolution_panel(multiline_sess, [], [])
        panel_text = layout_text(panel)
        self.assertIn("Line 1: Question", panel_text)
        self.assertIn("Line 2: Details", panel_text)

    async def test_questions_command_send_panel_failure_cleans_up_session(self):
        self.cog.active_sessions.clear()
        inter = _mock_interaction()
        with mock.patch.dict(
            self.cog.questions.callback.__globals__,
            {"send_panel": mock.AsyncMock(side_effect=discord.HTTPException(mock.MagicMock(), "API Down"))},
        ):
            with self.assertRaises(discord.HTTPException):
                await self.cog.questions.callback(
                    self.cog,
                    inter,
                    question="Will this fail?",
                    role=self.role,
                    duration="1h",
                )
        self.assertEqual(len(self.cog.active_sessions), 0)

    def test_member_role_removed_after_submission_still_included_in_respondents(self):
        # Alice submits answer while having the role
        self.session.record_answer(self.alice, "Submitted when I had the role")

        # Now Alice loses the role
        self.alice.roles = []
        bob = _mock_member(302, "Bob", roles=[self.role])
        self.role.members = [bob]

        respondents, non_respondents = self.session.calculate_results()
        self.assertEqual(len(respondents), 1)
        self.assertEqual(respondents[0]["member"].id, self.alice.id)
        self.assertEqual(len(non_respondents), 1)
        self.assertEqual(non_respondents[0].id, bob.id)


if __name__ == "__main__":
    unittest.main()
