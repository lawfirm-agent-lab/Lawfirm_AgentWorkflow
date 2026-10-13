import copy
import unittest
from src.store.db import Store


def entry(i=1):
    return {"id": i, "docket": "https://www.courtlistener.com/api/rest/v4/dockets/73132799/", "description": "Original", "date_filed": "2026-10-08", "recap_documents": [{"id": 10, "is_available": False}]}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def apply(self, rows):
        return self.store.apply(self.store.start(73132799), rows)

    def test_baseline_and_repeat(self):
        self.assertTrue(self.apply([entry()])["baseline"])
        result = self.apply([entry(), entry()])
        self.assertEqual((result["added"], result["modified"]), (0, 0))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0], 0)

    def test_added_modified_and_history(self):
        self.apply([entry()])
        changed = entry()
        changed["description"] = "Updated"
        result = self.apply([changed, entry(2)])
        self.assertEqual((result["added"], result["modified"]), (1, 1))
        row = self.store.conn.execute("SELECT * FROM changes WHERE kind='modified'").fetchone()
        self.assertIn("Original", row["before_json"])
        self.assertIn("Updated", row["after_json"])
        self.assertEqual(self.apply([entry(2), changed])["modified"], 0)

    def test_metadata_and_document_availability(self):
        row = entry()
        row["recap_documents"].append({"id": 11, "is_available": False})
        self.apply([row])
        changed = copy.deepcopy(row)
        changed["date_modified"] = "different indexing time"
        changed["recap_documents"].reverse()
        self.assertEqual(self.apply([changed])["modified"], 0)
        changed["recap_documents"][0]["is_available"] = True
        self.assertEqual(self.apply([changed])["modified"], 1)

    def test_invalid_snapshot_and_failed_run(self):
        self.apply([entry()])
        run = self.store.start(73132799)
        with self.assertRaises(ValueError):
            self.store.apply(run, [entry(2), {"id": 3}])
        self.store.fail(run, "invalid snapshot")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 1)
        self.assertEqual(self.store.conn.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0], "failed")
        self.assertEqual(self.apply([entry()])["added"], 0)

    def test_missing_retained_and_conflicting_duplicate(self):
        self.apply([entry()])
        self.assertEqual(self.apply([])["missing"], 1)
        self.assertEqual(self.apply([entry()])["added"], 0)
        bad = entry()
        bad["description"] = "Conflict"
        with self.assertRaises(ValueError):
            self.apply([entry(), bad])

    def test_empty_initial_snapshot_rejected(self):
        with self.assertRaises(ValueError):
            self.apply([])
        self.assertFalse(self.store.initialized(73132799))


if __name__ == "__main__":
    unittest.main()
