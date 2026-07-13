import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from workflow_engine import ConfigError, WorkflowEngine, WorkflowError, render_workflow_ui, validate_config


CONFIG = {
    "version": 1,
    "workflows": [
        {
            "id": "case-review",
            "title": "Case Review",
            "description": "Review and approve operational cases.",
            "fields": [
                {"key": "caseName", "label": "Case name", "type": "text", "required": True},
                {"key": "priority", "label": "Priority", "type": "select", "options": ["Low", "High"], "default": "Low"},
                {"key": "amount", "label": "Amount", "type": "number"},
                {"key": "dueDate", "label": "Due date", "type": "date"},
                {"key": "approved", "label": "Approved", "type": "boolean", "default": False},
            ],
            "states": ["draft", {"id": "review", "label": "In review"}, "approved"],
            "transitions": [
                {"id": "submit-review", "label": "Submit", "from": "draft", "to": "review"},
                {"id": "approve", "label": "Approve", "from": ["review"], "to": "approved"},
            ],
            "seeds": [{"id": "case-seed", "state": "draft", "caseName": "Seed case", "priority": "High"}],
        }
    ],
    "resources": [
        {"name": "case_records", "type": "table", "workflowId": "case-review"},
        {"name": "active_case_view", "type": "view", "workflowId": "case-review"},
    ],
}


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "workflows.json").write_text(json.dumps(CONFIG), encoding="utf-8")
        self.database = self.root / "database.sqlite"
        self.engine = WorkflowEngine(self.root, self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def test_initializes_seeds_resources_and_does_not_duplicate(self):
        self.assertEqual(self.engine.dashboard()["totalRecords"], 1)
        WorkflowEngine(self.root, self.database)
        self.assertEqual(self.engine.dashboard()["totalRecords"], 1)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM case_records").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM active_case_view").fetchone()[0], 1)

    def test_reinitialization_is_byte_idempotent(self):
        initialized_database = self.database.read_bytes()
        WorkflowEngine(self.root, self.database)
        self.assertEqual(self.database.read_bytes(), initialized_database)

    def test_crud_validation_filtering_and_optimistic_version(self):
        with self.assertRaises(WorkflowError) as invalid:
            self.engine.create("case-review", {"priority": "Urgent"}, "operator@example.com")
        self.assertEqual(invalid.exception.status, 422)

        record = self.engine.create("case-review", {"caseName": "Quarterly review", "amount": "12.5"}, "operator@example.com")
        self.assertEqual(record["version"], 1)
        self.assertEqual(record["values"]["priority"], "Low")
        self.assertEqual(record["values"]["amount"], 12.5)
        found = self.engine.list_records("case-review", search="quarterly", filters={"priority": "Low"})
        self.assertEqual(found["total"], 1)

        updated = self.engine.update("case-review", record["id"], {"priority": "High"}, 1, "reviewer")
        self.assertEqual(updated["version"], 2)
        with self.assertRaises(WorkflowError) as conflict:
            self.engine.update("case-review", record["id"], {"priority": "Low"}, 1, "reviewer")
        self.assertEqual(conflict.exception.status, 409)
        with sqlite3.connect(self.database) as connection:
            mirrored = connection.execute("SELECT version,data_json FROM case_records WHERE workflow_record_id=?", (record["id"],)).fetchone()
        self.assertEqual(mirrored[0], 2)
        self.assertEqual(json.loads(mirrored[1])["priority"], "High")

    def test_transitions_delete_and_attributed_hash_chained_audit(self):
        record = self.engine.create("case-review", {"caseName": "Transition me"}, "creator")
        with self.assertRaises(WorkflowError):
            self.engine.transition("case-review", record["id"], "approve", 1, "approver")
        record = self.engine.transition("case-review", record["id"], "submit-review", 1, "reviewer")
        record = self.engine.transition("case-review", record["id"], "approve", 2, "approver")
        events = self.engine.events("case-review", record["id"])
        self.assertEqual([event["actor"] for event in events], ["creator", "reviewer", "approver"])
        self.assertIsNone(events[0]["previousHash"])
        self.assertEqual(events[1]["previousHash"], events[0]["eventHash"])
        self.assertTrue(self.engine.verify_audit_chain()["valid"])

        self.engine.delete("case-review", record["id"], 3, "records-admin")
        with self.assertRaises(WorkflowError):
            self.engine.get("case-review", record["id"])
        self.assertEqual(self.engine.events("case-review", record["id"])[-1]["eventType"], "deleted")
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM case_records WHERE workflow_record_id=?", (record["id"],)).fetchone()[0], 0)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE workflow_events SET actor='tampered' WHERE record_id=?", (record["id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM workflow_events WHERE record_id=?", (record["id"],))

    def test_dispatch_contract_csv_status_and_ui(self):
        definitions = self.engine.dispatch("GET", "/api/workflows").body
        self.assertEqual(definitions["workflows"][0]["id"], "case-review")
        created_response = self.engine.dispatch(
            "POST", "/api/workflows/case-review/records", payload={"values": {"caseName": "API case"}},
            headers={"X-Actor": "api-user"},
        )
        self.assertEqual(created_response.status, 201)
        record = created_response.body["record"]
        patched = self.engine.dispatch(
            "PATCH", f"/api/workflows/case-review/records/{record['id']}",
            payload={"version": 1, "values": {"priority": "High"}}, headers={"X-Actor": "api-user"},
        )
        self.assertEqual(patched.body["record"]["version"], 2)
        listed = self.engine.dispatch("GET", "/api/workflows/case-review/records", {"field.priority": ["High"]}).body
        self.assertGreaterEqual(listed["total"], 1)
        csv_response = self.engine.dispatch("GET", "/api/workflows/case-review/export.csv")
        self.assertIn("caseName", csv_response.body)
        status = self.engine.dispatch("GET", "/api/product/status").body
        self.assertEqual(status["status"], "ok")
        self.assertTrue(status["audit"]["valid"])
        page = render_workflow_ui("Test Product", "case-review")
        self.assertIn("Operations workspace", page)
        self.assertIn("/api/workflows", page)

    def test_external_resource_collision_is_preserved(self):
        other = self.root / "external.sqlite"
        with sqlite3.connect(other) as connection:
            connection.execute("CREATE TABLE case_records(source_key TEXT NOT NULL)")
            connection.execute("INSERT INTO case_records VALUES('source-data')")
        engine = WorkflowEngine(self.root, other)
        with sqlite3.connect(other) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(case_records)")]
            self.assertEqual(columns, ["source_key"])
            self.assertEqual(connection.execute("SELECT source_key FROM case_records").fetchone()[0], "source-data")
        resource = next(item for item in engine.status()["resources"] if item["name"] == "case_records")
        self.assertTrue(resource["ready"])
        self.assertFalse(resource["runtimeOwned"])


class ConfigValidationTests(unittest.TestCase):
    def test_rejects_unsafe_and_invalid_contracts(self):
        bad = json.loads(json.dumps(CONFIG))
        bad["resources"][0]["name"] = "unsafe; DROP TABLE x"
        with self.assertRaises(ConfigError):
            validate_config(bad)
        bad = json.loads(json.dumps(CONFIG))
        bad["workflows"][0]["transitions"][0]["to"] = "missing"
        with self.assertRaises(ConfigError):
            validate_config(bad)
        bad = json.loads(json.dumps(CONFIG))
        bad["workflows"][0]["fields"][0]["type"] = "executable"
        with self.assertRaises(ConfigError):
            validate_config(bad)


if __name__ == "__main__":
    unittest.main()
