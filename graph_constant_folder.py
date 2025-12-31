import os
import time
import json
import logging
import re
from typing import Any, Dict, Tuple, Optional, Set, List

_LOG_PREFIX = "[GraphConstantFolder]"

def _log_info(msg: str) -> None:
	logging.info(f"{_LOG_PREFIX} {msg}")

def _log_debug(msg: str) -> None:
	if _DEBUG:
		logging.info(f"{_LOG_PREFIX} {msg}")

def _log_verbose(msg: str) -> None:
	if _VERBOSE:
		logging.info(f"{_LOG_PREFIX} {msg}")

def _load_local_config() -> Dict[str, Any]:
	path = os.path.join(os.path.dirname(__file__), "graph_constant_folder_config.json")
	if not os.path.isfile(path):
		return {}

	try:
		with open(path, "r", encoding="utf-8") as f:
			cfg = json.load(f)
		return (cfg if isinstance(cfg, dict) else {})
	except Exception as e:
		_log_info(f"failed to read config file '{path}': {e}")
		return {}

def _truthy_str(v: str) -> bool:
	return v.strip() in ("1", "true", "True", "yes", "YES", "on", "ON")

def _falsy_str(v: str) -> bool:
	return v.strip() in ("0", "false", "False", "no", "NO", "off", "OFF", "")

def _get_flag(cfg: Dict[str, Any], cfg_key: str, env_names: List[str], default: bool) -> bool:
	for n in env_names:
		if n in os.environ:
			raw = str(os.environ.get(n, "")).strip()
			if _falsy_str(raw):
				return False
			if _truthy_str(raw):
				return True
			return True

	if cfg_key in cfg:
		v = cfg.get(cfg_key)
		if isinstance(v, bool):
			return v
		if isinstance(v, (int, float)):
			return bool(v)
		if isinstance(v, str):
			if _falsy_str(v):
				return False
			if _truthy_str(v):
				return True
			return True

	return default

_CFG = _load_local_config()

# Env vars: support both new prefix and legacy shorthand.
_ENABLED = _get_flag(_CFG, "ENABLED", ["GRAPH_CONSTANT_FOLDER_ENABLED", "GCF_ENABLED", "ENABLED"], default=True)
_DEBUG = _get_flag(_CFG, "DEBUG", ["GRAPH_CONSTANT_FOLDER_DEBUG", "GCF_DEBUG", "DEBUG"], default=False)
_VERBOSE = _get_flag(_CFG, "VERBOSE", ["GRAPH_CONSTANT_FOLDER_VERBOSE", "GCF_VERBOSE", "VERBOSE"], default=False)
_PRUNE = _get_flag(_CFG, "PRUNE", ["GRAPH_CONSTANT_FOLDER_PRUNE", "GCF_PRUNE", "PRUNE"], default=False)

# --- constant resolution -----------------------------------------------------

_DEFAULT_CONST_CLASS_RE = r"(?i)(?:primitive|constant|literal|bool\s*primitive|int\s*primitive|float\s*primitive)"
_CONST_CLASS_RE = re.compile(os.environ.get("GRAPH_CONSTANT_FOLDER_CONST_CLASS_TYPES", _DEFAULT_CONST_CLASS_RE))

try:
	from comfy_execution.graph_utils import is_link as _is_link
except Exception:
	def _is_link(v: Any) -> bool:
		return isinstance(v, list) and len(v) == 2 and isinstance(v[1], int)

def _coerce_bool(v: Any) -> Optional[bool]:
	if isinstance(v, bool):
		return v
	if isinstance(v, int) and not isinstance(v, bool):
		if v in (0, 1):
			return bool(v)
		return None
	if isinstance(v, float):
		if v == 0.0:
			return False
		if v == 1.0:
			return True
		return None
	if isinstance(v, str):
		s = v.strip().lower()
		if s in ("true", "yes", "on", "1"):
			return True
		if s in ("false", "no", "off", "0", ""):
			return False
	return None

def _coerce_int(v: Any) -> Optional[int]:
	if isinstance(v, bool):
		return None
	if isinstance(v, int):
		return v
	if isinstance(v, float) and v.is_integer():
		return int(v)
	if isinstance(v, str):
		s = v.strip()
		if re.fullmatch(r"[+-]?\d+", s):
			try:
				return int(s)
			except Exception:
				return None
	return None

