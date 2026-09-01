"""Regression tests for the order-spec handoff; no broker or quote connection is used."""
import ast
from pathlib import Path
import unittest


def load_order_spec_builder():
    source_path = Path(__file__).with_name("direction_advisor_service.py")
    module = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    function = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "do_order"
    )
    namespace = {
        "_analyze": lambda code, direction, market, instrument: {
            "ok": True,
            "name": "騰訊控股",
            "contract_size": 100,
            "best": {
                "strategy": "看升 Call Spread",
                "expiry": "2026-09-25",
                "dte": 24,
                "win_rate": 62.5,
                "legs": [
                    {"futu_code": "HK.TCHC", "action": "買入", "cp": "C", "strike": 500, "price": 3.2},
                    {"futu_code": "HK.TCHD", "action": "賣出", "cp": "C", "strike": 520, "price": 1.4},
                ],
            },
            "alternatives": [],
        },
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace["do_order"], function


class AdvisorOrderSpecTests(unittest.TestCase):
    def setUp(self):
        self.do_order, self.function = load_order_spec_builder()

    def test_order_returns_a_parent_handoff_spec_without_direct_trade(self):
        result = self.do_order({"code": "700", "dir": "up", "idx": 0, "qty": 2})

        self.assertTrue(result["ok"])
        self.assertEqual(result["type"], "advisor_place_order")
        self.assertEqual(result["spec"]["stock"], "00700")
        self.assertEqual(result["spec"]["qty"], 2)
        self.assertEqual(result["spec"]["legs"][0]["action"], "買入")
        self.assertEqual(result["spec"]["legs"][1]["action"], "賣出")
        self.assertNotIn("real", result)
        self.assertNotIn("confirm", result)
        called_names = {
            node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(self.function)
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
        }
        self.assertNotIn("place_order", called_names)
        self.assertNotIn("open_position", called_names)

    def test_order_builder_rejects_invalid_quantity_before_any_handoff(self):
        result = self.do_order({"code": "700", "dir": "up", "qty": 501})
        self.assertFalse(result["ok"])
        self.assertIn("1-500", result["error"])


if __name__ == "__main__":
    unittest.main()
