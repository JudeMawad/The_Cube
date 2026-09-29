import unittest

from core.response_options import approved_response_options


class ApprovedOptionsTests(unittest.TestCase):
    def test_canonical_first_validated_deduplicated_and_never_normalized(self):
        alternatives = ["Alternative.", "Canonical.", None, "", " \n", "x" * 501,
                        "Alternative.", " Spaced. ", {"response": "No"}, "x" * 500]
        self.assertEqual(approved_response_options("Canonical.", alternatives),
                         ["Canonical.", "Alternative.", " Spaced. ", "x" * 500])
        self.assertEqual(len(alternatives), 10)

    def test_missing_or_malformed_options_keep_only_canonical(self):
        for alternatives in (None, "Unapproved string", {"response": "No"}, 1):
            self.assertEqual(approved_response_options("Canonical.", alternatives), ["Canonical."])
        for canonical in (None, "", " \t", "x" * 501):
            self.assertEqual(approved_response_options(canonical, ["Alternative."]), [])
