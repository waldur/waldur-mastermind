from django.test import TestCase

from waldur_mastermind.chat.intent_classifier import Intent, classify_intent


class IntentEnumTest(TestCase):
    def test_tool_action_includes_tools(self):
        self.assertTrue(Intent.TOOL_ACTION.include_tools)

    def test_ambiguous_includes_tools(self):
        self.assertTrue(Intent.AMBIGUOUS.include_tools)

    def test_knowledge_excludes_tools(self):
        self.assertFalse(Intent.KNOWLEDGE.include_tools)

    def test_greeting_excludes_tools(self):
        self.assertFalse(Intent.GREETING.include_tools)


class GreetingIntentTest(TestCase):
    def test_hello(self):
        self.assertEqual(classify_intent("hello"), Intent.GREETING)

    def test_hi(self):
        self.assertEqual(classify_intent("hi"), Intent.GREETING)

    def test_hey(self):
        self.assertEqual(classify_intent("hey"), Intent.GREETING)

    def test_good_morning(self):
        self.assertEqual(classify_intent("good morning"), Intent.GREETING)

    def test_good_evening(self):
        self.assertEqual(classify_intent("good evening!"), Intent.GREETING)

    def test_thanks(self):
        self.assertEqual(classify_intent("thanks"), Intent.GREETING)

    def test_thank_you(self):
        self.assertEqual(classify_intent("thank you"), Intent.GREETING)

    def test_bye(self):
        self.assertEqual(classify_intent("bye"), Intent.GREETING)

    def test_goodbye(self):
        self.assertEqual(classify_intent("goodbye"), Intent.GREETING)

    def test_cheers(self):
        self.assertEqual(classify_intent("cheers"), Intent.GREETING)

    def test_case_insensitive(self):
        self.assertEqual(classify_intent("Hello"), Intent.GREETING)
        self.assertEqual(classify_intent("HELLO"), Intent.GREETING)

    def test_with_punctuation(self):
        self.assertEqual(classify_intent("hello!"), Intent.GREETING)
        self.assertEqual(classify_intent("hi?"), Intent.GREETING)

    def test_hello_with_tool_request_is_not_greeting(self):
        result = classify_intent("hello, show my resources")
        self.assertNotEqual(result, Intent.GREETING)

    def test_long_message_is_not_greeting(self):
        result = classify_intent(
            "hello, I need help understanding how Waldur works and what it can do"
        )
        self.assertNotEqual(result, Intent.GREETING)


class ToolActionIntentTest(TestCase):
    def test_show_my_resources(self):
        self.assertEqual(classify_intent("show my resources"), Intent.TOOL_ACTION)

    def test_list_my_vms(self):
        self.assertEqual(classify_intent("list my VMs"), Intent.TOOL_ACTION)

    def test_display_my_resources(self):
        self.assertEqual(classify_intent("display my resources"), Intent.TOOL_ACTION)

    def test_create_a_vm(self):
        self.assertEqual(classify_intent("create a VM"), Intent.TOOL_ACTION)

    def test_create_new_virtual_machine(self):
        self.assertEqual(
            classify_intent("create a new virtual machine"), Intent.TOOL_ACTION
        )

    def test_show_me_my_projects(self):
        self.assertEqual(classify_intent("show me my projects"), Intent.TOOL_ACTION)

    def test_get_my_resources(self):
        self.assertEqual(classify_intent("get my resources"), Intent.TOOL_ACTION)

    def test_deploy_a_vm(self):
        self.assertEqual(classify_intent("deploy a vm"), Intent.TOOL_ACTION)

    def test_launch_new_instance(self):
        self.assertEqual(classify_intent("launch a new instance"), Intent.TOOL_ACTION)

    def test_my_resources(self):
        self.assertEqual(classify_intent("my resources"), Intent.TOOL_ACTION)

    def test_case_insensitive(self):
        self.assertEqual(classify_intent("SHOW MY RESOURCES"), Intent.TOOL_ACTION)


class KnowledgeIntentTest(TestCase):
    def test_what_is_a_vm(self):
        self.assertEqual(classify_intent("what is a VM?"), Intent.KNOWLEDGE)

    def test_how_does_waldur_work(self):
        self.assertEqual(classify_intent("how does Waldur work?"), Intent.KNOWLEDGE)

    def test_explain_resources(self):
        self.assertEqual(classify_intent("explain resources"), Intent.KNOWLEDGE)

    def test_what_can_you_do(self):
        self.assertEqual(classify_intent("what can you do?"), Intent.KNOWLEDGE)

    def test_how_do_i_debug(self):
        self.assertEqual(
            classify_intent("how do I debug network issues?"), Intent.KNOWLEDGE
        )

    def test_best_practices(self):
        self.assertEqual(
            classify_intent("what are SSH key best practices?"), Intent.KNOWLEDGE
        )

    def test_eol_question(self):
        self.assertEqual(
            classify_intent("which distros have reached EOL?"), Intent.KNOWLEDGE
        )

    def test_troubleshoot(self):
        self.assertEqual(
            classify_intent("how to troubleshoot VM access?"), Intent.KNOWLEDGE
        )

    def test_tell_me_about(self):
        self.assertEqual(
            classify_intent("tell me about security groups"), Intent.KNOWLEDGE
        )

    def test_what_are_my_resources_is_ambiguous(self):
        """'what are my resources' has both knowledge and tool signals."""
        self.assertEqual(classify_intent("what are my resources"), Intent.AMBIGUOUS)

    def test_how_do_i_create_a_vm_is_ambiguous(self):
        """'how do I create a VM' has both knowledge and tool signals."""
        result = classify_intent("how do I create a VM?")
        self.assertEqual(result, Intent.AMBIGUOUS)

    def test_help_me_understand_is_knowledge(self):
        result = classify_intent("help me understand security groups")
        self.assertEqual(result, Intent.KNOWLEDGE)

    def test_help_me_create_vm_is_tool_action(self):
        """'help me create a VM' should trigger tool action, not knowledge."""
        result = classify_intent("help me create a VM")
        self.assertEqual(result, Intent.TOOL_ACTION)


