"""Tests for complex workflow scenarios from documentation.

Tests comprehensive workflows that combine multiple checklist features:
- Question dependencies with conditional user guidance
- Review triggers combined with user guidance
- Multi-step approval workflows
- End-to-end compliance checking scenarios
- Real-world integration patterns
"""

from ddt import ddt
from rest_framework import test

from waldur_core.checklist import enums, models
from waldur_core.checklist.tests import factories
from waldur_core.structure.tests import fixtures as structure_fixtures


@ddt
class ComplexWorkflowScenariosTest(test.APITransactionTestCase):
    """Test complex workflows combining multiple checklist features."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.checklist = factories.ChecklistFactory(
            name="Comprehensive Data Processing Checklist"
        )

    def test_gdpr_compliance_multi_step_workflow(self):
        """Test complete GDPR compliance workflow with dependencies and guidance."""
        # Step 1: Initial question about EU data processing
        eu_data_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Will you process EU citizen data?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            order=1,
            user_guidance="Processing EU citizen data requires GDPR compliance.",
            always_show_guidance=True,
        )

        # Step 2: Data types question (dependent on Step 1 = Yes)
        data_types_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="What types of personal data will you process?",
            question_type=enums.QuestionTypes.MULTI_SELECT,
            required=True,
            order=2,
            user_guidance="Special categories of data require additional protections under GDPR Article 9.",
            always_show_guidance=False,
            guidance_answer_value=["health", "biometric", "genetic", "religious"],
            guidance_operator="in",
            # Review trigger for high-risk data types
            always_requires_review=False,
            review_answer_value=["health", "biometric", "genetic", "criminal"],
            operator="in",
        )

        # Create dependency: data types only visible if EU data = Yes
        factories.QuestionDependencyFactory(
            question=data_types_question,
            depends_on_question=eu_data_question,
            required_answer_value=True,
            operator="equals",
        )

        # Step 3: Data processing basis (dependent on Step 1 = Yes)
        legal_basis_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="What is your legal basis for processing?",
            question_type=enums.QuestionTypes.SINGLE_SELECT,
            required=True,
            order=3,
            user_guidance="Consent requires explicit opt-in mechanisms. Legitimate interest requires balancing assessment.",
            always_show_guidance=False,
            guidance_answer_value=["consent", "legitimate_interest"],
            guidance_operator="in",
            # Always requires review for proper legal basis verification
            always_requires_review=True,
        )

        factories.QuestionDependencyFactory(
            question=legal_basis_question,
            depends_on_question=eu_data_question,
            required_answer_value=True,
            operator="equals",
        )

        # Step 4: Third-party transfers (dependent on Step 1 = Yes)
        transfer_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Will you transfer data outside the EU?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            order=4,
            user_guidance="International transfers require adequate protection mechanisms (adequacy decision, SCCs, or derogations).",
            always_show_guidance=False,
            guidance_answer_value=True,
            guidance_operator="equals",
            # Review required for any international transfers
            always_requires_review=False,
            review_answer_value=True,
            operator="equals",
        )

        factories.QuestionDependencyFactory(
            question=transfer_question,
            depends_on_question=eu_data_question,
            required_answer_value=True,
            operator="equals",
        )

        # Create completion
        completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.fixture.project
        )

        # Test Step 1: Answer EU data processing = Yes
        models.Answer.objects.create(
            completion=completion,
            question=eu_data_question,
            user=self.fixture.admin,
            answer_data=True,
        )

        # Verify dependent questions become visible
        visible_questions = self.checklist.get_visible_questions(completion)
        visible_descriptions = [q.description for q in visible_questions]

        self.assertIn("Will you process EU citizen data?", visible_descriptions)
        self.assertIn(
            "What types of personal data will you process?", visible_descriptions
        )
        self.assertIn("What is your legal basis for processing?", visible_descriptions)
        self.assertIn("Will you transfer data outside the EU?", visible_descriptions)

        # Test Step 2: Answer with high-risk data type (should trigger guidance & review)
        models.Answer.objects.create(
            completion=completion,
            question=data_types_question,
            user=self.fixture.admin,
            answer_data=["health"],  # High-risk data type
        )

        answer = models.Answer.objects.get(
            completion=completion, question=data_types_question
        )

        # Should show guidance for special category data
        self.assertTrue(data_types_question.should_show_guidance(["health"]))
        # Should trigger review for health data
        self.assertTrue(answer.requires_review)

        # Test Step 3: Answer legal basis (always requires review)
        models.Answer.objects.create(
            completion=completion,
            question=legal_basis_question,
            user=self.fixture.admin,
            answer_data=["consent"],
        )

        legal_answer = models.Answer.objects.get(
            completion=completion, question=legal_basis_question
        )
        self.assertTrue(legal_answer.requires_review)  # Always requires review

        # Test Step 4: Answer international transfer = Yes (should trigger guidance & review)
        models.Answer.objects.create(
            completion=completion,
            question=transfer_question,
            user=self.fixture.admin,
            answer_data=True,
        )

        transfer_answer = models.Answer.objects.get(
            completion=completion, question=transfer_question
        )

        # Should show guidance for international transfers
        self.assertTrue(transfer_question.should_show_guidance(True))
        # Should trigger review for international transfers
        self.assertTrue(transfer_answer.requires_review)

        # Verify completion status
        completion.refresh_from_db()
        self.assertTrue(completion.is_completed)  # All required questions answered

        # Verify review workload - multiple answers should require review
        answers_needing_review = models.Answer.objects.filter(
            completion=completion, requires_review=True
        ).count()
        self.assertEqual(
            answers_needing_review, 3
        )  # data types, legal basis, transfers

    def test_financial_compliance_branching_workflow(self):
        """Test financial compliance workflow with complex branching logic."""
        # Root question
        financial_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Will your application handle financial transactions?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            order=1,
            user_guidance="Financial transaction processing requires PCI DSS compliance and additional security measures.",
            always_show_guidance=False,
            guidance_answer_value=True,
            guidance_operator="equals",
            # Always review financial applications
            always_requires_review=False,
            review_answer_value=True,
            operator="equals",
        )

        # Branch 1: Transaction types (if financial = Yes)
        transaction_types_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="What types of financial transactions?",
            question_type=enums.QuestionTypes.MULTI_SELECT,
            required=True,
            order=2,
            user_guidance="Credit card processing requires PCI DSS Level 1 compliance.",
            always_show_guidance=False,
            guidance_answer_value=["credit_card", "debit_card"],
            guidance_operator="in",
            # High-value transactions need extra review
            always_requires_review=False,
            review_answer_value=["wire_transfer", "cryptocurrency"],
            operator="in",
        )

        factories.QuestionDependencyFactory(
            question=transaction_types_question,
            depends_on_question=financial_question,
            required_answer_value=True,
            operator="equals",
        )

        # Branch 2A: PCI compliance (if credit/debit cards selected)
        pci_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Do you have current PCI DSS certification?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            order=3,
            user_guidance="PCI DSS certification is mandatory for card processing. Contact Security team if you need help obtaining certification.",
            always_show_guidance=False,
            guidance_answer_value=False,
            guidance_operator="equals",
            # Always review PCI compliance claims
            always_requires_review=True,
        )

        # Complex dependency: PCI question depends on having card transactions
        factories.QuestionDependencyFactory(
            question=pci_question,
            depends_on_question=transaction_types_question,
            required_answer_value=["credit_card", "debit_card"],
            operator="in",
        )

        # Branch 2B: AML compliance (if wire transfers or crypto)
        aml_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Have you implemented AML/KYC procedures?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            order=4,
            user_guidance="Anti-Money Laundering procedures are required for high-value transfers and cryptocurrency transactions.",
            always_show_guidance=False,
            guidance_answer_value=False,
            guidance_operator="equals",
            # Always review AML compliance
            always_requires_review=True,
        )

        factories.QuestionDependencyFactory(
            question=aml_question,
            depends_on_question=transaction_types_question,
            required_answer_value=["wire_transfer", "cryptocurrency"],
            operator="in",
        )

        # Create completion
        completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.fixture.project
        )

        # Test non-financial path (should end early)
        models.Answer.objects.create(
            completion=completion,
            question=financial_question,
            user=self.fixture.admin,
            answer_data=False,
        )

        # Only root question should be visible for non-financial apps
        visible_questions = self.checklist.get_visible_questions(completion)
        self.assertEqual(len(visible_questions), 1)
        self.assertEqual(visible_questions[0], financial_question)

        # Change to financial application
        models.Answer.objects.filter(
            completion=completion, question=financial_question
        ).delete()
        models.Answer.objects.create(
            completion=completion,
            question=financial_question,
            user=self.fixture.admin,
            answer_data=True,
        )

        # Now transaction types should be visible
        visible_questions = self.checklist.get_visible_questions(completion)
        self.assertEqual(len(visible_questions), 2)

        # Test card processing branch
        models.Answer.objects.create(
            completion=completion,
            question=transaction_types_question,
            user=self.fixture.admin,
            answer_data=["credit_card"],
        )

        # PCI question should now be visible, but not AML
        visible_questions = self.checklist.get_visible_questions(completion)
        question_descriptions = [q.description for q in visible_questions]

        self.assertIn(
            "Do you have current PCI DSS certification?", question_descriptions
        )
        self.assertNotIn(
            "Have you implemented AML/KYC procedures?", question_descriptions
        )

        # Test guidance triggers
        self.assertTrue(
            transaction_types_question.should_show_guidance(["credit_card"])
        )

        # Test multiple transaction types (should show both compliance questions)
        models.Answer.objects.filter(
            completion=completion, question=transaction_types_question
        ).delete()
        models.Answer.objects.create(
            completion=completion,
            question=transaction_types_question,
            user=self.fixture.admin,
            answer_data=["credit_card", "wire_transfer"],
        )

        visible_questions = self.checklist.get_visible_questions(completion)
        question_descriptions = [q.description for q in visible_questions]

        # Both compliance questions should be visible
        self.assertIn(
            "Do you have current PCI DSS certification?", question_descriptions
        )
        self.assertIn("Have you implemented AML/KYC procedures?", question_descriptions)

        # Answer compliance questions
        models.Answer.objects.create(
            completion=completion,
            question=pci_question,
            user=self.fixture.admin,
            answer_data=True,
        )

        models.Answer.objects.create(
            completion=completion,
            question=aml_question,
            user=self.fixture.admin,
            answer_data=True,
        )

        # Verify review triggers
        financial_answer = models.Answer.objects.get(
            completion=completion, question=financial_question
        )
        transaction_answer = models.Answer.objects.get(
            completion=completion, question=transaction_types_question
        )
        pci_answer = models.Answer.objects.get(
            completion=completion, question=pci_question
        )
        aml_answer = models.Answer.objects.get(
            completion=completion, question=aml_question
        )

        self.assertTrue(
            financial_answer.requires_review
        )  # Financial = Yes triggers review
        self.assertTrue(
            transaction_answer.requires_review
        )  # Wire transfer triggers review
        self.assertTrue(pci_answer.requires_review)  # Always requires review
        self.assertTrue(aml_answer.requires_review)  # Always requires review

    def test_ai_ethics_workflow_with_conditional_guidance(self):
        """Test AI/ML ethics review workflow with sophisticated conditional guidance."""
        # Question 1: AI/ML usage
        ai_usage_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Does your project use AI or machine learning?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            order=1,
            user_guidance="AI/ML systems require ethical review and bias testing.",
            always_show_guidance=False,
            guidance_answer_value=True,
            guidance_operator="equals",
        )

        # Question 2: AI application types (dependent)
        ai_types_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="What AI/ML applications will you use?",
            question_type=enums.QuestionTypes.MULTI_SELECT,
            required=True,
            order=2,
            user_guidance="High-risk AI applications (facial recognition, hiring, lending) require additional ethical safeguards.",
            always_show_guidance=False,
            guidance_answer_value=[
                "facial_recognition",
                "hiring_ai",
                "credit_scoring",
                "medical_diagnosis",
            ],
            guidance_operator="in",
            # High-risk AI always needs review
            always_requires_review=False,
            review_answer_value=[
                "facial_recognition",
                "hiring_ai",
                "credit_scoring",
                "medical_diagnosis",
            ],
            operator="in",
        )

        factories.QuestionDependencyFactory(
            question=ai_types_question,
            depends_on_question=ai_usage_question,
            required_answer_value=True,
            operator="equals",
        )

        # Question 3: Training data (dependent)
        training_data_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="What data will you use for training?",
            question_type=enums.QuestionTypes.MULTI_SELECT,
            required=True,
            order=3,
            user_guidance="Sensitive training data requires privacy impact assessment and bias testing.",
            always_show_guidance=False,
            guidance_answer_value=[
                "demographic",
                "behavioral",
                "biometric",
                "personal",
            ],
            guidance_operator="in",
            # Biometric and demographic data need review
            always_requires_review=False,
            review_answer_value=["biometric", "demographic"],
            operator="in",
        )

        factories.QuestionDependencyFactory(
            question=training_data_question,
            depends_on_question=ai_usage_question,
            required_answer_value=True,
            operator="equals",
        )

        # Question 4: Bias testing (dependent on high-risk AI or sensitive data)
        bias_testing_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Have you conducted bias testing?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            order=4,
            user_guidance="Bias testing must be performed by qualified data scientists. Contact the AI Ethics team for assistance.",
            always_show_guidance=False,
            guidance_answer_value=False,
            guidance_operator="equals",
            # Always review bias testing claims
            always_requires_review=True,
        )

        # Complex dependency: bias testing required for high-risk AI OR sensitive training data
        # This simulates "OR" logic by creating multiple dependencies
        factories.QuestionDependencyFactory(
            question=bias_testing_question,
            depends_on_question=ai_types_question,
            required_answer_value=["facial_recognition", "hiring_ai", "credit_scoring"],
            operator="in",
        )

        # Create completion and test workflow
        completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.fixture.project
        )

        # Test non-AI path
        models.Answer.objects.create(
            completion=completion,
            question=ai_usage_question,
            user=self.fixture.admin,
            answer_data=False,
        )

        visible_questions = self.checklist.get_visible_questions(completion)
        self.assertEqual(len(visible_questions), 1)  # Only root question visible

        # Switch to AI project
        models.Answer.objects.filter(
            completion=completion, question=ai_usage_question
        ).delete()
        models.Answer.objects.create(
            completion=completion,
            question=ai_usage_question,
            user=self.fixture.admin,
            answer_data=True,
        )

        # Now AI-specific questions should be visible
        visible_questions = self.checklist.get_visible_questions(completion)
        self.assertEqual(len(visible_questions), 3)  # Root + types + training data

        # Test low-risk AI (chatbot)
        models.Answer.objects.create(
            completion=completion,
            question=ai_types_question,
            user=self.fixture.admin,
            answer_data=["chatbot"],
        )

        models.Answer.objects.create(
            completion=completion,
            question=training_data_question,
            user=self.fixture.admin,
            answer_data=["public_text"],
        )

        # Should not trigger guidance or review for low-risk AI
        types_answer = models.Answer.objects.get(
            completion=completion, question=ai_types_question
        )
        training_answer = models.Answer.objects.get(
            completion=completion, question=training_data_question
        )

        self.assertFalse(ai_types_question.should_show_guidance(["chatbot"]))
        self.assertFalse(training_data_question.should_show_guidance(["public_text"]))
        self.assertFalse(types_answer.requires_review)
        self.assertFalse(training_answer.requires_review)

        # Bias testing should not be required for low-risk AI
        visible_questions = self.checklist.get_visible_questions(completion)
        bias_question_visible = any(
            q.description == "Have you conducted bias testing?"
            for q in visible_questions
        )
        self.assertFalse(bias_question_visible)

        # Test high-risk AI (facial recognition) - recreate answers to trigger save logic
        models.Answer.objects.filter(
            completion=completion, question=ai_types_question
        ).delete()

        models.Answer.objects.filter(
            completion=completion, question=training_data_question
        ).delete()

        models.Answer.objects.create(
            completion=completion,
            question=ai_types_question,
            user=self.fixture.admin,
            answer_data=["facial_recognition"],
        )

        models.Answer.objects.create(
            completion=completion,
            question=training_data_question,
            user=self.fixture.admin,
            answer_data=["biometric"],
        )

        # Should trigger guidance and review
        self.assertTrue(ai_types_question.should_show_guidance(["facial_recognition"]))
        self.assertTrue(training_data_question.should_show_guidance(["biometric"]))

        # Get new answer objects
        types_answer = models.Answer.objects.get(
            completion=completion, question=ai_types_question
        )
        training_answer = models.Answer.objects.get(
            completion=completion, question=training_data_question
        )

        self.assertTrue(types_answer.requires_review)
        self.assertTrue(training_answer.requires_review)

        # Bias testing should now be required
        visible_questions = self.checklist.get_visible_questions(completion)
        bias_question_visible = any(
            q.description == "Have you conducted bias testing?"
            for q in visible_questions
        )
        self.assertTrue(bias_question_visible)

    def test_security_risk_assessment_multi_tier_workflow(self):
        """Test multi-tier security risk assessment with escalating requirements."""
        # Tier 1: Basic risk level
        risk_level_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="What is your project's risk level?",
            question_type=enums.QuestionTypes.SINGLE_SELECT,
            required=True,
            order=1,
            user_guidance="Risk level determines required security controls.",
            always_show_guidance=True,
        )

        # Add options for risk levels
        factories.QuestionOptionFactory(
            question=risk_level_question,
            label="Low - Internal tools, no sensitive data",
            order=1,
        )
        factories.QuestionOptionFactory(
            question=risk_level_question,
            label="Medium - Customer-facing, some sensitive data",
            order=2,
        )
        factories.QuestionOptionFactory(
            question=risk_level_question,
            label="High - Critical systems, highly sensitive data",
            order=3,
        )

        # Tier 2: Security controls (required for medium/high risk)
        security_controls_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Which security controls have you implemented?",
            question_type=enums.QuestionTypes.MULTI_SELECT,
            required=True,
            order=2,
            user_guidance="High-risk systems require additional security controls beyond basic authentication.",
            always_show_guidance=False,
            guidance_answer_value="High - Critical systems, highly sensitive data",
            guidance_operator="contains",  # Use contains to match the option text
            # Review if insufficient controls for risk level
            always_requires_review=False,
            review_answer_value=["basic_auth"],  # Basic auth alone is insufficient
            operator="equals",
        )

        # Security controls required for medium+ risk
        factories.QuestionDependencyFactory(
            question=security_controls_question,
            depends_on_question=risk_level_question,
            required_answer_value=[
                "Medium - Customer-facing, some sensitive data",
                "High - Critical systems, highly sensitive data",
            ],
            operator="in",
        )

        # Tier 3: Penetration testing (required for high risk only)
        pentest_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="When was your last penetration test?",
            question_type=enums.QuestionTypes.SINGLE_SELECT,
            required=True,
            order=3,
            user_guidance="High-risk systems require annual penetration testing by certified professionals.",
            always_show_guidance=False,
            guidance_answer_value=["never", "more_than_1_year"],
            guidance_operator="in",
            # Always review pentest timing for high-risk systems
            always_requires_review=True,
        )

        factories.QuestionDependencyFactory(
            question=pentest_question,
            depends_on_question=risk_level_question,
            required_answer_value=["High - Critical systems, highly sensitive data"],
            operator="equals",
        )

        # Tier 4: SOC compliance (required for high risk with insufficient recent pentests)
        soc_compliance_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Do you have current SOC 2 certification?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            order=4,
            user_guidance="SOC 2 certification may be required for high-risk systems without recent security testing.",
            always_show_guidance=False,
            guidance_answer_value=False,
            guidance_operator="equals",
            # Always review SOC compliance claims
            always_requires_review=True,
        )

        # Complex dependency: SOC required for high risk AND old/no pentests
        # This would need custom logic in a real implementation
        factories.QuestionDependencyFactory(
            question=soc_compliance_question,
            depends_on_question=risk_level_question,
            required_answer_value=["High - Critical systems, highly sensitive data"],
            operator="equals",
        )

        # Test the workflow
        completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.fixture.project
        )

        # Test low-risk path (minimal requirements)
        models.Answer.objects.create(
            completion=completion,
            question=risk_level_question,
            user=self.fixture.admin,
            answer_data=["Low - Internal tools, no sensitive data"],
        )

        visible_questions = self.checklist.get_visible_questions(completion)
        self.assertEqual(len(visible_questions), 1)  # Only risk level question

        # Test medium-risk path (adds security controls)
        models.Answer.objects.filter(
            completion=completion, question=risk_level_question
        ).delete()
        models.Answer.objects.create(
            completion=completion,
            question=risk_level_question,
            user=self.fixture.admin,
            answer_data=["Medium - Customer-facing, some sensitive data"],
        )

        visible_questions = self.checklist.get_visible_questions(completion)
        self.assertEqual(len(visible_questions), 2)  # Risk + security controls

        # Test high-risk path (adds pentest and potentially SOC)
        models.Answer.objects.filter(
            completion=completion, question=risk_level_question
        ).delete()
        models.Answer.objects.create(
            completion=completion,
            question=risk_level_question,
            user=self.fixture.admin,
            answer_data=["High - Critical systems, highly sensitive data"],
        )

        visible_questions = self.checklist.get_visible_questions(completion)
        question_descriptions = [q.description for q in visible_questions]

        self.assertIn("What is your project's risk level?", question_descriptions)
        self.assertIn(
            "Which security controls have you implemented?", question_descriptions
        )
        self.assertIn("When was your last penetration test?", question_descriptions)
        self.assertIn("Do you have current SOC 2 certification?", question_descriptions)

        # Test guidance triggers for high-risk systems
        self.assertTrue(
            security_controls_question.should_show_guidance(
                "High - Critical systems, highly sensitive data"
            )
        )

        # Answer security controls with insufficient protection
        models.Answer.objects.create(
            completion=completion,
            question=security_controls_question,
            user=self.fixture.admin,
            answer_data=["basic_auth"],
        )

        # Should trigger review due to insufficient controls
        security_answer = models.Answer.objects.get(
            completion=completion, question=security_controls_question
        )
        self.assertTrue(security_answer.requires_review)

        # Test pentest timing
        models.Answer.objects.create(
            completion=completion,
            question=pentest_question,
            user=self.fixture.admin,
            answer_data=["never"],
        )

        # Should show guidance for missing pentest
        self.assertTrue(pentest_question.should_show_guidance(["never"]))

        # Should always require review for high-risk systems
        pentest_answer = models.Answer.objects.get(
            completion=completion, question=pentest_question
        )
        self.assertTrue(pentest_answer.requires_review)

        # Complete SOC compliance question
        models.Answer.objects.create(
            completion=completion,
            question=soc_compliance_question,
            user=self.fixture.admin,
            answer_data=False,
        )

        # Should show guidance for missing SOC
        self.assertTrue(soc_compliance_question.should_show_guidance(False))

        # Should require review
        soc_answer = models.Answer.objects.get(
            completion=completion, question=soc_compliance_question
        )
        self.assertTrue(soc_answer.requires_review)

        # Verify completion
        completion.refresh_from_db()
        self.assertTrue(completion.is_completed)

        # All high-risk answers should require review
        review_count = models.Answer.objects.filter(
            completion=completion, requires_review=True
        ).count()
        self.assertEqual(review_count, 3)  # Security controls, pentest, SOC


@ddt
class DocumentationExampleValidationTest(test.APITransactionTestCase):
    """Validate that all documented examples work as described."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.checklist = factories.ChecklistFactory()

    def test_question_dependencies_documentation_example(self):
        """Test the exact example from question dependencies documentation."""
        # Create exactly as documented
        data_handling_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Will you be handling user data?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            order=1,
        )

        data_types_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="What types of user data will you collect?",
            question_type=enums.QuestionTypes.MULTI_SELECT,
            required=True,
            order=2,
        )

        # Create dependency exactly as documented
        factories.QuestionDependencyFactory(
            question=data_types_question,
            depends_on_question=data_handling_question,
            required_answer_value=True,
            operator="equals",
        )

        completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.fixture.project
        )

        # Test: initially only parent question visible
        visible_questions = self.checklist.get_visible_questions(completion)
        self.assertEqual(len(visible_questions), 1)
        self.assertEqual(visible_questions[0], data_handling_question)

        # Answer "Yes" to parent question
        models.Answer.objects.create(
            completion=completion,
            question=data_handling_question,
            user=self.fixture.admin,
            answer_data=True,
        )

        # Now both questions should be visible
        visible_questions = self.checklist.get_visible_questions(completion)
        self.assertEqual(len(visible_questions), 2)

        descriptions = [q.description for q in visible_questions]
        self.assertIn("Will you be handling user data?", descriptions)
        self.assertIn("What types of user data will you collect?", descriptions)

    def test_review_triggers_documentation_example(self):
        """Test the exact example from review triggers documentation."""
        # Create exactly as documented
        risk_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="What is your project's risk level?",
            question_type=enums.QuestionTypes.SINGLE_SELECT,
            required=True,
            review_answer_value=["high", "critical"],
            operator="in",
            always_requires_review=False,
        )

        completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.fixture.project
        )

        # Test high risk triggers review
        models.Answer.objects.create(
            completion=completion,
            question=risk_question,
            user=self.fixture.admin,
            answer_data=["high"],
        )

        answer = models.Answer.objects.get(
            completion=completion, question=risk_question
        )
        self.assertTrue(answer.requires_review)

        # Test low risk doesn't trigger review
        models.Answer.objects.filter(
            completion=completion, question=risk_question
        ).delete()
        models.Answer.objects.create(
            completion=completion,
            question=risk_question,
            user=self.fixture.admin,
            answer_data=["low"],
        )

        answer = models.Answer.objects.get(
            completion=completion, question=risk_question
        )
        self.assertFalse(answer.requires_review)

    def test_user_guidance_documentation_example(self):
        """Test the exact example from user guidance documentation."""
        # Create exactly as documented
        eu_data_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Will you be processing EU citizen data?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            user_guidance="Since you're processing EU data, you must comply with GDPR requirements. Please review our GDPR compliance checklist.",
            always_show_guidance=False,
            guidance_answer_value=True,
            guidance_operator="equals",
        )

        # Test guidance shows for Yes
        self.assertTrue(eu_data_question.should_show_guidance(True))

        # Test guidance hidden for No
        self.assertFalse(eu_data_question.should_show_guidance(False))

    def test_combined_guidance_and_review_documentation_example(self):
        """Test the combined guidance and review example from documentation."""
        # Create exactly as documented
        financial_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Will your application process financial transactions?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            # User guidance configuration
            user_guidance="Financial transaction processing requires PCI DSS compliance. Please review our payment processing guidelines.",
            always_show_guidance=False,
            guidance_answer_value=True,
            guidance_operator="equals",
            # Review trigger configuration (same condition)
            review_answer_value=True,
            operator="equals",
            always_requires_review=False,
        )

        completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.fixture.project
        )

        # Test "Yes" triggers both guidance and review
        models.Answer.objects.create(
            completion=completion,
            question=financial_question,
            user=self.fixture.admin,
            answer_data=True,
        )

        # Both guidance and review should trigger
        self.assertTrue(financial_question.should_show_guidance(True))

        answer = models.Answer.objects.get(
            completion=completion, question=financial_question
        )
        self.assertTrue(answer.requires_review)

        # Test "No" triggers neither
        models.Answer.objects.filter(
            completion=completion, question=financial_question
        ).delete()
        models.Answer.objects.create(
            completion=completion,
            question=financial_question,
            user=self.fixture.admin,
            answer_data=False,
        )

        self.assertFalse(financial_question.should_show_guidance(False))

        answer = models.Answer.objects.get(
            completion=completion, question=financial_question
        )
        self.assertFalse(answer.requires_review)

    def test_multi_condition_security_documentation_example(self):
        """Test the multi-condition security example from documentation."""
        # Create exactly as documented
        data_types_question = factories.QuestionFactory(
            checklist=self.checklist,
            description="Select all data types you'll be handling:",
            question_type=enums.QuestionTypes.MULTI_SELECT,
            required=True,
            # Guidance for any sensitive data
            user_guidance="You've selected sensitive data types. Additional security measures will be required.",
            always_show_guidance=False,
            guidance_answer_value=[
                "personal_data",
                "financial_data",
                "health_data",
                "confidential",
            ],
            guidance_operator="in",
            # Review trigger for high-risk combinations only
            review_answer_value=["financial_data", "health_data"],
            operator="in",
            always_requires_review=False,
        )

        completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.fixture.project
        )

        # Test personal data: guidance but no review
        self.assertTrue(data_types_question.should_show_guidance(["personal_data"]))

        models.Answer.objects.create(
            completion=completion,
            question=data_types_question,
            user=self.fixture.admin,
            answer_data=["personal_data"],
        )

        personal_answer = models.Answer.objects.get(
            completion=completion, question=data_types_question
        )
        self.assertFalse(personal_answer.requires_review)

        # Test financial data: both guidance and review
        self.assertTrue(data_types_question.should_show_guidance(["financial_data"]))

        models.Answer.objects.filter(
            completion=completion, question=data_types_question
        ).delete()
        models.Answer.objects.create(
            completion=completion,
            question=data_types_question,
            user=self.fixture.admin,
            answer_data=["financial_data"],
        )

        personal_answer = models.Answer.objects.get(
            completion=completion, question=data_types_question
        )
        self.assertTrue(personal_answer.requires_review)

        # Test public data: neither guidance nor review
        self.assertFalse(data_types_question.should_show_guidance(["public_data"]))

        models.Answer.objects.filter(
            completion=completion, question=data_types_question
        ).delete()
        models.Answer.objects.create(
            completion=completion,
            question=data_types_question,
            user=self.fixture.admin,
            answer_data=["public_data"],
        )

        personal_answer = models.Answer.objects.get(
            completion=completion, question=data_types_question
        )
        self.assertFalse(personal_answer.requires_review)


