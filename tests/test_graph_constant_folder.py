import importlib.util
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
	"graph_constant_folder",
	PACKAGE_ROOT / "graph_constant_folder.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def make_prompt(index):
	return {
		"default": {"class_type": "Source", "inputs": {}},
		"route3": {"class_type": "Source", "inputs": {}},
		"route5": {"class_type": "Source", "inputs": {}},
		"switch": {
			"class_type": "SparknightLazyFallbackSwitch",
			"inputs": {
				"index": index,
				"default": ["default", 0],
				"values.value3": ["route3", 0],
				"values.value5": ["route5", 0],
			},
		},
		"output": {
			"class_type": "Output",
			"inputs": {"model": ["switch", 0]},
		},
	}


class GraphConstantFolderTests(unittest.TestCase):
	def test_folds_selected_autogrow_route(self):
		result = MODULE._handler({
			"prompt": make_prompt(3),
			"partial_execution_targets": ["output"],
		})

		self.assertEqual(result["prompt"]["output"]["inputs"]["model"], ["route3", 0])
		self.assertIn("route3", result["prompt"])
		self.assertNotIn("route5", result["prompt"])
		self.assertNotIn("default", result["prompt"])
		self.assertNotIn("switch", result["prompt"])

	def test_folds_to_default_when_selected_route_is_absent(self):
		result = MODULE._handler({
			"prompt": make_prompt(4),
			"partial_execution_targets": ["output"],
		})

		self.assertEqual(result["prompt"]["output"]["inputs"]["model"], ["default", 0])
		self.assertIn("default", result["prompt"])
		self.assertNotIn("route3", result["prompt"])
		self.assertNotIn("route5", result["prompt"])

	def test_folds_route_selected_by_linked_primitive(self):
		prompt = make_prompt(["index", 0])
		prompt["index"] = {
			"class_type": "PrimitiveInt",
			"inputs": {"value": 3},
		}

		result = MODULE._handler({
			"prompt": prompt,
			"partial_execution_targets": ["output"],
		})

		self.assertEqual(result["prompt"]["output"]["inputs"]["model"], ["route3", 0])
		self.assertNotIn("route5", result["prompt"])

	def test_folds_static_fallback_switch(self):
		prompt = make_prompt(3)
		prompt["switch"] = {
			"class_type": "SparknightLazyFallbackSwitchStatic",
			"inputs": {
				"index": 3,
				"default": ["default", 0],
				"value3": ["route3", 0],
				"value5": ["route5", 0],
			},
		}

		result = MODULE._handler({
			"prompt": prompt,
			"partial_execution_targets": ["output"],
		})

		self.assertEqual(result["prompt"]["output"]["inputs"]["model"], ["route3", 0])
		self.assertNotIn("route5", result["prompt"])

	def test_does_not_fold_missing_route_without_default(self):
		prompt = make_prompt(4)
		del prompt["switch"]["inputs"]["default"]

		replacements, fold_count, candidates, _ = MODULE._constant_fold_switches(prompt)

		self.assertEqual(replacements, {})
		self.assertEqual(fold_count, 0)
		self.assertEqual(candidates, 1)

	def test_existing_lazy_switch_types_still_fold(self):
		prompt = {
			"bool": {
				"class_type": "LazySwitch",
				"inputs": {
					"switch": True,
					"on_false": ["false", 0],
					"on_true": ["true", 0],
				},
			},
			"index": {
				"class_type": "LazyIndexSwitch",
				"inputs": {
					"index": 1,
					"value0": ["zero", 0],
					"value1": ["one", 0],
				},
			},
		}

		replacements, fold_count, candidates, _ = MODULE._constant_fold_switches(prompt)

		self.assertEqual(replacements["bool"], ["true", 0])
		self.assertEqual(replacements["index"], ["one", 0])
		self.assertEqual(fold_count, 2)
		self.assertEqual(candidates, 2)


if __name__ == "__main__":
	unittest.main()