def _get_inputs(node: Dict[str, Any]) -> Dict[str, Any]:
	inp = node.get("inputs", None)
	return inp if isinstance(inp, dict) else {}

def _resolve_constant(prompt: Dict[str, Any], value: Any, want: str, cache: Dict[Tuple[str, int, str], Any], depth: int = 12) -> Any:
	# direct literal
	if want == "bool":
		b = _coerce_bool(value)
		if b is not None:
			return b
	elif want == "int":
		i = _coerce_int(value)
		if i is not None:
			return i
	else:
		return None

	if depth <= 0 or not _is_link(value):
		return None

	src_id = str(value[0])
	out_idx = int(value[1])
	key = (src_id, out_idx, want)
	if key in cache:
		return cache[key]

	node = prompt.get(src_id, None)
	if not isinstance(node, dict):
		cache[key] = None
		return None

	class_type = str(node.get("class_type", ""))
	inputs = _get_inputs(node)

	# Reroute-like pass-through
	if "reroute" in class_type.lower():
		link_in = None
		for v in inputs.values():
			if _is_link(v):
				link_in = v
				break
		if link_in is not None:
			res = _resolve_constant(prompt, link_in, want, cache, depth - 1)
			cache[key] = res
			return res

	# Constant-like sources: class name matches AND no linked inputs
	if _CONST_CLASS_RE.search(class_type) and not any(_is_link(v) for v in inputs.values()):
		candidates = []
		for k in (
			"value", "bool", "boolean", "boolean_value", "state", "enabled", "enable",
			"switch", "toggle", "flag", "index", "int", "number"
		):
			if k in inputs:
				candidates.append(inputs.get(k))

		if not candidates and len(inputs) == 1:
			candidates.append(next(iter(inputs.values())))

		for c in candidates:
			if want == "bool":
				res = _coerce_bool(c)
				if res is not None:
					cache[key] = res
					return res
			elif want == "int":
				res = _coerce_int(c)
				if res is not None:
					cache[key] = res
					return res

	cache[key] = None
	return None

# --- folding rules -----------------------------------------------------------

def _try_fold_bool_switch(prompt: Dict[str, Any], node_inputs: Dict[str, Any], cache: Dict[Tuple[str, int, str], Any], decision_key: str, false_key: str, true_key: str) -> Optional[Any]:
	switch_raw = node_inputs.get(decision_key, None)
	switch = _resolve_constant(prompt, switch_raw, "bool", cache)
	if not isinstance(switch, bool):
		return None

	if false_key not in node_inputs or true_key not in node_inputs:
		return None

	return (node_inputs.get(true_key) if switch else node_inputs.get(false_key))

def _try_fold_index_switch(prompt: Dict[str, Any], node_inputs: Dict[str, Any], cache: Dict[Tuple[str, int, str], Any], decision_key: str, value_prefix: str) -> Optional[Any]:
	index_raw = node_inputs.get(decision_key, None)
	index = _resolve_constant(prompt, index_raw, "int", cache)
	if not isinstance(index, int):
		return None

	key = f"{value_prefix}{index}"
	if key not in node_inputs:
		return None

	return node_inputs.get(key)

def _try_fold_lazy_conditional(prompt: Dict[str, Any], node_inputs: Dict[str, Any], cache: Dict[Tuple[str, int, str], Any]) -> Optional[Any]:
	cond_idxs = []
	for k in node_inputs.keys():
		m = re.fullmatch(r"condition(\d+)", str(k))
		if m:
			cond_idxs.append(int(m.group(1)))

	if not cond_idxs:
		return None

	for i in sorted(cond_idxs):
		cond_key = f"condition{i}"
		cond_val_raw = node_inputs.get(cond_key, None)
		cond_val = _resolve_constant(prompt, cond_val_raw, "bool", cache)
		if not isinstance(cond_val, bool):
			return None

		if cond_val:
			val_key = f"value{i}"
			if val_key not in node_inputs:
				return None
			return node_inputs.get(val_key)

	if "else" in node_inputs:
		return node_inputs.get("else")

	return None

def _has_keys(d: Dict[str, Any], keys: List[str]) -> bool:
	for k in keys:
		if k not in d:
			return False
	return True

def _is_bool_switch_like(inputs: Dict[str, Any]) -> bool:
	if _has_keys(inputs, ["switch", "on_true", "on_false"]):
		return True
	if _has_keys(inputs, ["condition", "if_true", "if_false"]):
		return True
	return False

