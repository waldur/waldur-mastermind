from constance.test.unittest import override_config as override_constance_config
from django.db import IntegrityError
from django.test import TestCase
from rest_framework.test import APIClient

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.context_assembler import build_context
from waldur_mastermind.chat.models import ChatSession, SystemPrompt, ThreadSession


class SystemPromptModelTest(TestCase):
    def test_create_system_prompt(self):
        prompt = SystemPrompt.objects.create(
            name="Test Prompt",
            custom_instructions="Always greet users by name.",
        )
        self.assertEqual(prompt.name, "Test Prompt")
        self.assertFalse(prompt.is_active)
        self.assertIsNotNone(prompt.uuid)

    def test_get_active_returns_none_when_no_active(self):
        SystemPrompt.objects.create(name="Inactive", is_active=False)
        self.assertIsNone(SystemPrompt.get_active())

    def test_get_active_returns_active_prompt(self):
        prompt = SystemPrompt.objects.create(name="Active", is_active=True)
        result = SystemPrompt.get_active()
        self.assertEqual(result.pk, prompt.pk)

    def test_unique_active_constraint_prevents_two_active(self):
        SystemPrompt.objects.create(name="First Active", is_active=True)
        with self.assertRaises(IntegrityError):
            SystemPrompt.objects.create(name="Second Active", is_active=True)

    def test_multiple_inactive_allowed(self):
        SystemPrompt.objects.create(name="Inactive 1", is_active=False)
        SystemPrompt.objects.create(name="Inactive 2", is_active=False)
        self.assertEqual(SystemPrompt.objects.filter(is_active=False).count(), 2)

    def test_str_representation(self):
        prompt = SystemPrompt(name="My Prompt", is_active=False)
        self.assertEqual(str(prompt), "My Prompt")
        prompt.is_active = True
        self.assertEqual(str(prompt), "My Prompt (active)")


class SystemPromptContextAssemblerTest(TestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=session)

    def test_default_uses_hardcoded_prompts(self):
        """When no SystemPrompt is active, hardcoded defaults are used."""
        context = build_context(self.user, "Hello", thread=self.thread)
        system_msg = next(m for m in context if m["role"] == "system")
        self.assertIn("a highly knowledgeable", system_msg["content"])
        self.assertIn("SCOPE:", system_msg["content"])

    @override_constance_config(AI_ASSISTANT_NAME="TestBot", SITE_NAME="TestOrg")
    def test_active_prompt_injects_custom_instructions(self):
        """Active SystemPrompt injects custom_instructions into the prompt."""
        SystemPrompt.objects.create(
            name="Custom",
            custom_instructions="Always mention the TechCorp helpdesk for billing issues.",
            is_active=True,
        )
        context = build_context(self.user, "Hello", thread=self.thread)
        system_msg = next(m for m in context if m["role"] == "system")
        # Custom instructions appear
        self.assertIn("ADDITIONAL INSTRUCTIONS", system_msg["content"])
        self.assertIn(
            "Always mention the TechCorp helpdesk for billing issues.",
            system_msg["content"],
        )
        # Core prompt structure is still present
        self.assertIn("a highly knowledgeable", system_msg["content"])
        self.assertIn("SCOPE:", system_msg["content"])

    @override_constance_config(AI_ASSISTANT_NAME="TestBot", SITE_NAME="TestOrg")
    def test_placeholders_resolved_in_custom_instructions(self):
        """Placeholders {assistant_name} and {organization} are resolved."""
        SystemPrompt.objects.create(
            name="With placeholders",
            custom_instructions="I am {assistant_name} serving {organization}.",
            is_active=True,
        )
        context = build_context(self.user, "Hello", thread=self.thread)
        system_msg = next(m for m in context if m["role"] == "system")
        self.assertIn("I am TestBot serving TestOrg.", system_msg["content"])

    @override_constance_config(AI_ASSISTANT_NAME="TestBot", SITE_NAME="TestOrg")
    def test_unknown_placeholders_pass_through_safely(self):
        """Unknown {placeholders} in admin-supplied text must not crash."""
        SystemPrompt.objects.create(
            name="With braces",
            custom_instructions="Contact {unknown_placeholder} for help.",
            is_active=True,
        )
        context = build_context(self.user, "Hello", thread=self.thread)
        system_msg = next(m for m in context if m["role"] == "system")
        self.assertIn("{unknown_placeholder}", system_msg["content"])

    @override_constance_config(AI_ASSISTANT_NAME="TestBot", SITE_NAME="TestOrg")
    def test_empty_custom_instructions_no_section_header(self):
        """Active prompt with empty custom_instructions doesn't add a blank section."""
        SystemPrompt.objects.create(
            name="Empty instructions",
            custom_instructions="",
            is_active=True,
        )
        context = build_context(self.user, "Hello", thread=self.thread)
        system_msg = next(m for m in context if m["role"] == "system")
        self.assertNotIn("ADDITIONAL INSTRUCTIONS", system_msg["content"])

    @override_constance_config(AI_ASSISTANT_NAME="TestBot", SITE_NAME="TestOrg")
    def test_whitespace_only_custom_instructions_no_section_header(self):
        """Whitespace-only custom_instructions is treated as empty."""
        SystemPrompt.objects.create(
            name="Whitespace only",
            custom_instructions="   \n  ",
            is_active=True,
        )
        context = build_context(self.user, "Hello", thread=self.thread)
        system_msg = next(m for m in context if m["role"] == "system")
        self.assertNotIn("ADDITIONAL INSTRUCTIONS", system_msg["content"])

    @override_constance_config(
        AI_ASSISTANT_NAME="TestBot",
        SITE_NAME="TestOrg",
        AI_ASSISTANT_SYSTEM_PROMPT_CUSTOM_INSTRUCTIONS=(
            "Fallback from constance for {organization}."
        ),
    )
    def test_constance_fallback_used_when_no_active_prompt(self):
        """Constance override is injected when no active SystemPrompt exists."""
        context = build_context(self.user, "Hello", thread=self.thread)
        system_msg = next(m for m in context if m["role"] == "system")
        self.assertIn("ADDITIONAL INSTRUCTIONS", system_msg["content"])
        self.assertIn("Fallback from constance for TestOrg.", system_msg["content"])

    @override_constance_config(
        AI_ASSISTANT_NAME="TestBot",
        SITE_NAME="TestOrg",
        AI_ASSISTANT_SYSTEM_PROMPT_CUSTOM_INSTRUCTIONS="From constance.",
    )
    def test_active_prompt_takes_precedence_over_constance(self):
        """Active SystemPrompt custom_instructions override the Constance fallback."""
        SystemPrompt.objects.create(
            name="Custom",
            custom_instructions="From SystemPrompt.",
            is_active=True,
        )
        context = build_context(self.user, "Hello", thread=self.thread)
        system_msg = next(m for m in context if m["role"] == "system")
        self.assertIn("From SystemPrompt.", system_msg["content"])
        self.assertNotIn("From constance.", system_msg["content"])

    @override_constance_config(
        AI_ASSISTANT_NAME="TestBot",
        SITE_NAME="TestOrg",
        AI_ASSISTANT_SYSTEM_PROMPT_CUSTOM_INSTRUCTIONS="From constance.",
    )
    def test_empty_active_prompt_falls_back_to_constance(self):
        """Active prompt with empty custom_instructions still falls back to Constance."""
        SystemPrompt.objects.create(
            name="Empty",
            custom_instructions="   ",
            is_active=True,
        )
        context = build_context(self.user, "Hello", thread=self.thread)
        system_msg = next(m for m in context if m["role"] == "system")
        self.assertIn("From constance.", system_msg["content"])

    @override_constance_config(AI_ASSISTANT_NAME="TestBot", SITE_NAME="TestOrg")
    def test_inactive_prompt_ignored(self):
        """Inactive SystemPrompt is not used."""
        SystemPrompt.objects.create(
            name="Inactive",
            custom_instructions="Should not appear.",
            is_active=False,
        )
        context = build_context(self.user, "Hello", thread=self.thread)
        system_msg = next(m for m in context if m["role"] == "system")
        self.assertNotIn("Should not appear", system_msg["content"])
        self.assertNotIn("ADDITIONAL INSTRUCTIONS", system_msg["content"])