@ddt
class RealWorldIntegrationPatternsTest(test.APITransactionTestCase):
    """Test patterns for real-world integration scenarios."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    def test_project_onboarding_checklist_pattern(self):
        """Test typical project onboarding checklist integration pattern."""
        # Create project metadata checklist
        onboarding_checklist = factories.ChecklistFactory(
            name="Project Onboarding Checklist",
            checklist_type=enums.ChecklistTypes.PROJECT_METADATA,
        )

        # Basic project info questions
        purpose_question = factories.QuestionFactory(
            checklist=onboarding_checklist,
            description="What is the purpose of this project?",
            question_type=enums.QuestionTypes.TEXT_AREA,
            required=True,
            order=1,
        )

        category_question = factories.QuestionFactory(
            checklist=onboarding_checklist,
            description="Project category:",
            question_type=enums.QuestionTypes.SINGLE_SELECT,
            required=True,
            order=2,
        )

        # Risk assessment questions
        risk_question = factories.QuestionFactory(
            checklist=onboarding_checklist,
            description="Expected risk level:",
            question_type=enums.QuestionTypes.SINGLE_SELECT,
            required=True,
            order=3,
            user_guidance="High-risk projects require additional approvals and security reviews.",
            always_show_guidance=False,
            guidance_answer_value=["high"],
            guidance_operator="equals",
            always_requires_review=False,
            review_answer_value=["high"],
            operator="equals",
        )

        # Compliance requirements (conditional)
        compliance_question = factories.QuestionFactory(
            checklist=onboarding_checklist,
            description="Which compliance frameworks apply?",
            question_type=enums.QuestionTypes.MULTI_SELECT,
            required=False,
            order=4,
            user_guidance="Multiple compliance frameworks may require coordinated implementation.",
            always_show_guidance=False,
            guidance_answer_value=2,  # More than 1 framework
            guidance_operator="greater_than",  # Note: would need custom operator
        )

        # Make compliance conditional on high risk
        factories.QuestionDependencyFactory(
            question=compliance_question,
            depends_on_question=risk_question,
            required_answer_value=["high"],
            operator="equals",
        )

        # Create completion (simulating project creation)
        completion = models.ChecklistCompletion.objects.create(
            checklist=onboarding_checklist, scope=self.fixture.project
        )

        # Simulate project manager filling out basic info
        models.Answer.objects.create(
            completion=completion,
            question=purpose_question,
            user=self.fixture.manager,
            answer_data="Develop customer-facing web application for order management",
        )

        models.Answer.objects.create(
            completion=completion,
            question=category_question,
            user=self.fixture.manager,
            answer_data=["production"],
        )

        # Initially low risk
        models.Answer.objects.create(
            completion=completion,
            question=risk_question,
            user=self.fixture.manager,
            answer_data=["low"],
        )

        # Compliance question should not be visible for low risk
        visible_questions = onboarding_checklist.get_visible_questions(completion)
        compliance_visible = any(
            "compliance frameworks" in q.description.lower() for q in visible_questions
        )
        self.assertFalse(compliance_visible)

        # Project scope changes - now high risk
        models.Answer.objects.filter(
            completion=completion, question=risk_question
        ).delete()
        models.Answer.objects.create(
            completion=completion,
            question=risk_question,
            user=self.fixture.manager,
            answer_data=["high"],
        )

        # Compliance question should now be visible
        visible_questions = onboarding_checklist.get_visible_questions(completion)
        compliance_visible = any(
            "compliance frameworks" in q.description.lower() for q in visible_questions
        )
        self.assertTrue(compliance_visible)

        # Should show guidance for high risk
        self.assertTrue(risk_question.should_show_guidance(["high"]))

        # Should trigger review for high risk
        risk_answer = models.Answer.objects.get(
            completion=completion, question=risk_question
        )
        self.assertTrue(risk_answer.requires_review)

        # Complete compliance question
        models.Answer.objects.create(
            completion=completion,
            question=compliance_question,
            user=self.fixture.manager,
            answer_data=["gdpr", "pci_dss"],
        )

        # Verify completion status
        completion.refresh_from_db()
        self.assertTrue(completion.is_completed)  # All required questions answered

    def test_proposal_compliance_checklist_pattern(self):
        """Test proposal compliance checklist integration pattern."""
        # Create compliance checklist
        compliance_checklist = factories.ChecklistFactory(
            name="Proposal Compliance Checklist",
            checklist_type=enums.ChecklistTypes.PROPOSAL_COMPLIANCE,
        )

        # Ethics questions
        ethics_question = factories.QuestionFactory(
            checklist=compliance_checklist,
            description="Does your research involve human subjects?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            order=1,
            user_guidance="Research involving human subjects requires IRB approval.",
            always_show_guidance=False,
            guidance_answer_value=True,
            guidance_operator="equals",
            always_requires_review=False,
            review_answer_value=True,
            operator="equals",
        )

        # IRB approval (conditional)
        irb_question = factories.QuestionFactory(
            checklist=compliance_checklist,
            description="Have you obtained IRB approval?",
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            order=2,
            user_guidance="IRB approval is mandatory before research can begin. Contact Research Compliance office for assistance.",
            always_show_guidance=False,
            guidance_answer_value=False,
            guidance_operator="equals",
            always_requires_review=True,  # Always review IRB claims
        )

        factories.QuestionDependencyFactory(
            question=irb_question,
            depends_on_question=ethics_question,
            required_answer_value=True,
            operator="equals",
        )

        # Budget compliance
        budget_question = factories.QuestionFactory(
            checklist=compliance_checklist,
            description="Total project budget:",
            question_type=enums.QuestionTypes.SINGLE_SELECT,
            required=True,
            order=3,
            user_guidance="Large budgets require additional financial oversight and approval.",
            always_show_guidance=False,
            guidance_answer_value=["over_100k"],
            guidance_operator="equals",
            always_requires_review=False,
            review_answer_value=["over_100k"],
            operator="equals",
        )

        # Create completion (simulating proposal creation)
        completion = models.ChecklistCompletion.objects.create(
            checklist=compliance_checklist,
            scope=self.fixture.project,  # Using project as proxy for proposal
        )

        # Test no human subjects path
        models.Answer.objects.create(
            completion=completion,
            question=ethics_question,
            user=self.fixture.admin,
            answer_data=False,
        )

        models.Answer.objects.create(
            completion=completion,
            question=budget_question,
            user=self.fixture.admin,
            answer_data=["under_10k"],
        )

        # IRB question should not be visible
        visible_questions = compliance_checklist.get_visible_questions(completion)
        irb_visible = any("IRB approval" in q.description for q in visible_questions)
        self.assertFalse(irb_visible)

        # No guidance or review triggers for low-risk scenario
        ethics_answer = models.Answer.objects.get(
            completion=completion, question=ethics_question
        )
        budget_answer = models.Answer.objects.get(
            completion=completion, question=budget_question
        )

        self.assertFalse(ethics_question.should_show_guidance(False))
        self.assertFalse(budget_question.should_show_guidance(["under_10k"]))
        self.assertFalse(ethics_answer.requires_review)
        self.assertFalse(budget_answer.requires_review)

        # Test high-risk scenario
        models.Answer.objects.filter(
            completion=completion, question=ethics_question
        ).delete()
        models.Answer.objects.create(
            completion=completion,
            question=ethics_question,
            user=self.fixture.admin,
            answer_data=True,
        )

        models.Answer.objects.filter(
            completion=completion, question=budget_question
        ).delete()
        models.Answer.objects.create(
            completion=completion,
            question=budget_question,
            user=self.fixture.admin,
            answer_data=["over_100k"],
        )

        # IRB question should now be visible
        visible_questions = compliance_checklist.get_visible_questions(completion)
        irb_visible = any("IRB approval" in q.description for q in visible_questions)
        self.assertTrue(irb_visible)

        # Should trigger guidance and review
        ethics_answer = models.Answer.objects.get(
            completion=completion, question=ethics_question
        )
        budget_answer = models.Answer.objects.get(
            completion=completion, question=budget_question
        )

        self.assertTrue(ethics_question.should_show_guidance(True))
        self.assertTrue(budget_question.should_show_guidance(["over_100k"]))
        self.assertTrue(ethics_answer.requires_review)
        self.assertTrue(budget_answer.requires_review)

        # Complete IRB question
        models.Answer.objects.create(
            completion=completion,
            question=irb_question,
            user=self.fixture.admin,
            answer_data=True,
        )

        irb_answer = models.Answer.objects.get(
            completion=completion, question=irb_question
        )
        self.assertTrue(irb_answer.requires_review)  # Always requires review

        # Verify completion
        completion.refresh_from_db()
        self.assertTrue(completion.is_completed)

        # Multiple answers should require review
        review_count = models.Answer.objects.filter(
            completion=completion, requires_review=True
        ).count()
        self.assertEqual(review_count, 3)  # Ethics, budget, and IRB
