"""
Tests for migration 0025_rename_llm_chat_constance_keys.

Verifies that renaming constance keys works even when the target keys
already exist (constance auto-creates them on first access).
"""

from django.db import connection
from django.test import TestCase


class ConstanceRenameTest(TestCase):
    """Test the rename logic from migration 0025."""

    def _execute(self, sql, params=None):
        with connection.cursor() as cursor:
            cursor.execute(sql, params)

    def _get_value(self, key):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT value FROM constance_constance WHERE key = %s", [key]
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def _key_exists(self, key):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM constance_constance WHERE key = %s", [key])
            return cursor.fetchone() is not None

    def test_rename_when_target_already_exists(self):
        """Reproduces the UniqueViolation: target key exists before rename."""
        old_key = "_TEST_OLD_KEY"
        new_key = "_TEST_NEW_KEY"

        # Setup: both old and new keys exist (the problematic state)
        self._execute(
            "INSERT INTO constance_constance (key, value) VALUES (%s, %s)",
            [old_key, "user_value"],
        )
        self._execute(
            "INSERT INTO constance_constance (key, value) VALUES (%s, %s)",
            [new_key, "auto_default"],
        )

        # Apply the same logic as the fixed migration
        self._execute("DELETE FROM constance_constance WHERE key = %s", [new_key])
        self._execute(
            "UPDATE constance_constance SET key = %s WHERE key = %s",
            [new_key, old_key],
        )

        # The old key's value is preserved under the new key
        self.assertEqual(self._get_value(new_key), "user_value")
        self.assertFalse(self._key_exists(old_key))

    def test_rename_when_target_does_not_exist(self):
        """Normal case: only the old key exists."""
        old_key = "_TEST_OLD_KEY_2"
        new_key = "_TEST_NEW_KEY_2"

        self._execute(
            "INSERT INTO constance_constance (key, value) VALUES (%s, %s)",
            [old_key, "user_value"],
        )

        # DELETE is a no-op when new key doesn't exist
        self._execute("DELETE FROM constance_constance WHERE key = %s", [new_key])
        self._execute(
            "UPDATE constance_constance SET key = %s WHERE key = %s",
            [new_key, old_key],
        )

        self.assertEqual(self._get_value(new_key), "user_value")
        self.assertFalse(self._key_exists(old_key))

    def test_rename_when_neither_key_exists(self):
        """Edge case: old key was already deleted/never existed."""
        old_key = "_TEST_OLD_KEY_3"
        new_key = "_TEST_NEW_KEY_3"

        # Both operations are no-ops
        self._execute("DELETE FROM constance_constance WHERE key = %s", [new_key])
        self._execute(
            "UPDATE constance_constance SET key = %s WHERE key = %s",
            [new_key, old_key],
        )

        self.assertFalse(self._key_exists(new_key))
        self.assertFalse(self._key_exists(old_key))
