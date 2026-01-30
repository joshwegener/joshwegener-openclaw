import unittest

from scripts import board_orchestrator as bo


class TestDependsParsing(unittest.TestCase):
    def test_depends_on_parses_hash_ids(self) -> None:
        desc = "Depends on: #30, #31\n"
        self.assertEqual(bo.parse_depends_on(desc), [30, 31])

    def test_dependencies_alias_is_supported(self) -> None:
        desc = "Dependencies: #30\n"
        self.assertEqual(bo.parse_depends_on(desc), [30])

    def test_dependency_singular_is_supported(self) -> None:
        desc = "Dependency: 30\n"
        self.assertEqual(bo.parse_depends_on(desc), [30])

    def test_depends_on_whitespace_separated(self) -> None:
        desc = "Depends on: #30 #31 #32\n"
        self.assertEqual(bo.parse_depends_on(desc), [30, 31, 32])


if __name__ == "__main__":
    unittest.main()
