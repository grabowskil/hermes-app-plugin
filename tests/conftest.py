"""Load the plugin directory as the package ``hermes_app`` (the directory itself is named ``plugin``)."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _ensure_hermes_on_path():
    """The editable install of hermes-agent can miss top-level modules (hermes_yaml); add its root."""
    try:
        import hermes_yaml  # noqa: F401
        return
    except ImportError:
        pass
    import hermes_cli

    sys.path.append(str(Path(hermes_cli.__file__).resolve().parent.parent))


_ensure_hermes_on_path()


def _load():
    if "hermes_app" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "hermes_app", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_app"] = module
    spec.loader.exec_module(module)


_load()