def _is_index_switch_like(inputs: Dict[str, Any]) -> bool:
	if "index" in inputs:
		return ("value0" in inputs and "value1" in inputs)
	return False

def _constant_fold_switches(prompt: Dict[str, Any]) -> Tuple[Dict[str, Any], int, int, List[str]]:
	# Returns (replacements, fold_count, candidates, not_foldable_messages)
	replacements: Dict[str, Any] = {}
	fold_count = 0
	candidates = 0
	cache: Dict[Tuple[str, int, str], Any] = {}
	not_foldable: List[str] = []

	for node_id, node in prompt.items():
		if not isinstance(node, dict):
			continue

		class_type = str(node.get("class_type", ""))
		inputs = _get_inputs(node)

		kind = None

		# Known lazy nodes
		if class_type in ("LazySwitch", "LazyIndexSwitch", "LazyConditional"):
			kind = class_type
		elif class_type == "LazySwitchKJ":
			kind = "LazySwitch"

		# Switch-like nodes (non-lazy)
		elif _is_bool_switch_like(inputs):
			kind = "BoolSwitchLike"
		elif _is_index_switch_like(inputs):
			kind = "IndexSwitchLike"

		if kind is None:
			continue

		candidates += 1
		replacement = None

		if kind in ("LazySwitch", "BoolSwitchLike"):
			if _has_keys(inputs, ["switch", "on_true", "on_false"]):
				replacement = _try_fold_bool_switch(prompt, inputs, cache, "switch", "on_false", "on_true")
			elif _has_keys(inputs, ["condition", "if_true", "if_false"]):
				replacement = _try_fold_bool_switch(prompt, inputs, cache, "condition", "if_false", "if_true")

			if replacement is None and _VERBOSE:
				not_foldable.append(f"not foldable: {class_type} #{node_id} (decision input not constant)")

		elif kind in ("LazyIndexSwitch", "IndexSwitchLike"):
			replacement = _try_fold_index_switch(prompt, inputs, cache, "index", "value")
			if replacement is None and _VERBOSE:
				not_foldable.append(f"not foldable: {class_type} #{node_id} (decision input not constant or missing valueN)")

		elif kind == "LazyConditional":
			replacement = _try_fold_lazy_conditional(prompt, inputs, cache)
			if replacement is None and _VERBOSE:
				not_foldable.append(f"not foldable: {class_type} #{node_id} (conditions not constant)")

		if replacement is None:
			continue

		replacements[str(node_id)] = replacement
		fold_count += 1

	return (replacements, fold_count, candidates, not_foldable)

def _resolve_replacement_chain(replacements: Dict[str, Any], value: Any) -> Any:
	seen: Set[str] = set()
	v = value
	while _is_link(v):
		src = str(v[0])
		if src in seen or src not in replacements:
			break
		seen.add(src)
		v = replacements[src]
	return v

def _rewrite_value(value: Any, replacements: Dict[str, Any]) -> Tuple[Any, bool]:
	if _is_link(value):
		src_id = str(value[0])
		out_idx = value[1]
		if out_idx == 0 and src_id in replacements:
			new_v = _resolve_replacement_chain(replacements, replacements[src_id])
			if new_v != value:
				return (new_v, True)
		return (value, False)

	if isinstance(value, list):
		changed_any = False
		new_list = []
		for v in value:
			new_v, changed = _rewrite_value(v, replacements)
			new_list.append(new_v)
			changed_any = changed_any or changed
		return (new_list if changed_any else value, changed_any)

	if isinstance(value, dict):
		changed_any = False
		new_dict = {}
		for k, v in value.items():
			new_v, changed = _rewrite_value(v, replacements)
			new_dict[k] = new_v
			changed_any = changed_any or changed
		return (new_dict if changed_any else value, changed_any)

	return (value, False)

def _apply_replacements(prompt: Dict[str, Any], replacements: Dict[str, Any]) -> Set[str]:
	changed_nodes: Set[str] = set()
	for node_id, node in prompt.items():
		if not isinstance(node, dict):
			continue
		inputs = node.get("inputs", None)
		if not isinstance(inputs, dict):
			continue
		new_inputs, changed = _rewrite_value(inputs, replacements)
		if changed:
			node["inputs"] = new_inputs
			changed_nodes.add(str(node_id))
	return changed_nodes

