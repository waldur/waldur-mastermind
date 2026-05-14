import unittest

from waldur_core.logging.diff import compute_collection_diff


def _rule(pk, **kwargs):
    base = {
        "pk": pk,
        "direction": "ingress",
        "protocol": "tcp",
        "from_port": 80,
        "to_port": 80,
        "cidr": "0.0.0.0/0",
    }
    base.update(kwargs)
    return base


COMPARE_FIELDS = ["direction", "protocol", "from_port", "to_port", "cidr"]


def _serialize(rule):
    return {
        k: rule[k] for k in ["direction", "protocol", "from_port", "to_port", "cidr"]
    }


def _diff(old, new):
    return compute_collection_diff(
        old,
        new,
        identity_key=lambda r: r.get("pk"),
        compare_fields=COMPARE_FIELDS,
        serialize=_serialize,
    )


class ComputeCollectionDiffTest(unittest.TestCase):
    def test_pure_add(self):
        result = _diff([], [_rule(None, from_port=22, to_port=22)])
        self.assertEqual(result["summary"], {"added": 1, "removed": 0, "modified": 0})
        self.assertEqual(len(result["added"]), 1)
        self.assertEqual(result["added"][0]["from_port"], 22)
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["modified"], [])

    def test_pure_remove(self):
        result = _diff([_rule(1)], [])
        self.assertEqual(result["summary"], {"added": 0, "removed": 1, "modified": 0})
        self.assertEqual(len(result["removed"]), 1)

    def test_in_place_modify_single_field(self):
        old = [_rule(1, to_port=80)]
        new = [_rule(1, to_port=443)]
        result = _diff(old, new)
        self.assertEqual(result["summary"], {"added": 0, "removed": 0, "modified": 1})
        entry = result["modified"][0]
        self.assertEqual(entry["changed_fields"], ["to_port"])
        self.assertEqual(entry["old"]["to_port"], 80)
        self.assertEqual(entry["new"]["to_port"], 443)

    def test_in_place_modify_multi_field(self):
        old = [_rule(1, from_port=80, to_port=80, cidr="0.0.0.0/0")]
        new = [_rule(1, from_port=8000, to_port=9000, cidr="10.0.0.0/8")]
        result = _diff(old, new)
        self.assertEqual(result["summary"]["modified"], 1)
        self.assertEqual(
            sorted(result["modified"][0]["changed_fields"]),
            ["cidr", "from_port", "to_port"],
        )

    def test_noop(self):
        old = [_rule(1)]
        new = [_rule(1)]
        result = _diff(old, new)
        self.assertEqual(result["summary"], {"added": 0, "removed": 0, "modified": 0})

    def test_mixed(self):
        old = [_rule(1, to_port=80), _rule(2, from_port=22, to_port=22)]
        new = [
            _rule(1, to_port=443),  # modified
            _rule(None, from_port=53, to_port=53),  # added
        ]
        # rule 2 absent in new → removed
        result = _diff(old, new)
        self.assertEqual(result["summary"], {"added": 1, "removed": 1, "modified": 1})

    def test_empty_inputs(self):
        result = _diff([], [])
        self.assertEqual(result["summary"], {"added": 0, "removed": 0, "modified": 0})
        self.assertEqual(result["added"], [])
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["modified"], [])

    def test_identity_none_treated_as_add(self):
        # Multiple entries without identity should all count as added,
        # never accidentally matched together.
        new = [_rule(None, from_port=1), _rule(None, from_port=2)]
        result = _diff([], new)
        self.assertEqual(result["summary"]["added"], 2)

    def test_compare_fields_scoped(self):
        # A field outside compare_fields differing should not flag modified.
        old = [_rule(1, description="old")]
        new = [_rule(1, description="new")]
        # "description" is not in COMPARE_FIELDS
        result = _diff(old, new)
        self.assertEqual(result["summary"]["modified"], 0)


if __name__ == "__main__":
    unittest.main()