class AmbiguousIntentTest(TestCase):
    def test_short_unclear_message(self):
        self.assertEqual(classify_intent("ok"), Intent.AMBIGUOUS)

    def test_yes(self):
        self.assertEqual(classify_intent("yes"), Intent.AMBIGUOUS)

    def test_proceed(self):
        self.assertEqual(classify_intent("proceed"), Intent.AMBIGUOUS)

    def test_random_text(self):
        self.assertEqual(classify_intent("just checking in"), Intent.AMBIGUOUS)

    def test_mixed_knowledge_and_action(self):
        """Messages with both signals default to AMBIGUOUS."""
        result = classify_intent("show me my VMs running EOL distros")
        self.assertEqual(result, Intent.AMBIGUOUS)


class ConversationContextOverrideTest(TestCase):
    def _history_with_tool_calls(self):
        return [
            {"role": "user", "content": "show my resources"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "show_user_resources",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_abc",
                "content": "Found 3 resources",
            },
        ]

    def _history_without_tool_calls(self):
        return [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Hi! How can I help?"},
        ]

    def test_greeting_after_tool_calls_returns_ambiguous(self):
        """Mid-workflow: even a greeting should keep tools available."""
        result = classify_intent("thanks", self._history_with_tool_calls())
        self.assertEqual(result, Intent.AMBIGUOUS)

    def test_knowledge_after_tool_calls_returns_ambiguous(self):
        """Mid-workflow: knowledge question keeps tools available."""
        result = classify_intent("what is this?", self._history_with_tool_calls())
        self.assertEqual(result, Intent.AMBIGUOUS)

    def test_yes_after_tool_calls_returns_ambiguous(self):
        result = classify_intent("yes", self._history_with_tool_calls())
        self.assertEqual(result, Intent.AMBIGUOUS)

    def test_greeting_without_recent_tool_calls_is_greeting(self):
        result = classify_intent("hello", self._history_without_tool_calls())
        self.assertEqual(result, Intent.GREETING)

    def test_knowledge_without_recent_tool_calls_is_knowledge(self):
        result = classify_intent("what is a VM?", self._history_without_tool_calls())
        self.assertEqual(result, Intent.KNOWLEDGE)

    def test_tool_calls_detected_despite_expanded_messages(self):
        """Mid-workflow detection counts assistant messages, not expanded items.

        _get_conversation_history expands each tool-calling assistant message
        into 1 assistant + N tool messages. The window should filter to
        assistant-role messages so tool results don't push tool_calls out.
        """
        history = [
            {"role": "user", "content": "show my resources"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "show_user_resources",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "Found 3 resources"},
            {"role": "user", "content": "interesting"},
            {"role": "assistant", "content": "Glad you found that useful!"},
            {"role": "user", "content": "tell me more"},
            {"role": "assistant", "content": "Sure, here are more details..."},
            {"role": "user", "content": "ok"},
            {"role": "assistant", "content": "Anything else?"},
        ]
        # The tool_calls assistant is 4 assistant messages back, but still within
        # the window because we count assistant messages, not all messages.
        result = classify_intent("thanks", history)
        self.assertEqual(result, Intent.AMBIGUOUS)

    def test_none_history_uses_normal_classification(self):
        self.assertEqual(classify_intent("hello", None), Intent.GREETING)

    def test_empty_history_uses_normal_classification(self):
        self.assertEqual(classify_intent("hello", []), Intent.GREETING)


class EdgeCaseTest(TestCase):
    def test_empty_string(self):
        self.assertEqual(classify_intent(""), Intent.AMBIGUOUS)

    def test_whitespace_only(self):
        self.assertEqual(classify_intent("   "), Intent.AMBIGUOUS)

    def test_very_long_input(self):
        long_input = "show my resources " * 100
        self.assertEqual(classify_intent(long_input), Intent.TOOL_ACTION)

    def test_unicode(self):
        result = classify_intent("hello \U0001f44b")
        self.assertIsInstance(result, Intent)

    def test_mixed_case(self):
        self.assertEqual(classify_intent("ShOw My ReSoUrCeS"), Intent.TOOL_ACTION)