def _walk_upstream(prompt: Dict[str, Any], start_nodes: Set[str]) -> Set[str]:
	reachable: Set[str] = set()
	stack = list(start_nodes)
	while stack:
		nid = str(stack.pop())
		if nid in reachable:
			continue
		reachable.add(nid)
		node = prompt.get(nid, None)
		if not isinstance(node, dict):
			continue
		inputs = node.get("inputs", None)
		if not isinstance(inputs, dict):
			continue

		def push_link(v: Any) -> None:
			if _is_link(v):
				stack.append(str(v[0]))
			elif isinstance(v, list):
				for vv in v:
					if _is_link(vv):
						stack.append(str(vv[0]))
			elif isinstance(v, dict):
				for vv in v.values():
					if _is_link(vv):
						stack.append(str(vv[0]))

		for v in inputs.values():
			push_link(v)

	return reachable

def _collect_outputs(prompt: Dict[str, Any], prefer_targets: Optional[Set[str]] = None) -> Set[str]:
	if prefer_targets:
		return set(str(x) for x in prefer_targets)

	try:
		import nodes as comfy_nodes
	except Exception:
		return set()

	outputs: Set[str] = set()
	for node_id, node in prompt.items():
		class_type = node.get("class_type", None)
		if not class_type:
			continue
		class_def = comfy_nodes.NODE_CLASS_MAPPINGS.get(class_type, None)
		if class_def is None:
			continue
		if getattr(class_def, "OUTPUT_NODE", False) is True:
			outputs.add(str(node_id))
	return outputs

def _extract_partial_targets(json_data: Dict[str, Any]) -> Optional[Set[str]]:
	for k in ("partial_execution_targets", "partial_execution_list", "partial_execution_nodes", "partial_execution"):
		v = json_data.get(k, None)
		if isinstance(v, list) and v:
			try:
				return set(str(x) for x in v)
			except Exception:
				continue
	return None

def _prune_unreachable(prompt: Dict[str, Any], start_nodes: Set[str]) -> Tuple[Dict[str, Any], int]:
	reachable = _walk_upstream(prompt, start_nodes)
	removed = 0
	new_prompt: Dict[str, Any] = {}
	for node_id, node in prompt.items():
		sid = str(node_id)
		if sid in reachable:
			new_prompt[sid] = node
		else:
			removed += 1
	return (new_prompt, removed)

def _handler(json_data: Dict[str, Any]) -> Dict[str, Any]:
	if not _ENABLED:
		return json_data

	prompt = json_data.get("prompt", None)
	if not isinstance(prompt, dict) or not prompt:
		return json_data

	t0 = time.perf_counter()

	replacements, fold_count, candidates, not_foldable = _constant_fold_switches(prompt)

	if _VERBOSE:
		for msg in not_foldable:
			_log_verbose(msg)

	if _DEBUG or _VERBOSE:
		_log_debug(f"on_prompt: nodes={len(prompt)}, switch_candidates={candidates}, foldable={fold_count}, prune={int(_PRUNE)}, verbose={int(_VERBOSE)}")

	if fold_count == 0:
		return json_data

	changed_nodes = _apply_replacements(prompt, replacements)

	removed = 0
	if _PRUNE:
		targets = _extract_partial_targets(json_data)
		start_nodes = _collect_outputs(prompt, targets)
		new_prompt, removed = _prune_unreachable(prompt, start_nodes)
		json_data["prompt"] = new_prompt

	dt_ms = (time.perf_counter() - t0) * 1000.0
	if _DEBUG or _VERBOSE:
		_log_debug(f"rewrote nodes={len(changed_nodes)}, pruned={removed}, dt_ms={dt_ms:.2f}")

	return json_data

_installed = False

def install() -> None:
	global _installed
	if _installed:
		return
	_installed = True

	if not _ENABLED:
		_log_info("disabled (set GRAPH_CONSTANT_FOLDER_ENABLED=0 or GCF_ENABLED=0)")
		return

	try:
		from server import PromptServer
	except Exception as e:
		_log_info(f"could not import PromptServer: {e}")
		return

	inst = getattr(PromptServer, "instance", None)
	if inst is None:
		_log_info("PromptServer.instance is None; handler not installed")
		return

	if not hasattr(inst, "add_on_prompt_handler"):
		_log_info("ComfyUI server missing add_on_prompt_handler; update ComfyUI")
		return

	inst.add_on_prompt_handler(_handler)
	_log_info("installed on_prompt handler (constant-fold: lazy switches + switch-like nodes)")
