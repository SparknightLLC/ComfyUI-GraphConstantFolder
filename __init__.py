# ComfyUI-GraphConstantFolder
# Server-side on_prompt graph rewrite to constant-fold switch/select nodes so unused branches
# aren't traversed during prompt validation.

from .graph_constant_folder import install

install()

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
