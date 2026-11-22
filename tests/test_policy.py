import unittest
from pathlib import PurePosixPath

from machina_deid.policy import PathMap, Rulebook


class RulebookTests(unittest.TestCase):
    def test_aliases_consistent_without_matching_engineering_tokens(self):
        policy = Rulebook.from_dict({"rules": [
            {"source": "Acme Engineering", "aliases": ["Acme"], "replacement": "Cedar Works", "ignore_case": True},
            {"source": "Works", "replacement": "Mills"},
        ]})
        self.assertEqual(
            policy.replace("ACME Engineering / acme_X / ACME50 / Works"),
            "Cedar Works / Cedar Works_X / ACME50 / Mills",
        )
        self.assertEqual(policy.map_path("Acme/ACME-20.step"), PurePosixPath("Cedar Works/Cedar Works-20.step"))

    def test_keyed_pseudonyms_use_the_identity_and_are_stable(self):
        data = {"rules": [{"source": "ACME", "id": "manufacturer", "category": "Company"}]}
        alias_data = {"rules": [{"source": "Acme LLC", "id": "manufacturer", "category": "Company"}]}
        result = Rulebook.from_dict(data, key="fixture key").replace("ACME")
        self.assertRegex(result, r"^Company-[a-f0-9]{16}$")
        self.assertEqual(result, Rulebook.from_dict(alias_data, key="fixture key").replace("Acme LLC"))
        self.assertNotEqual(result, Rulebook.from_dict(data, key="different key").replace("ACME"))

    def test_ambiguous_identity_rules_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "distinct identities"):
            Rulebook.from_dict({"rules": [
                {"source": "ACME", "ignore_case": True, "replacement": "Cedar"},
                {"source": "Acme", "replacement": "Spruce", "id": "another"},
            ]})


class PathMapTests(unittest.TestCase):
    def setUp(self):
        self.policy = Rulebook.from_dict({"rules": [
            {"source": "ACME", "replacement": "Cedar", "scope": "path"},
            {"source": "alice", "replacement": "Jordan"},
            {"source": "step", "replacement": "walking"},
        ]})
        self.paths = PathMap.build(
            ["ACME/Plate ACME.step", "Design/assembly.SLDASM", "Design/bolt.step"], self.policy
        )

    def test_rewrites_a_relative_reference_and_preserves_extension_and_slashes(self):
        self.assertEqual(self.paths["ACME/Plate ACME.step"], PurePosixPath("Cedar/Plate Cedar.step"))
        value = 'Uses "..\\ACME\\Plate ACME.step" at this step.'
        result = self.paths.replace(value, source_path="Design/assembly.SLDASM")
        self.assertEqual(result, 'Uses "..\\Cedar\\Plate Cedar.step" at this walking.')
        self.assertEqual(self.paths.replace("./bolt.step", "Design/assembly.SLDASM"), "./bolt.step")

    def test_recognizes_unique_bare_names_and_absolute_references(self):
        self.assertEqual(self.paths.replace("plate acme.STEP"), "Plate Cedar.step")
        self.assertEqual(
            self.paths.replace(r"D:\alice\ACME\Plate ACME.step"), r"D:\Jordan\Cedar\Plate Cedar.step"
        )

    def test_preserves_case_sensitive_paths_and_detects_rename_collisions(self):
        paths = PathMap.build(["File.step", "FILE.step"], Rulebook())
        self.assertEqual(paths["FILE.step"], PurePosixPath("FILE.step"))
        with self.assertRaisesRegex(ValueError, "collide"):
            PathMap.build(["ACME/one.step", "Cedar/two.step"], self.policy)
        with self.assertRaisesRegex(ValueError, "collide"):
            PathMap.build(["ACME", "Cedar/two.step"], self.policy)

    def test_rejects_replacements_that_would_escape_the_output(self):
        policy = Rulebook.from_dict({"rules": [{"source": "ACME", "replacement": "../../other"}]})
        with self.assertRaisesRegex(ValueError, "invalid path component"):
            PathMap.build(["ACME/file.step"], policy)


if __name__ == "__main__":
    unittest.main()