class SystemPromptAPITest(TestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.user = structure_factories.UserFactory()
        self.client = APIClient()
        self.list_url = "/api/chat-system-prompts/"

    def test_non_staff_cannot_list(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 403)

    def test_staff_can_list(self):
        self.client.force_authenticate(self.staff)
        SystemPrompt.objects.create(name="Prompt A")
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_staff_can_create(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.list_url,
            {
                "name": "New Prompt",
                "custom_instructions": "Always mention the helpdesk.",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(SystemPrompt.objects.count(), 1)

    def test_non_staff_cannot_create(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(self.list_url, {"name": "Nope"})
        self.assertEqual(response.status_code, 403)

    def test_staff_can_update(self):
        self.client.force_authenticate(self.staff)
        prompt = SystemPrompt.objects.create(name="Original")
        url = f"/api/chat-system-prompts/{prompt.uuid}/"
        response = self.client.patch(url, {"name": "Updated"})
        self.assertEqual(response.status_code, 200)
        prompt.refresh_from_db()
        self.assertEqual(prompt.name, "Updated")

    def test_staff_can_delete(self):
        self.client.force_authenticate(self.staff)
        prompt = SystemPrompt.objects.create(name="Deletable")
        url = f"/api/chat-system-prompts/{prompt.uuid}/"
        response = self.client.delete(url)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(SystemPrompt.objects.count(), 0)

    def test_activate_action(self):
        self.client.force_authenticate(self.staff)
        prompt = SystemPrompt.objects.create(name="To Activate")
        url = f"/api/chat-system-prompts/{prompt.uuid}/activate/"
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        prompt.refresh_from_db()
        self.assertTrue(prompt.is_active)

    def test_activate_deactivates_previous(self):
        self.client.force_authenticate(self.staff)
        old = SystemPrompt.objects.create(name="Old Active", is_active=True)
        new = SystemPrompt.objects.create(name="New Active")
        url = f"/api/chat-system-prompts/{new.uuid}/activate/"
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertFalse(old.is_active)
        self.assertTrue(new.is_active)

    def test_is_active_ignored_on_create(self):
        """is_active is read-only; must use activate action instead."""
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.list_url, {"name": "Try Active", "is_active": True}
        )
        self.assertEqual(response.status_code, 201)
        prompt = SystemPrompt.objects.get(name="Try Active")
        self.assertFalse(prompt.is_active)

    def test_is_active_ignored_on_update(self):
        """is_active is read-only; must use activate action instead."""
        self.client.force_authenticate(self.staff)
        prompt = SystemPrompt.objects.create(name="Inactive")
        url = f"/api/chat-system-prompts/{prompt.uuid}/"
        response = self.client.patch(url, {"is_active": True})
        self.assertEqual(response.status_code, 200)
        prompt.refresh_from_db()
        self.assertFalse(prompt.is_active)

    def test_deactivate_action(self):
        self.client.force_authenticate(self.staff)
        prompt = SystemPrompt.objects.create(name="Active", is_active=True)
        url = f"/api/chat-system-prompts/{prompt.uuid}/deactivate/"
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        prompt.refresh_from_db()
        self.assertFalse(prompt.is_active)
